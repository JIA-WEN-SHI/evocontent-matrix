"""Idempotent links from saved public captures to scoped reference evidence."""
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException

from app.services.kb_service import upsert_tags_for_entity
from app.services.browser_capture import guard_capture_metadata


def _ensure_record(client, table, record):
    rows = client.table(table).select('*').eq('id', record['id']).limit(1).execute().data or []
    if rows:
        row = rows[0]
        if row.get('account_id') != record['account_id'] or row.get('domain_id') != record['domain_id'] or row.get('deleted_at'):
            raise HTTPException(409, '关联记录归属不一致或已删除，请先人工核对')
        return row
    try:
        rows = client.table(table).insert(record).execute().data or []
    except Exception:
        # Recover only a confirmed same-ID insert; an unknown write is not success.
        rows = client.table(table).select('*').eq('id', record['id']).limit(1).execute().data or []
        if not rows:
            raise
    if not rows:
        raise RuntimeError('knowledge_write_unconfirmed')
    row = rows[0]
    if row.get('account_id') != record['account_id'] or row.get('domain_id') != record['domain_id']:
        raise HTTPException(409, '关联记录归属不一致')
    return row


def link_capture_to_knowledge(client, *, account_id, domain_id, item_id, actor):
    if actor.role not in {'admin', 'operator'}:
        raise HTTPException(403, '整理素材需要运营权限')
    account_id, domain_id, item_id = map(lambda v: str(UUID(v)), (account_id, domain_id, item_id))
    rows = client.table('intelligence_items').select('*').eq('id', item_id).eq('account_id', account_id).eq('domain_id', domain_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(404, '未找到此账号的采集资料')
    item = rows[0]
    capture = (item.get('meta_jsonb') or {}).get('browser_capture') or {}
    body, url = str(capture.get('body_text') or '').strip(), str(capture.get('source_url') or '')
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise HTTPException(422, '资料来源链接无效') from None
    if not body or parts.scheme != 'https' or parts.hostname not in {'www.xiaohongshu.com', 'xiaohongshu.com'} or parts.query or parts.fragment or parts.username or parts.password or port not in {None, 443} or not re.fullmatch(r'/(?:explore|discovery/item)/[a-fA-F0-9]{24}', parts.path):
        raise HTTPException(422, '资料正文或来源无效，不能计为有效素材')
    existing = client.table('cases').select('id,account_id').eq('domain_id', domain_id).eq('platform', 'xiaohongshu').eq('url', url).is_('deleted_at', 'null').limit(1).execute().data or []
    if existing and existing[0].get('account_id') != account_id:
        raise HTTPException(409, '同一来源已属于其他账号，请核对数据库案例唯一索引；未覆盖任何记录')
    common = {'domain_id': domain_id, 'account_id': account_id, 'source_type': 'cli_reference',
        'source_ref': item_id, 'created_by': actor.user_id}
    identity = f'{domain_id}:{account_id}:{url}'
    case = _ensure_record(client, 'cases', {**common,
        'id': existing[0]['id'] if existing else str(uuid5(NAMESPACE_URL, 'case:'+identity)),
        'platform': 'xiaohongshu', 'author': str(capture.get('author_name') or '')[:200],
        'url': url, 'title': str(capture.get('title') or '采集参考')[:300], 'content': body,
        'analysis': '', 'metrics': {}})
    asset = _ensure_record(client, 'assets', {**common, 'id': str(uuid5(NAMESPACE_URL, 'asset:'+identity)),
        'type': 'reference', 'content': body, 'source': url, 'usable_scene': '选题与结构参考，不作为本人原创或亲测结论',
        'is_verified': False, 'summary': str(capture.get('title') or '')[:300]})
    warnings = []
    for kind, row in [('case', case), ('asset', asset)]:
        try:
            upsert_tags_for_entity(client, domain_id=domain_id, account_id=account_id, entity_type=kind,
                entity_id=row['id'], tags=capture.get('tags') or [], created_by=actor.user_id,
                source_type='cli_reference', source_ref=item_id)
        except Exception:
            warnings.append('标签关联未完成，可重试整理；正文关联已保存')
        log_id = str(uuid5(NAMESPACE_URL, f'knowledge:{kind}:{row["id"]}'))
        _ensure_record(client, 'ingestion_logs', {**{k: common[k] for k in ('domain_id', 'account_id', 'created_by')},
            'id': log_id, 'source': 'cli_reference', 'source_run_id': item_id, 'entity_type': kind,
            'status': 'success', 'normalized_count': 1, 'success_count': 1, 'failed_count': 0,
            'request_payload': {'entity_ids': [row['id']], 'source_url': url}})
    links = {'status': 'ok', 'case_ids': [case['id']], 'asset_ids': [asset['id']],
        'linked_at': datetime.now(timezone.utc).isoformat(), 'source_url': url}
    meta = dict(item.get('meta_jsonb') or {})
    meta['knowledge_links'] = links
    meta['content_revision'] = uuid4().hex
    query = client.table('intelligence_items').update({'meta_jsonb': meta}).eq('id', item_id).eq('account_id', account_id).eq('domain_id', domain_id)
    saved = guard_capture_metadata(query, item.get('meta_jsonb') or {}).execute().data or []
    if not saved:
        raise RuntimeError('knowledge_link_write_unconfirmed')
    return {**links, 'warnings': warnings}
