-- Compatibility views for knowledge-base workflows.
-- Exposes runtime publishing/review data with stable view names.

do $$
begin
  if exists (
    select 1 from information_schema.tables
    where table_schema = 'public' and table_name = 'pipeline_tasks'
  ) then
    execute $sql$
      create or replace view v_content_items as
      select
        pt.id as id,
        pt.id as pipeline_task_id,
        pt.domain_id,
        pt.account_id,
        pt.channel as platform,
        pt.published_at as publish_time,
        coalesce(
          nullif(pt.publish_jsonb -> 'identity' ->> 'published_url', ''),
          nullif(pt.publish_jsonb -> 'last_result' ->> 'published_url', ''),
          nullif(pt.payload_jsonb ->> 'published_url', '')
        ) as url,
        coalesce(
          nullif(pt.payload_jsonb ->> 'title', ''),
          nullif(pt.intent_jsonb ->> 'topic', ''),
          pt.id::text
        ) as title,
        null::uuid as topic_id,
        pt.status,
        pt.stage,
        pt.metrics_jsonb,
        pt.payload_jsonb,
        pt.publish_jsonb,
        pt.created_at,
        pt.updated_at
      from pipeline_tasks pt;
    $sql$;
  else
    execute $sql$
      create or replace view v_content_items as
      select
        null::uuid as id,
        null::uuid as pipeline_task_id,
        null::uuid as domain_id,
        null::uuid as account_id,
        null::text as platform,
        null::timestamptz as publish_time,
        null::text as url,
        null::text as title,
        null::uuid as topic_id,
        null::text as status,
        null::text as stage,
        '{}'::jsonb as metrics_jsonb,
        '{}'::jsonb as payload_jsonb,
        '{}'::jsonb as publish_jsonb,
        null::timestamptz as created_at,
        null::timestamptz as updated_at
      where false;
    $sql$;
  end if;
end $$;

do $$
begin
  if exists (
    select 1 from information_schema.tables
    where table_schema = 'public' and table_name = 'task_metrics'
  ) and exists (
    select 1 from information_schema.tables
    where table_schema = 'public' and table_name = 'pipeline_tasks'
  ) then
    execute $sql$
      create or replace view v_reviews_runtime as
      with latest_insight as (
        select distinct on (ei.pipeline_task_id)
          ei.pipeline_task_id,
          ei.insight_jsonb,
          ei.action_jsonb,
          ei.confidence,
          ei.created_at
        from evolution_insights ei
        where ei.pipeline_task_id is not null
        order by ei.pipeline_task_id, ei.created_at desc
      )
      select
        tm.id as task_metric_id,
        tm.pipeline_task_id,
        pt.domain_id,
        pt.account_id,
        pt.channel as platform,
        tm.metric_window,
        tm.metrics_jsonb,
        tm.score,
        tm.captured_at,
        pt.review_jsonb,
        coalesce(li.insight_jsonb, '{}'::jsonb) as insight_jsonb,
        coalesce(li.action_jsonb, '{}'::jsonb) as action_jsonb,
        li.confidence as insight_confidence,
        li.created_at as insight_created_at,
        pt.status,
        pt.stage,
        pt.created_at as task_created_at,
        pt.updated_at as task_updated_at
      from task_metrics tm
      join pipeline_tasks pt on pt.id = tm.pipeline_task_id
      left join latest_insight li on li.pipeline_task_id = tm.pipeline_task_id;
    $sql$;
  else
    execute $sql$
      create or replace view v_reviews_runtime as
      select
        null::uuid as task_metric_id,
        null::uuid as pipeline_task_id,
        null::uuid as domain_id,
        null::uuid as account_id,
        null::text as platform,
        null::text as metric_window,
        '{}'::jsonb as metrics_jsonb,
        null::numeric as score,
        null::timestamptz as captured_at,
        '{}'::jsonb as review_jsonb,
        '{}'::jsonb as insight_jsonb,
        '{}'::jsonb as action_jsonb,
        null::numeric as insight_confidence,
        null::timestamptz as insight_created_at,
        null::text as status,
        null::text as stage,
        null::timestamptz as task_created_at,
        null::timestamptz as task_updated_at
      where false;
    $sql$;
  end if;
end $$;
