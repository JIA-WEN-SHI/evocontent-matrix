"""Persist actual scoped observations without converting unknowns to zeros."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException

from app.models import AuditLogEntry
from app.services.audit import write_audit_log
from app.services.xhs_cli_collection import call_cli, CliFailure

COUNTERS = {'views', 'likes', 'collects', 'comments_count', 'shares', 'followers_delta'}


def _time(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise HTTPException(422, '观测时间必须包含时区') from None


def _feed_id(url):
    try:
        parts = urlsplit(str(url))
        port = parts.port
    except ValueError:
        raise HTTPException(422, '请输入有效的小红书笔记链接') from None
    match = re.fullmatch(r'/(?:explore|discovery/item)/([a-f0-9]{24})', parts.path)
    if parts.scheme != 'https' or parts.hostname not in {'www.xiaohongshu.com', 'xiaohongshu.com'} or parts.username or parts.password or port not in {None, 443} or not match:
        raise HTTPException(422, '请输入有效的小红书笔记链接')
    return match[1]


def normalize_values(values):
    result = {}
    if not isinstance(values, dict) or set(values)-COUNTERS:
        raise HTTPException(422, '指标字段无效')
    for name, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            raise HTTPException(422, '指标不能是布尔值')
        try:
            number = float(value)
        except (ValueError, TypeError):
            raise HTTPException(422, '指标必须是明确的整数，不能填写约数') from None
        if not math.isfinite(number) or not number.is_integer() or abs(number) > 10**12 or (number < 0 and name != 'followers_delta'):
            raise HTTPException(422, '指标必须是有效计数，涨粉可填写负数')
        result[name] = int(number)
    if not result:
        raise HTTPException(422, '至少填写一项真实观测指标；未知项请留空')
    return result


def read_cli_observation(*, note_id, call=call_cli):
    if not re.fullmatch(r'[a-f0-9]{24}', str(note_id)):
        raise HTTPException(422, '笔记身份无效')
    data = call('read', note_id, timeout=40)
    items = data.get('items') or []
    row = items[0] if items and isinstance(items[0], dict) else {}
    card = row.get('note_card') or data.get('note_card') or {}
    if not isinstance(card, dict) or not any(v == note_id for v in (row.get('id'), card.get('note_id'))) or any(v and v != note_id for v in (row.get('id'), card.get('note_id'))):
        raise HTTPException(409, '读取结果与已发布笔记不一致，未保存指标')
    stat = card.get('interact_info') or {}
    aliases = {'views': ('views', 'view_count'), 'likes': ('likes', 'liked_count', 'like_count'),
        'collects': ('collects', 'collected_count', 'collect_count'), 'comments_count': ('comments_count', 'comment_count'),
        'shares': ('shares', 'share_count'), 'followers_delta': ('followers_delta',)}
    values = {}
    for name, keys in aliases.items():
        for key in keys:
            value = stat.get(key, card.get(key))
            if value is not None:
                try:
                    values.update(normalize_values({name: value}))
                except HTTPException:
                    pass
                break
    if not values:
        raise HTTPException(503, '接口没有返回可确认的指标，未填入假零；可手动补录')
    return {'values': values, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'provider': 'xiaohongshu_cli', 'source_ref': 'https://www.xiaohongshu.com/explore/'+note_id,
        'provenance': 'public_note_detail'}


def _scoped_task(client, pipeline_task_id, account_id, domain_slug):
    domain = client.table('domains').select('id').eq('slug', domain_slug).limit(1).execute().data or []
    if not domain:
        raise HTTPException(404, '赛道不存在')
    rows = client.table('pipeline_tasks').select('*').eq('id', str(UUID(pipeline_task_id))).eq('account_id', str(UUID(account_id))).eq('domain_id', domain[0]['id']).limit(1).execute().data or []
    if not rows:
        raise HTTPException(404, '未找到此账号的任务')
    task = rows[0]
    if task.get('status') not in {'published', 'metrics_ready', 'reflecting', 'reflection_failed', 'done'} or not task.get('published_at'):
        raise HTTPException(409, '请先回填真实发布链接和时间')
    return task


def save_task_observation(client, *, pipeline_task_id, account_id, domain_slug, observation, actor):
    if actor.role not in {'admin', 'operator'}:
        raise HTTPException(403, '反馈录入需要运营权限')
    task = _scoped_task(client, pipeline_task_id, account_id, domain_slug)
    values = normalize_values(observation.get('values'))
    observed = _time(observation.get('observed_at'))
    if observed > datetime.now(timezone.utc)+timedelta(minutes=1) or observed < _time(task['published_at']):
        raise HTTPException(422, '观测时间不能早于发布或晚于当前时间')
    identity = (task.get('publish_jsonb') or {}).get('identity') or {}
    feed_id = identity.get('feed_id') or _feed_id(identity.get('published_url'))
    if _feed_id(observation.get('source_ref')) != feed_id:
        raise HTTPException(409, '观测来源不是这篇已发布笔记')
    provider = observation.get('provider')
    provenance = str(observation.get('provenance') or '').strip()[:200]
    if provider not in {'manual', 'xiaohongshu_cli'} or not provenance:
        raise HTTPException(422, '必须标记真实观测来源')
    normalized = {'values': values, 'observed_at': observed.isoformat(), 'provider': provider,
        'source_ref': 'https://www.xiaohongshu.com/explore/'+feed_id, 'provenance': provenance}
    snapshot_id = str(uuid5(NAMESPACE_URL, pipeline_task_id+sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()))
    metrics = dict(task.get('metrics_jsonb') or {})
    if metrics.get('post_metrics_synced_at') and _time(metrics['post_metrics_synced_at']) > observed:
        raise HTTPException(409, '已有更新的观测数据，请核对后再补录')
    existing = client.table('task_metrics').select('id').eq('id', snapshot_id).eq('pipeline_task_id', pipeline_task_id).limit(1).execute().data or []
    if not existing:
        client.table('task_metrics').insert({'id': snapshot_id, 'pipeline_task_id': pipeline_task_id,
            'metric_window': 'observed', 'metrics_jsonb': {'post_metrics': values, 'observation': normalized, 'metrics_mode': 'real'},
            'captured_at': observed.isoformat()}).execute()
    metrics.update({'post_metrics': values, 'post_metrics_synced_at': observed.isoformat(),
        'metrics_mode': 'real', 'observation': normalized, 'observation_id': snapshot_id})
    if task.get('metrics_jsonb') == metrics:
        return task
    patch = {'metrics_jsonb': metrics, 'updated_at': datetime.now(timezone.utc).isoformat()}
    query = client.table('pipeline_tasks').update(patch).eq('id', pipeline_task_id).eq('account_id', account_id).eq('domain_id', task['domain_id'])
    if task.get('updated_at'):
        query = query.eq('updated_at', task['updated_at'])
    saved = query.execute().data or []
    if not saved:
        raise HTTPException(409, '任务已更新，请刷新核对；本轮观测已保留')
    write_audit_log(client, AuditLogEntry(actor=actor.user_id, action='pipeline.observation_saved',
        target_type='pipeline_task', target_id=pipeline_task_id,
        diff_jsonb={'observation_id': snapshot_id, 'provider': provider, 'observed_at': normalized['observed_at'], 'fields': list(values)}))
    return saved[0]


def sync_task_observation(client, *, pipeline_task_id, account_id, domain_slug, actor):
    task = _scoped_task(client, pipeline_task_id, account_id, domain_slug)
    account = client.table('channel_accounts').select('config_jsonb').eq('id', account_id).limit(1).execute().data or []
    if not account or ((account[0].get('config_jsonb') or {}).get('collection_plan') or {}).get('mode') != 'xhs_cli':
        raise HTTPException(409, '此入口适用于本地CLI账号，其他来源请用手动观测录入')
    identity = (task.get('publish_jsonb') or {}).get('identity') or {}
    note_id = identity.get('feed_id') or _feed_id(identity.get('published_url'))
    try:
        observation = read_cli_observation(note_id=note_id)
    except CliFailure as exc:
        raise HTTPException(503, '指标读取未完成：'+exc.code+'；已有观测仍保留，可手动补录') from None
    return save_task_observation(client, pipeline_task_id=pipeline_task_id, account_id=account_id,
        domain_slug=domain_slug, observation=observation, actor=actor)
