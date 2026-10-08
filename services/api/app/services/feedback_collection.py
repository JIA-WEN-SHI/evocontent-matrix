"""Collect due CLI observations before persisted agent-side reconciliation."""
from datetime import datetime, timedelta, timezone

from app.services.kb_service import resolve_domain
from app.services.task_observations import sync_task_observation


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def collect_due_observations(client, *, domain_slug, account_id='', limit=20, actor):
    domain = resolve_domain(client, domain_slug)
    query = client.table('channel_accounts').select('id,is_active,config_jsonb').eq('is_active', True)
    if account_id:
        query = query.eq('id', account_id)
    accounts = {row['id']: row for row in query.execute().data or []
                if ((row.get('config_jsonb') or {}).get('collection_plan') or {}).get('mode') == 'xhs_cli'}
    if not accounts:
        return {'synced': 0, 'failures': []}
    tasks = (client.table('pipeline_tasks').select('*').eq('domain_id', domain['id'])
             .in_('account_id', list(accounts)).in_('status', ['published', 'metrics_ready', 'reflection_failed'])
             .order('updated_at').limit(500).execute().data or [])
    now = datetime.now(timezone.utc)
    count, failures, attempted = 0, [], 0
    for task in tasks:
        published = _time(task.get('published_at'))
        if not published or published > now:
            continue
        state = task.get('publish_jsonb') or {}
        next_at = _time(state.get('next_feedback_at'))
        plan = (accounts[task['account_id']].get('config_jsonb') or {}).get('feedback_plan') or {}
        hours = [hour for hour in plan.get('checkpoints_hours', [1, 3, 24]) if type(hour) is int and hour > 0]
        first_at = published + timedelta(hours=min(hours or [1]))
        if now < (next_at or first_at):
            continue
        last = _time((task.get('metrics_jsonb') or {}).get('post_metrics_synced_at'))
        # A saved observation must be reconciled before collecting another one.
        reviewed = _time(state.get('last_feedback_at'))
        if last and (not reviewed or last > reviewed):
            continue
        if last and now-last < timedelta(minutes=5):
            continue
        attempted += 1
        try:
            sync_task_observation(client, pipeline_task_id=task['id'], account_id=task['account_id'], domain_slug=domain_slug, actor=actor)
            count += 1
        except Exception:
            failures.append({'task_id': task['id'], 'message': '真实观测暂未读取成功，已保留历史数据，可手动补录'})
        if attempted >= max(1, min(limit, 50)):
            break
    return {'synced': count, 'failures': failures}
