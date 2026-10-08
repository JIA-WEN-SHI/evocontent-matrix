-- KB governance extension:
-- 1) rulebook library (platform/compliance/quality/style rules)
-- 2) playbook library (methodology for collect/analysis/copy/review)
-- 3) IO rules library (ingest/egress contracts and gates)

create extension if not exists pgcrypto;

create table if not exists kb_rulebooks (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  rule_code text not null,
  rule_name text not null default '',
  rule_type text not null default 'platform_policy' check (
    rule_type in ('platform_policy', 'compliance', 'quality_gate', 'style', 'delivery')
  ),
  applies_to text[] not null default array[]::text[],
  severity text not null default 'warn' check (severity in ('info', 'warn', 'blocker')),
  priority integer not null default 100,
  source_level text not null default 'manual' check (
    source_level in ('official', 'law', 'platform_help', 'crawler_observed', 'synthetic', 'manual')
  ),
  effective_at timestamptz,
  citation_title text not null default '',
  citation_url text not null default '',
  rule_text text not null default '',
  rule_jsonb jsonb not null default '{}'::jsonb,
  status text not null default 'active' check (status in ('draft', 'active', 'inactive', 'archived')),
  source_type text not null default 'manual',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists kb_playbooks (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  playbook_code text not null,
  playbook_name text not null default '',
  stage text not null default 'analysis' check (
    stage in ('collect', 'analysis', 'rewrite', 'copy', 'review', 'strategy')
  ),
  objective text not null default '',
  applicability text not null default '',
  input_contract jsonb not null default '{}'::jsonb,
  method_steps jsonb not null default '[]'::jsonb,
  output_contract jsonb not null default '{}'::jsonb,
  quality_checks jsonb not null default '{}'::jsonb,
  example_material jsonb not null default '{}'::jsonb,
  status text not null default 'active' check (status in ('draft', 'active', 'inactive', 'archived')),
  version integer not null default 1,
  source_level text not null default 'manual' check (
    source_level in ('official', 'law', 'platform_help', 'crawler_observed', 'synthetic', 'manual')
  ),
  source_type text not null default 'manual',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists kb_io_rules (
  id uuid primary key default gen_random_uuid(),
  domain_id uuid not null references domains(id) on delete cascade,
  account_id uuid references channel_accounts(id) on delete set null,
  rule_code text not null,
  direction text not null check (direction in ('ingest', 'egress')),
  entity_type text not null check (
    entity_type in ('case', 'asset', 'user_need', 'review', 'topic', 'rulebook', 'playbook')
  ),
  target_agent text not null default '',
  required_fields jsonb not null default '[]'::jsonb,
  field_mapping jsonb not null default '{}'::jsonb,
  validation_jsonb jsonb not null default '{}'::jsonb,
  dedupe_keys jsonb not null default '[]'::jsonb,
  quality_gate_jsonb jsonb not null default '{}'::jsonb,
  output_template jsonb not null default '{}'::jsonb,
  status text not null default 'active' check (status in ('draft', 'active', 'inactive', 'archived')),
  source_type text not null default 'manual',
  source_ref text not null default '',
  created_by text not null default 'system',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists idx_kb_rulebooks_domain_account_code
  on kb_rulebooks (domain_id, coalesce(account_id::text, ''), lower(rule_code))
  where deleted_at is null;

create index if not exists idx_kb_rulebooks_domain_status_type
  on kb_rulebooks (domain_id, status, rule_type, updated_at desc)
  where deleted_at is null;

create index if not exists idx_kb_rulebooks_applies_to
  on kb_rulebooks using gin (applies_to);

create unique index if not exists idx_kb_playbooks_domain_account_code_version
  on kb_playbooks (domain_id, coalesce(account_id::text, ''), lower(playbook_code), version)
  where deleted_at is null;

create index if not exists idx_kb_playbooks_domain_stage_status
  on kb_playbooks (domain_id, stage, status, updated_at desc)
  where deleted_at is null;

create unique index if not exists idx_kb_io_rules_domain_account_code
  on kb_io_rules (domain_id, coalesce(account_id::text, ''), lower(rule_code), direction, entity_type)
  where deleted_at is null;

create index if not exists idx_kb_io_rules_domain_direction_status
  on kb_io_rules (domain_id, direction, status, updated_at desc)
  where deleted_at is null;

create index if not exists idx_kb_io_rules_target_agent
  on kb_io_rules (target_agent, direction, updated_at desc)
  where deleted_at is null and target_agent <> '';

do $$
begin
  if exists (select 1 from pg_proc where proname = 'kb_touch_updated_at') then
    if not exists (select 1 from pg_trigger where tgname = 'trg_kb_rulebooks_touch_updated_at') then
      create trigger trg_kb_rulebooks_touch_updated_at before update on kb_rulebooks
        for each row execute function kb_touch_updated_at();
    end if;
    if not exists (select 1 from pg_trigger where tgname = 'trg_kb_playbooks_touch_updated_at') then
      create trigger trg_kb_playbooks_touch_updated_at before update on kb_playbooks
        for each row execute function kb_touch_updated_at();
    end if;
    if not exists (select 1 from pg_trigger where tgname = 'trg_kb_io_rules_touch_updated_at') then
      create trigger trg_kb_io_rules_touch_updated_at before update on kb_io_rules
        for each row execute function kb_touch_updated_at();
    end if;
  end if;
end $$;

do $$
declare
  d_id uuid;
begin
  select id into d_id from domains where slug = 'japan_immigration' limit 1;
  if d_id is null then
    return;
  end if;

  -- Rulebook seeds (real operation constraints for xiaohongshu content pipeline).
  if not exists (
    select 1 from kb_rulebooks
    where domain_id = d_id and lower(rule_code) = 'xhs_content_safety_basic' and deleted_at is null
  ) then
    insert into kb_rulebooks (
      domain_id, rule_code, rule_name, rule_type, applies_to, severity, priority, source_level,
      citation_title, citation_url, rule_text, rule_jsonb, status, source_type, source_ref, created_by
    ) values (
      d_id,
      'xhs_content_safety_basic',
      '小红书内容安全基础约束',
      'platform_policy',
      array['copy', 'review', 'publish'],
      'blocker',
      10,
      'platform_help',
      '小红书社区规范（内容安全与违规治理）',
      'https://www.xiaohongshu.com',
      '不得发布违规承诺、虚假保证、极限化效果描述；涉及移民/签证信息需给出条件边界，不得宣称必过、包过或收益保证。',
      jsonb_build_object(
        'hard_block_patterns', jsonb_build_array('包过', '百分百成功', '内部渠道', '保证下签'),
        'must_have_disclaimer', true,
        'disclaimer_example', '本文仅为信息参考，需结合个人条件评估。'
      ),
      'active',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;

  if not exists (
    select 1 from kb_rulebooks
    where domain_id = d_id and lower(rule_code) = 'xhs_quality_gate_v1' and deleted_at is null
  ) then
    insert into kb_rulebooks (
      domain_id, rule_code, rule_name, rule_type, applies_to, severity, priority, source_level,
      citation_title, citation_url, rule_text, rule_jsonb, status, source_type, source_ref, created_by
    ) values (
      d_id,
      'xhs_quality_gate_v1',
      '账号文案质量门（标题/正文/标签/CTA）',
      'quality_gate',
      array['copy', 'review'],
      'warn',
      40,
      'manual',
      '账号运行质量门',
      '',
      '标题长度、正文长度、段落数、关键词覆盖、话题标签和CTA必须满足质量门阈值，未通过不得进入自动发布。',
      jsonb_build_object(
        'title_min', 10,
        'title_max', 28,
        'body_min', 140,
        'body_max', 1200,
        'paragraph_min', 4,
        'hashtag_target', 3,
        'min_keyword_hits', 2,
        'pass_score', 72
      ),
      'active',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;

  if not exists (
    select 1 from kb_rulebooks
    where domain_id = d_id and lower(rule_code) = 'xhs_narrative_structure_v1' and deleted_at is null
  ) then
    insert into kb_rulebooks (
      domain_id, rule_code, rule_name, rule_type, applies_to, severity, priority, source_level,
      citation_title, citation_url, rule_text, rule_jsonb, status, source_type, source_ref, created_by
    ) values (
      d_id,
      'xhs_narrative_structure_v1',
      '正文叙事结构基线',
      'style',
      array['copy', 'review'],
      'info',
      70,
      'manual',
      '账号写作方法论',
      '',
      '正文采用“结论先行 -> 条件边界 -> 操作步骤 -> 风险提醒 -> 互动引导”五段结构，首段必须出现结论句。',
      jsonb_build_object(
        'structure', jsonb_build_array('结论先行', '条件边界', '操作步骤', '风险提醒', '互动引导'),
        'opening_rule', '首句必须给出结论，不得先讲背景',
        'cta_rule', '末段加入一个单问题CTA，便于评论区互动'
      ),
      'active',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;

  -- Playbook seeds (collect / analysis / copy / review).
  if not exists (
    select 1 from kb_playbooks
    where domain_id = d_id and lower(playbook_code) = 'collect_hotspot_case_v1' and version = 1 and deleted_at is null
  ) then
    insert into kb_playbooks (
      domain_id, playbook_code, playbook_name, stage, objective, applicability, input_contract,
      method_steps, output_contract, quality_checks, example_material, status, version,
      source_level, source_type, source_ref, created_by
    ) values (
      d_id,
      'collect_hotspot_case_v1',
      '热点案例采集法',
      'collect',
      '稳定获取可复用案例输入，供分析与改写使用',
      '小红书移民垂类，曝光优先',
      jsonb_build_object(
        'required', jsonb_build_array('query', 'platform'),
        'optional', jsonb_build_array('time_window', 'author_filter', 'limit')
      ),
      jsonb_build_array(
        '先按关键词采集近7天高互动内容（点赞/收藏/评论可见）',
        '按source_ref去重，保留正文可读且非广告内容',
        '写入cases并生成ingestion_logs，失败项写明原因',
        '达到当日最小样本后再进入分析阶段'
      ),
      jsonb_build_object(
        'entity', 'case',
        'minimum_fields', jsonb_build_array('title', 'content_or_url', 'source_ref')
      ),
      jsonb_build_object(
        'min_case_per_day', 3,
        'dedupe', 'source_ref+title',
        'reject_if', jsonb_build_array('empty_title', 'invalid_url_and_empty_content')
      ),
      jsonb_build_object(
        'notes', '不依赖曝光字段，优先采集可观测互动指标'
      ),
      'active',
      1,
      'manual',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;

  if not exists (
    select 1 from kb_playbooks
    where domain_id = d_id and lower(playbook_code) = 'analysis_hotpost_method_v1' and version = 1 and deleted_at is null
  ) then
    insert into kb_playbooks (
      domain_id, playbook_code, playbook_name, stage, objective, applicability, input_contract,
      method_steps, output_contract, quality_checks, example_material, status, version,
      source_level, source_type, source_ref, created_by
    ) values (
      d_id,
      'analysis_hotpost_method_v1',
      '爆款拆解分析法',
      'analysis',
      '把采集输入转成可执行写作策略',
      '有cases输入且需生成选题/改写策略',
      jsonb_build_object(
        'required', jsonb_build_array('cases[]'),
        'optional', jsonb_build_array('assets[]', 'user_needs[]')
      ),
      jsonb_build_array(
        '提取标题钩子、开头句型、结构节奏、CTA方式',
        '按互动指标估计信号强度，形成高/中/低优先级',
        '输出可复用方法卡（asset）和选题角度（topic）',
        '给出禁用模式（过度承诺/空泛鸡汤/无边界结论）'
      ),
      jsonb_build_object(
        'deliverables', jsonb_build_array('analysis_summary', 'asset_candidates', 'topic_angles')
      ),
      jsonb_build_object(
        'must_include', jsonb_build_array('证据引用', '风险提示', '可执行动作'),
        'evidence_min_count', 1
      ),
      jsonb_build_object('scoring', jsonb_build_object('likes', 0.35, 'collects', 0.35, 'comments_count', 0.3)),
      'active',
      1,
      'manual',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;

  if not exists (
    select 1 from kb_playbooks
    where domain_id = d_id and lower(playbook_code) = 'copy_xhs_fullpost_v1' and version = 1 and deleted_at is null
  ) then
    insert into kb_playbooks (
      domain_id, playbook_code, playbook_name, stage, objective, applicability, input_contract,
      method_steps, output_contract, quality_checks, example_material, status, version,
      source_level, source_type, source_ref, created_by
    ) values (
      d_id,
      'copy_xhs_fullpost_v1',
      '小红书完整帖写作法',
      'copy',
      '生成可直接发布的标题+正文+标签',
      '分析阶段已给出方法卡与选题后',
      jsonb_build_object(
        'required', jsonb_build_array('topic', 'analysis_summary'),
        'optional', jsonb_build_array('assets', 'account_profile')
      ),
      jsonb_build_array(
        '标题：18-22字，结论前置，禁止空泛口号',
        '正文：结论先行 -> 条件边界 -> 操作步骤 -> 风险提醒 -> CTA',
        '标签：3-6个，含1个主话题+1个人群话题+1个场景话题',
        '结尾加入单问题互动引导'
      ),
      jsonb_build_object(
        'fields', jsonb_build_array('title', 'body', 'hashtags', 'cta_question'),
        'body_format', 'markdown_like_plain_text'
      ),
      jsonb_build_object(
        'title_chars', jsonb_build_object('min', 10, 'max', 28),
        'body_chars', jsonb_build_object('min', 140, 'max', 1200),
        'paragraph_min', 4,
        'hashtag_min', 3
      ),
      jsonb_build_object(
        'title_patterns', jsonb_build_array('结论型', '反直觉型', '清单型')
      ),
      'active',
      1,
      'manual',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;

  if not exists (
    select 1 from kb_playbooks
    where domain_id = d_id and lower(playbook_code) = 'review_feedback_loop_v1' and version = 1 and deleted_at is null
  ) then
    insert into kb_playbooks (
      domain_id, playbook_code, playbook_name, stage, objective, applicability, input_contract,
      method_steps, output_contract, quality_checks, example_material, status, version,
      source_level, source_type, source_ref, created_by
    ) values (
      d_id,
      'review_feedback_loop_v1',
      '发布后复盘法',
      'review',
      '用真实反馈优化下一轮SOP和提示词',
      '帖子发布后1h/3h/24h回收指标',
      jsonb_build_object(
        'required', jsonb_build_array('published_task', 'metrics'),
        'optional', jsonb_build_array('peer_samples')
      ),
      jsonb_build_array(
        '先对比本号历史中位值，判断是否达标',
        '再对比同题材外部样本，找结构差异',
        '输出一条保留动作、一条调整动作、一条废弃动作',
        '写入reviews与memory_items并生成sop快照'
      ),
      jsonb_build_object(
        'deliverables', jsonb_build_array('review_summary', 'next_actions', 'sop_delta')
      ),
      jsonb_build_object(
        'require_evidence', true,
        'hard_metrics', jsonb_build_array('likes', 'collects', 'comments_count'),
        'soft_metrics', jsonb_build_array('impressions', 'views')
      ),
      jsonb_build_object('window_hours', jsonb_build_array(1, 3, 24)),
      'active',
      1,
      'manual',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;

  -- IO rule seeds (ingest + egress contracts).
  if not exists (
    select 1 from kb_io_rules
    where domain_id = d_id
      and lower(rule_code) = 'ingest_case_octopus_v1'
      and direction = 'ingest'
      and entity_type = 'case'
      and deleted_at is null
  ) then
    insert into kb_io_rules (
      domain_id, rule_code, direction, entity_type, target_agent,
      required_fields, field_mapping, validation_jsonb, dedupe_keys, quality_gate_jsonb, output_template,
      status, source_type, source_ref, created_by
    ) values (
      d_id,
      'ingest_case_octopus_v1',
      'ingest',
      'case',
      'collector_agent',
      '["title","source_ref"]'::jsonb,
      '{"likes":"metrics.likes","collects":"metrics.collects","comments_count":"metrics.comments_count"}'::jsonb,
      '{"title_min_chars":6,"allow_empty_url_if_content":true}'::jsonb,
      '["source_ref","title"]'::jsonb,
      '{"reject_on_empty_title":true,"reject_on_empty_url_and_content":true}'::jsonb,
      '{"table":"cases"}'::jsonb,
      'active',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;

  if not exists (
    select 1 from kb_io_rules
    where domain_id = d_id
      and lower(rule_code) = 'egress_analysis_bundle_v1'
      and direction = 'egress'
      and entity_type = 'case'
      and deleted_at is null
  ) then
    insert into kb_io_rules (
      domain_id, rule_code, direction, entity_type, target_agent,
      required_fields, field_mapping, validation_jsonb, dedupe_keys, quality_gate_jsonb, output_template,
      status, source_type, source_ref, created_by
    ) values (
      d_id,
      'egress_analysis_bundle_v1',
      'egress',
      'case',
      'analysis_agent',
      '["title","content","metrics"]'::jsonb,
      '{"hook":"hook","structure":"structure","analysis":"analysis"}'::jsonb,
      '{"minimum_bundle_size":3}'::jsonb,
      '["source_ref"]'::jsonb,
      '{"prefer_metrics":["likes","collects","comments_count"]}'::jsonb,
      '{"include_refs":true,"max_items":12}'::jsonb,
      'active',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;

  if not exists (
    select 1 from kb_io_rules
    where domain_id = d_id
      and lower(rule_code) = 'egress_copy_bundle_v1'
      and direction = 'egress'
      and entity_type = 'asset'
      and deleted_at is null
  ) then
    insert into kb_io_rules (
      domain_id, rule_code, direction, entity_type, target_agent,
      required_fields, field_mapping, validation_jsonb, dedupe_keys, quality_gate_jsonb, output_template,
      status, source_type, source_ref, created_by
    ) values (
      d_id,
      'egress_copy_bundle_v1',
      'egress',
      'asset',
      'copy_agent',
      '["type","content"]'::jsonb,
      '{"method_card":"type=method_card","opening_template":"type=opening_template","title_template":"type=title_template"}'::jsonb,
      '{"minimum_method_cards":1}'::jsonb,
      '["source_ref"]'::jsonb,
      '{"title_chars":[10,28],"body_chars":[140,1200],"hashtags_min":3}'::jsonb,
      '{"output":"title+body+hashtags+cta","must_explain_why":true}'::jsonb,
      'active',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;

  if not exists (
    select 1 from kb_io_rules
    where domain_id = d_id
      and lower(rule_code) = 'egress_review_bundle_v1'
      and direction = 'egress'
      and entity_type = 'review'
      and deleted_at is null
  ) then
    insert into kb_io_rules (
      domain_id, rule_code, direction, entity_type, target_agent,
      required_fields, field_mapping, validation_jsonb, dedupe_keys, quality_gate_jsonb, output_template,
      status, source_type, source_ref, created_by
    ) values (
      d_id,
      'egress_review_bundle_v1',
      'egress',
      'review',
      'review_agent',
      '["metrics","summary_text"]'::jsonb,
      '{"success":"success_points","failure":"failure_points","improvement":"improvement"}'::jsonb,
      '{"require_post_publish_metrics":true}'::jsonb,
      '["pipeline_task_id"]'::jsonb,
      '{"must_compare_history":true,"must_output_next_actions":true}'::jsonb,
      '{"write_to":["reviews","memory_items","sop_snapshots"]}'::jsonb,
      'active',
      'seed',
      'migration:019',
      'system:seed'
    );
  end if;
end $$;

do $$
declare
  tbl text;
  tables text[] := array['kb_rulebooks', 'kb_playbooks', 'kb_io_rules'];
begin
  foreach tbl in array tables loop
    if exists (
      select 1 from information_schema.tables
      where table_schema = 'public' and table_name = tbl
    ) then
      execute format('alter table %I enable row level security;', tbl);

      execute format('drop policy if exists %I on %I;', tbl || '_service_role_all', tbl);
      execute format('create policy %I on %I for all to service_role using (true) with check (true);', tbl || '_service_role_all', tbl);

      execute format('drop policy if exists %I on %I;', tbl || '_anon_deny_all', tbl);
      execute format('create policy %I on %I for all to anon using (false) with check (false);', tbl || '_anon_deny_all', tbl);
    end if;
  end loop;
end $$;
