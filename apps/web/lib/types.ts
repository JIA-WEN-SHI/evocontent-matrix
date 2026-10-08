export type TaskStatus =
  | "queued"
  | "intel_ready"
  | "drafting"
  | "pending_review"
  | "approved"
  | "review_rejected"
  | "publishing"
  | "published"
  | "publish_failed"
  | "metrics_ready"
  | "reflecting"
  | "reflection_failed"
  | "done";

export type PipelineStatus = TaskStatus;

export type PipelineChannel = "xiaohongshu" | "wechat_mp" | "douyin" | "video";

export type Task = {
  id: string;
  domain_id: string;
  channel: "xiaohongshu" | "wechat_mp";
  status: TaskStatus;
  title: string | null;
  body: string | null;
  utm_code: string | null;
  form_id: string | null;
  leads_count: number;
  meta_jsonb: Record<string, unknown>;
  published_at: string | null;
};

export type PipelineTask = {
  id: string;
  domain_id: string;
  account_id: string | null;
  channel: PipelineChannel;
  content_type: string;
  status: PipelineStatus;
  stage: string;
  intent_jsonb: Record<string, unknown>;
  payload_jsonb: Record<string, unknown>;
  review_jsonb: Record<string, unknown>;
  publish_jsonb: Record<string, unknown>;
  metrics_jsonb: Record<string, unknown>;
  scheduled_at: string | null;
  published_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type StrategyVersion = {
  id: string;
  domain_id: string;
  version: number;
  prompt_jsonb: Record<string, unknown>;
  reason: string;
  created_by: string;
  created_at: string;
};

export type Domain = {
  id: string;
  slug: string;
  name: string;
  config_jsonb: Record<string, unknown>;
  active_strategy_version_id: string | null;
};

export type AuditLog = {
  id: string;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  diff_jsonb: Record<string, unknown>;
  created_at: string;
};

export type DailyOpsReport = {
  id: string;
  domain_slug: string;
  run_key: string;
  attempt: number;
  triggered_by: string;
  status: "running" | "success" | "failed" | "skipped";
  started_at: string;
  finished_at: string | null;
  result_jsonb: Record<string, unknown>;
  error_message: string | null;
  created_at: string;
};

export type DailyOpsStatus = {
  status: string;
  flow: string;
  domain_slug: string;
  configured_runtime: {
    enabled: boolean;
    domain_slug: string;
    time: string;
    utc_offset: string;
    max_attempts: number;
    retry_backoff_seconds: number;
    viewpoint_enabled?: boolean;
  };
  latest_report: DailyOpsReport | null;
  latest_success_at: string | null;
  last_24h: {
    total: number;
    success: number;
    failed: number;
    skipped: number;
    running: number;
  };
  scheduler?: Record<string, unknown>;
};

export type SystemReadiness = {
  status: "ok" | "degraded";
  pipeline_mode: "native" | "schema_missing" | "unavailable";
  database?: { status: "ready" | "unavailable"; code?: string; message?: string };
  core_ready: boolean;
  core_tables: Record<string, boolean>;
  optional_tables: Record<string, boolean>;
  agent_health: {
    reachable: boolean;
    status: string;
  };
  suggestions: string[];
};

export type FeatureChecklistItem = {
  key: string;
  name: string;
  status: "ok" | "degraded" | "blocked" | "unverified";
  reason: string;
  action: string;
};

export type FeatureChecklist = {
  status: "ok" | "degraded";
  pipeline_mode: "native" | "schema_missing" | "unavailable";
  summary: {
    total: number;
    ok: number;
    degraded: number;
    blocked: number;
  };
  features: FeatureChecklistItem[];
  suggestions: string[];
};

export type OpsReferenceItem = {
  id: string;
  account_id: string | null;
  source_type: string;
  source_url: string | null;
  captured_at: string;
  raw_text: string;
  meta_jsonb: Record<string, unknown>;
};

export type OpsOverviewTask = {
  id: string;
  channel: PipelineChannel;
  status: PipelineStatus;
  stage: string;
  created_at: string;
  published_at: string | null;
  topic: string | null;
  title: string | null;
  body: string | null;
  image_prompt: string | null;
  analysis_jsonb: Record<string, unknown>;
};

export type OpsOverviewReport = {
  id: string;
  run_key: string;
  status: "running" | "success" | "failed" | "skipped";
  attempt: number;
  started_at: string;
  finished_at: string | null;
  error_message: string | null;
  result_jsonb: Record<string, unknown>;
};

export type OpsOverview = {
  status: string;
  account_id?: string | null;
  domain: {
    id: string;
    slug: string;
    name: string;
  };
  channel_stats: Record<
    string,
    {
      total: number;
      pending_review: number;
      published: number;
      done: number;
      failed: number;
      published_7d: number;
    }
  >;
  reference_items: OpsReferenceItem[];
  latest_tasks: OpsOverviewTask[];
  latest_reports: OpsOverviewReport[];
  analysis_logic: string[];
  strategy_snapshot: {
    focus_keywords: string[];
    hotspot_queries: string[];
    automation_policy: Record<string, unknown>;
  };
};

export type RunLedgerItem = {
  id: string;
  run_key: string;
  flow: "full" | "viewpoint" | string;
  run_date: string;
  status: string;
  target_posts_min: number;
  account_id: string | null;
  account_name: string;
  account_persona_name: string;
  primary_goal: string;
  topic: string;
  decisions_count: number;
  retro_report: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
};

export type MemoryItem = {
  id: string;
  domain_id: string;
  account_id: string | null;
  source_run_id: string | null;
  type: "brand_preference" | "strategy_rule" | "asset" | string;
  title: string;
  content: string;
  tags: string[];
  status: "active" | "pending" | "deprecated" | string;
  confidence: number;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type ChannelAccount = {
  id: string;
  channel: PipelineChannel;
  account_name: string;
  account_handle: string | null;
  login_mode: "storage_state" | "user_data_dir" | "cookies_json" | "credential";
  storage_state_path: string | null;
  user_data_dir: string | null;
  cookies_json: string | null;
  login_username: string | null;
  has_login_password: boolean;
  publish_selector: string;
  is_active: boolean;
  tags: string[];
  config_jsonb: Record<string, unknown>;
  notes: string | null;
  last_login_check_at: string | null;
  last_login_check_status: string | null;
  last_login_check_message: string | null;
  created_at: string;
  updated_at: string;
};

export type AccountLoginBootstrap = {
  status: string;
  account_id: string;
  login_mode: string;
  output_path: string | null;
  launched: boolean;
  pid: number | null;
  command: string[];
  next_step_hint: string;
};

export type ChannelAccountStrategyProfile = {
  persona_name: string;
  ip_positioning: string;
  tone_style: string;
  primary_goal: string;
  cta_style: string;
  audience: string[];
  pain_points: string[];
  content_pillars: string[];
  forbidden_claims: string[];
  publish_constraints?: string[];
  focus_keywords: string[];
  hotspot_queries: string[];
  viewpoint_queries: string[];
  mcp_mode: "home" | "keyword" | "hotspot" | "profile";
  mcp_query_default: string;
  mcp_limit_default: number;
  include_detail_metrics: boolean;
  mcp_feed_tool: string;
  mcp_search_tool: string;
  mcp_metrics_tool: string;
  mcp_profile_tool: string;
  mcp_profile_id_key: string;
  mcp_extra_args: Record<string, unknown>;
  prompt_overrides: Record<string, unknown>;
};

export type AccountCollectionPlanStep = {
  tool: string;
  limit: number;
  query?: string;
  profile_hint?: string;
  profile_id?: string;
  detail_url?: string;
  extra_args?: Record<string, unknown>;
  scope?: "global" | "account";
  reason?: string;
  fallback_to?: string | null;
  label?: string;
};

export type AccountCollectionPlan = {
  mode: "hybrid" | "mcp_only" | "playwright_only" | string;
  steps: AccountCollectionPlanStep[];
  fallback: AccountCollectionPlanStep[];
  notes: string;
  daily_schedule?: {
    posts_per_day: number;
    time_slots: string[];
    timezone: string;
  };
  loop_gate?: {
    min_case_per_day: number;
    min_asset_per_day: number;
  };
  topic_refresh?: {
    window_days: number;
    refresh_every_hours: number;
    source_priority: string[];
  };
};

export type AccountFeedbackPlan = {
  checkpoints_hours: number[];
  require_real_metrics_for_upgrade: boolean;
  synthetic_preview_enabled: boolean;
  auto_retro_after_last_checkpoint: boolean;
  exposure_gate?: {
    min_impressions: number;
    min_engagement_rate: number;
    review_window_hours: number;
  };
};

export type SopSnapshotRecord = {
  version: number;
  captured_at: string;
  reason: string;
  changed_fields: string[];
  actor: string;
  context: Record<string, unknown>;
  strategy_profile: Record<string, unknown>;
  collection_plan: Record<string, unknown>;
  feedback_plan: Record<string, unknown>;
};

export type SopCurrentView = {
  status: string;
  account_id: string;
  domain_slug: string;
  sop_latest?: SopSnapshotRecord | null;
  prompt_versions: Array<{
    id: string;
    agent_name: string;
    version: string;
    status: string;
    updated_at: string;
  }>;
};

export type SopSnapshotListResponse = {
  status: string;
  account_id: string;
  domain_slug: string;
  current_version: number;
  items: SopSnapshotRecord[];
};

export type AgentConfigNodeView = {
  agent_key: string;
  label: string;
  role_summary: string;
  model_name: string;
  temperature: number | null;
  prompt_source: string;
  prompt_version: string;
  prompt_preview: string;
  upstream_inputs: string[];
  downstream_outputs: string[];
  allowed_tools: string[];
  knowledge_sources: string[];
  notes: string[];
};

export type AgentConfigView = {
  status: string;
  account_id: string;
  domain_slug: string;
  primary_entry: string;
  coordinator: string;
  nodes: AgentConfigNodeView[];
};

export type LoopExecutionStepView = {
  step_key: string;
  label: string;
  status: string;
  summary: string;
  method_summary: string[];
  evidence_summary: string[];
  result_summary: string[];
};

export type LoopExecutionView = {
  status: string;
  account_id: string;
  domain_slug: string;
  current_stage: string;
  completion_score: number;
  steps: LoopExecutionStepView[];
  latest_summary: Record<string, unknown>;
};

export type ContentMethodSectionView = {
  section_key: string;
  title: string;
  summary: string;
  bullets: string[];
  evidence_sources: string[];
};

export type ContentMethodView = {
  status: string;
  account_id: string;
  domain_slug: string;
  sections: ContentMethodSectionView[];
};

export type ExecutionRouteStatusView = {
  status: string;
  account_id: string;
  domain_slug: string;
  primary_route: string;
  backup_route: string;
  readonly_bridge: string;
  switch_rules: string[];
  user_facing_status: string;
};

export type SopRollbackResponse = {
  status: string;
  account_id: string;
  domain_slug: string;
  active_version: number;
  sop_latest?: SopSnapshotRecord | null;
};

export type SubAgentRunRecord = {
  id: string;
  run_key: string;
  domain_id: string;
  domain_slug: string;
  account_id: string | null;
  actor: string;
  source: string;
  source_ref: string;
  action_type: string;
  idempotency_key: string;
  trace_id: string;
  status: string;
  retryable: boolean;
  error_code: string;
  error_message: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  input_jsonb: Record<string, unknown>;
  result_jsonb: Record<string, unknown>;
  policy_snapshot: Record<string, unknown>;
};

export type ExposureKpiGateResult = {
  status: string;
  passed: boolean;
  reason: string;
  metric_source: string;
  impressions: number;
  interactions: number;
  engagement_rate: number;
  thresholds: Record<string, unknown>;
};

export type AccountLoopStatus = {
  stage: string;
  onboarding_ready: boolean;
  blockers: string[];
  progress: Record<string, boolean>;
  collection_health: Record<string, unknown>;
  kpi_gate: ExposureKpiGateResult;
  latest: Record<string, unknown>;
  completion_score: number;
};

export type AccountLoopStatusResponse = {
  status: string;
  account_id: string;
  domain_slug: string;
  loop: AccountLoopStatus;
  sop_latest?: SopSnapshotRecord | null;
};

export type AccountOnboardingCommitResponse = {
  status: string;
  account_id: string;
  domain_slug: string;
  onboarding_ready: boolean;
  strategy_profile: Record<string, unknown>;
  collection_plan: Record<string, unknown>;
  feedback_plan: Record<string, unknown>;
  sop_snapshot?: SopSnapshotRecord | null;
};

export type AccountLoopRunDailyResponse = {
  status: string;
  account_id: string;
  domain_slug: string;
  gate_passed: boolean;
  blocked_reason: string;
  loop_report: Record<string, unknown>;
  result: Record<string, unknown>;
};

export type ReviewActionResponse = {
  status: string;
  account_id: string;
  pipeline_task_id: string;
  action: "approve" | "reject" | string;
  loop_stage: string;
  memory_item_id?: string | null;
  review_id?: string | null;
  pipeline_task: Record<string, unknown>;
};

export type AccountPromptVersion = {
  id: string;
  domain_id: string;
  account_id: string | null;
  agent_name: string;
  version: string;
  system_prompt: string;
  status: string;
  source: string;
  reason: string | null;
  rolled_back_from: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type AccountRuntimeContext = {
  status: string;
  account_id: string;
  account_name: string | null;
  channel: string | null;
  execution: {
    status: string;
    busy: boolean;
    current_action: string;
    current_task_id: string;
    last_error: string;
    last_used_at: string | null;
    browser_context_key: string;
    lock_key: string;
    [key: string]: unknown;
  };
  collection_plan: AccountCollectionPlan;
  latest: {
    task?: Record<string, unknown> | null;
    run?: Record<string, unknown> | null;
    strategy_profile?: Partial<ChannelAccountStrategyProfile>;
    [key: string]: unknown;
  };
};

export type PendingStrategyItem = {
  id: string;
  account_id: string | null;
  effective_account_id?: string | null;
  account_name: string;
  type: string;
  title: string;
  content: string;
  source: string;
  suggested_action: string;
  affected_agent: string;
  confidence: number;
  tags: string[];
  linked_prompt_version_candidates: AccountPromptVersion[];
  proposed_changes?: Array<{
    proposal_fingerprint?: string;
    target: "collection_plan" | "feedback_plan" | "publish_preferences" | string;
    label: string;
    reason: string;
    why: string;
    before_summary: string;
    after_summary: string;
    before?: Record<string, unknown>;
    after?: Record<string, unknown>;
    effect?: string;
  }>;
  created_at: string;
  updated_at: string;
};

export type ToolExecutionResult = {
  status: "ok" | "error";
  tool_name: string;
  account_id: string | null;
  scope: "account";
  collected_count?: number;
  items?: Array<Record<string, unknown>>;
  diagnostics?: string[];
  error?: string | null;
  next_suggested_tool?: string | null;
};

export type PublishFeedbackSummaryItem = {
  task_id: string;
  account_id: string | null;
  account_name: string;
  title: string;
  status: string;
  stage: string;
  feedback_state: string;
  metrics_mode: string;
  next_feedback_at: string | null;
  last_feedback_at: string | null;
  ces_score: number | null;
  primary_bottleneck: string;
  ee_mode: string;
  published_url: string | null;
  feed_id: string | null;
  published_at: string | null;
  updated_at: string;
  metrics_current?: Record<string, number>;
  metric_change?: Record<string, number>;
  snapshot_count?: number;
  historical_imported?: boolean;
  schedule_hours?: number[];
  completed_hours?: number[];
  checkpoints?: Array<{
    hour: number;
    due_at: string | null;
    status: "done" | "waiting" | "overdue";
    label: string;
  }>;
};

export type PublishIdentityImportItem = {
  task_id?: string;
  title?: string;
  published_url: string;
  feed_id?: string;
  xsec_token?: string;
  remote_post_id?: string;
};

export type PublishIdentityImportResult = {
  status: string;
  domain_slug: string;
  account_id: string;
  received: number;
  updated: number;
  matched_summary: Record<string, number>;
  updated_items: Array<{
    index: number;
    task_id: string;
    matched_by: string;
    feed_id: string;
    has_xsec_token: boolean;
    published_url: string;
  }>;
  skipped_items: Array<{
    index: number;
    task_id?: string;
    title?: string;
    reason: string;
  }>;
  reconcile: Record<string, unknown>;
};

export type PublishHistoryMetricSnapshot = {
  captured_at?: string;
  impressions?: number;
  views?: number;
  clicks?: number;
  ctr?: number;
  likes?: number;
  collects?: number;
  comments_count?: number;
  shares?: number;
  follows?: number;
  avg_read_seconds?: number;
  inquiries?: number;
};

export type PublishHistoryImportItem = {
  task_id?: string;
  title?: string;
  published_url?: string;
  feed_id?: string;
  xsec_token?: string;
  remote_post_id?: string;
  published_at?: string;
  metrics?: Record<string, unknown>;
  snapshots?: PublishHistoryMetricSnapshot[];
  tags?: string[];
  note?: string;
};

export type PublishHistoryImportResult = {
  status: string;
  domain_slug: string;
  account_id: string;
  received: number;
  imported: number;
  created_tasks: number;
  updated_tasks: number;
  memory_created: number;
  matched_summary: Record<string, number>;
  imported_items: Array<{
    index: number;
    task_id: string;
    matched_by: string;
    title: string;
    feed_id: string;
    snapshot_count: number;
    metric_keys: string[];
  }>;
  skipped_items: Array<{
    index: number;
    title?: string;
    reason: string;
  }>;
  reconcile: Record<string, unknown>;
};

export type TaskFeedbackDetail = {
  task_id: string;
  account_id: string | null;
  title: string;
  status: string;
  stage: string;
  feedback_state: string;
  schedule_hours: number[];
  completed_hours: number[];
  next_feedback_at: string | null;
  last_feedback_at: string | null;
  identity: Record<string, unknown>;
  feedback_analysis: Record<string, unknown>;
  metrics_jsonb: Record<string, unknown>;
  publish_jsonb: Record<string, unknown>;
  published_at: string | null;
  created_at: string | null;
  updated_at: string;
};

export type TaskPlanStep = {
  step: "collect" | "analyze" | "rebuild" | "draft" | "review" | "publish" | "feedback" | "strategy_confirm" | "prompt_upgrade";
  tool: string;
  scope: "global" | "account";
  input: Record<string, unknown>;
  reason: string;
  fallback: string | null;
  risk_level: "low" | "medium" | "high";
  index?: number;
};

export type TaskPlanPreview = {
  plan_id: string;
  account_id: string;
  domain_slug: string;
  created_by: "orchestrator";
  requires_confirmation: boolean;
  risk_level: "low" | "medium" | "high";
  status: "draft" | "approved" | "running" | "done" | "failed";
  steps: TaskPlanStep[];
};

export type RequiredConfirmation = {
  type: "confirm" | "second_confirm";
  action: string;
  risk_level: "medium" | "high";
  message?: string;
};

export type ExecutionEnvelope = {
  status: "ok" | "error";
  step: string;
  tool: string;
  account_id: string | null;
  state_patch: {
    status: string;
    stage: string;
    feedback_state?: string;
  };
  summary: string;
  diagnostics: string[];
  retry_action: string | null;
  fallback_action: string | null;
  error_code: string | null;
};

export type CoachEvidenceRef = {
  source_type: string;
  source_ref: string;
  timestamp: string;
};

export type CoachAction = {
  id: string;
  title: string;
  description: string;
  reason: string;
  acceptance: string;
  risk: string;
  evidence_tags: string[];
  evidence_refs: CoachEvidenceRef[];
  requires_confirmation: boolean;
  execution_supported: boolean;
  execution: Record<string, unknown>;
  state: "suggested" | "acknowledged" | "completed" | "failed" | "canceled";
};

export type CoachDecisionGate = {
  gate_type: "topic" | "review" | "retro" | string;
  message: string;
  status: "pending" | "completed" | string;
};

export type CoachBrief = {
  status: string;
  domain_slug: string;
  account_id: string;
  account_name: string;
  window_days: number;
  yesterday_summary: {
    date_local: string;
    impressions: number;
    interactions: number;
    engagement_rate: number;
    review_pass_rate: number;
    approved_count: number;
    rejected_count: number;
    top_fail_reasons: Array<{ reason: string; count: number }>;
  };
  today_actions: CoachAction[];
  decision_gates: CoachDecisionGate[];
  prompt_upgrade_suggestions: Array<{
    title: string;
    proposal: string;
    reason: string;
    evidence_tags: string[];
  }>;
  context_snapshot: {
    counts: Record<string, number>;
    intel_count: number;
    pipeline_count: number;
    kb_count: number;
  };
};

export type CoachChatResponse = {
  status: string;
  domain_slug: string;
  account_id: string;
  coach_reply: string;
  requires_confirmation: boolean;
  brief: CoachBrief;
  pending_actions: CoachAction[];
  decision_gates: CoachDecisionGate[];
  prompt_upgrade_suggestions: CoachBrief["prompt_upgrade_suggestions"];
  next_questions: string[];
  context_info?: {
    history_used_turns?: number;
    agent_topology?: string[];
  };
};

export type CoachConfirmActionResponse = {
  status: string;
  action_id?: string;
  state?: "completed" | "acknowledged" | "failed" | "canceled";
  message?: string;
  action?: CoachAction;
  result?: Record<string, unknown>;
  next_brief?: CoachBrief;
  error_code?: string;
};

export type IntelAnalysisResponse = {
  status: string;
  domain_slug: string;
  account_id?: string | null;
  analysis: {
    summary: Record<string, unknown>;
    source_distribution: Array<Record<string, unknown>>;
    top_queries: Array<Record<string, unknown>>;
    top_keywords: Array<Record<string, unknown>>;
    samples: string[];
    recommendations: string[];
  };
  ai_discussion_prompt: string;
};

export type OpsAutoConfigResponse = {
  status: string;
  domain_slug: string;
  account_id?: string | null;
  recommended_form: {
    channel: PipelineChannel;
    content_type: string;
    topic: string;
    track: string;
    audience_tag: string;
    publish_selector: string;
  };
  weights: Record<string, number>;
  evidence: {
    official_keywords: string[];
    top_xhs_queries: string[];
    top_weighted_keywords: Array<{
      keyword: string;
      score: number;
      sources: string[];
    }>;
    history_sample_titles: string[];
    intel_sample_count: number;
  };
  suggestions: string[];
  source_paths: string[];
};

export type RuntimeContext = {
  status: string;
  domain_slug: string;
  publish_runtime?: {
    mode: "demo" | "real";
    dry_run: boolean;
    ready_for_real_publish: boolean;
    blockers: string[];
    next_actions: string[];
  };
  publish_account: {
    configured: boolean;
    account_id: string | null;
    account_name: string | null;
    account_handle: string | null;
    login_mode: string | null;
    auth_hint: string;
    strategy_profile?: Partial<ChannelAccountStrategyProfile>;
    last_login_check_status: string | null;
    last_login_check_at: string | null;
    last_login_check_message: string | null;
  };
  crawler_account: {
    source: string;
    auth_hint: string;
    xhs_enabled: boolean;
    xhs_max_per_query: number;
    google_enabled: boolean;
    google_max_per_query: number;
  };
  latest: {
    last_xhs_intel_at: string | null;
    last_google_hotspot_at: string | null;
    last_google_viewpoint_at: string | null;
    last_draft_task_id: string | null;
    last_draft_task_status: string | null;
    last_published_task_id: string | null;
    last_published_at: string | null;
    last_task_account_id: string | null;
    last_task_account_name: string | null;
  };
};

export type McpReadonlyStatus = {
  status: string;
  enabled: boolean;
  base_url: string;
  search_tool: string;
  metrics_tool: string;
  timeout_sec: number;
  mode: string;
};

export type McpReadonlySearchResponse = {
  status: string;
  query: string;
  count: number;
  tool_name: string;
  reason?: string;
  attempts?: Array<Record<string, unknown>>;
  items: Array<{
    source_type: string;
    source_url: string;
    captured_at: string;
    raw_text: string;
    meta_jsonb: Record<string, unknown>;
  }>;
};

export type McpReadonlyCollectIntelResponse = {
  status: string;
  domain_slug: string;
  query: string;
  account_id?: string | null;
  requested_limit: number;
  collected: number;
  inserted: number;
  include_home: boolean;
  include_search: boolean;
  include_profile?: boolean;
  include_detail_metrics: boolean;
  tool_names: {
    feed: string;
    search: string;
    metrics: string;
    profile?: string;
  };
  metrics_sync: {
    attempted: number;
    success: number;
    failed: number;
  };
  diagnostics?: Array<Record<string, unknown>>;
  items: Array<{
    source_type: string;
    source_url: string;
    captured_at: string;
    raw_text: string;
    meta_jsonb: Record<string, unknown>;
  }>;
};

export type McpReadonlyCustomCallResponse = {
  status: string;
  tool_name?: string;
  count?: number;
  reason?: string;
  attempts?: Array<Record<string, unknown>>;
  items: Array<{
    source_type: string;
    source_url: string;
    captured_at: string;
    raw_text: string;
    meta_jsonb: Record<string, unknown>;
  }>;
  raw_result?: Record<string, unknown> | Array<Record<string, unknown>> | unknown;
};

export type McpReadonlySyncMetricsResponse = {
  status: string;
  domain_slug: string;
  checked: number;
  synced: number;
  failed: number;
  skipped: number;
  items: Record<string, unknown>;
};

export type HotPostLlmAnalyzeResponse = {
  status: string;
  process: string[];
  prompt_used: string;
  results: Array<{
    index: number;
    title: string;
    source_url: string;
    emotional_hook: string;
    core_pain_point: string;
    structure_pattern: string;
    extracted_keywords: string[];
  }>;
};

export type RebuildXhsContentResponse = {
  status: string;
  strategy: {
    core_argument: string;
    differentiated_angle: string;
    outline: {
      hook_intro: string;
      point1: string;
      point2: string;
      cta: string;
    };
  };
  copy: {
    candidate_titles: string[];
    body: string;
    tags: string[];
  };
  inputs: {
    ip_positioning: string;
    weekly_keywords: string[];
    tone_style: string;
    competitor_count: number;
  };
  process: string[];
  prompt_used: {
    strategy_prompt: string;
    copy_prompt: string;
  };
};

export type OrchestratorControl = {
  version: number;
  publish_method_order?: "playwright,mcp" | "mcp,playwright" | string;
  playwright_strict_account_scope?: boolean;
  nodes: {
    collect: { enabled: boolean };
    analyze: { enabled: boolean };
    rebuild: { enabled: boolean };
    publish: { enabled: boolean };
    reflect: { enabled: boolean };
  };
  prompt_overrides: {
    hot_post_analysis_prompt: string;
    rebuild_strategy_prompt: string;
    rebuild_copy_prompt: string;
  };
  data_input: {
    mcp_mode: "home" | "keyword" | "hotspot" | "profile";
    default_query: string;
    default_limit: number;
    include_detail_metrics: boolean;
    publish_method_order: "playwright,mcp" | "mcp,playwright" | string;
  };
  self_upgrade: {
    suggestions: Array<Record<string, unknown>>;
    last_suggested_at: string | null;
    last_applied_at: string | null;
    last_note: string;
  };
};

export type OrchestratorControlResponse = {
  status: string;
  domain_slug: string;
  control: OrchestratorControl;
  control_version?: string;
};

export type OrchestratorSelfUpgradeResponse = {
  status: string;
  domain_slug: string;
  applied: boolean;
  sample_summary: Record<string, unknown>;
  suggestions: Array<Record<string, unknown>>;
  applied_patch: Record<string, unknown>;
  control: OrchestratorControl;
  control_version?: string | null;
};

export type OrchestratorControlVersionItem = {
  version: string;
  created_at: string;
  reason: string;
  actor: string;
  control_jsonb: OrchestratorControl;
};

export type OrchestratorControlVersionsResponse = {
  status: string;
  domain_slug: string;
  active_version: string | null;
  items: OrchestratorControlVersionItem[];
};

export type OrchestratorControlRollbackResponse = {
  status: string;
  domain_slug: string;
  active_version: string;
  control: OrchestratorControl;
};

export type ChiefEvolutionCoreResult = {
  analysis_report: {
    competitor_insights: string;
    our_weakness: string;
    extracted_methodology: string;
  };
  action_decision: "UPGRADE" | "ROLLBACK" | "HOLD";
  version_control: {
    target_agent: "Agent 2" | "Agent 3" | "ALL";
    action_reason: string;
    new_system_prompt: string | null;
    rollback_target_version: string | null;
  };
  milestone_status: "NOT_REACHED" | "REACHED_STABLE_NODE";
};

export type ChiefEvolutionResponse = {
  status: string;
  domain_slug: string;
  inputs: {
    competitor_count: number;
    history_count: number;
    milestone_goal: string;
    account_id?: string | null;
  };
  result: ChiefEvolutionCoreResult;
  applied: Record<string, unknown>;
  control_version?: string | null;
};

export type CleanupDuplicateTasksResponse = {
  status: string;
  domain_slug: string;
  pipeline_mode: "native" | "schema_missing";
  dry_run: boolean;
  scanned: number;
  duplicate_groups: number;
  deleted: number;
  kept_ids: string[];
  deleted_ids: string[];
  groups: Array<{
    key: string;
    keep_id: string;
    drop_ids: string[];
  }>;
};

export type KbTopicStatus = "todo" | "drafted" | "produced" | "published" | "archived";
export type KbJobStatus = "queued" | "running" | "success" | "failed" | "canceled";
export type KbIngestionStatus = "received" | "processing" | "success" | "partial_failed" | "failed";
export type KbEntityType = "case" | "asset" | "user_need" | "topic" | "review";

export type KbCaseRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  platform: string;
  author: string;
  url: string;
  title: string;
  content: string;
  metrics: Record<string, unknown>;
  hook: string;
  structure: string;
  analysis: string;
  source_type: string;
  source_ref: string;
  created_by: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KbAssetRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  type: string;
  content: string;
  source: string;
  usable_scene: string;
  is_verified: boolean;
  summary: string;
  source_type: string;
  source_ref: string;
  created_by: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KbUserNeedRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  user_type: string;
  original_text: string;
  scenario: string;
  demand_type: string;
  emotion: string;
  real_problem: string;
  source_type: string;
  source_ref: string;
  created_by: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KbTopicRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  title: string;
  topic_description: string;
  target_user: string;
  platform: string;
  structure_type: string;
  status: KbTopicStatus | string;
  reason: string;
  source_type: string;
  source_ref: string;
  created_by: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KbReviewRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  topic_id: string | null;
  pipeline_task_id: string | null;
  content_item_ref: string;
  platform: string;
  metrics: Record<string, unknown>;
  success_points: string;
  failure_points: string;
  improvement: string;
  summary_text: string;
  source_type: string;
  source_ref: string;
  created_by: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KbTagRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  name: string;
  category: string;
  status: "active" | "disabled" | string;
  source_type: string;
  source_ref: string;
  created_by: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KbEntityTagRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  entity_type: KbEntityType | string;
  entity_id: string;
  tag_id: string;
  source_type: string;
  source_ref: string;
  created_by: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KbIngestionLogRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  source: string;
  source_run_id: string;
  entity_type: KbEntityType | string;
  status: KbIngestionStatus | string;
  request_payload: Record<string, unknown>;
  normalized_count: number;
  success_count: number;
  failed_count: number;
  error_message: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type KbAiJobRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  job_type: string;
  status: KbJobStatus | string;
  input_jsonb: Record<string, unknown>;
  prompt_version: string;
  model_name: string;
  output_jsonb: Record<string, unknown>;
  error_message: string | null;
  retries: number;
  started_at: string | null;
  finished_at: string | null;
  source_type: string;
  source_ref: string;
  created_by: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KbOctopusImportResult = {
  status: string;
  ingestion_log_id: string;
  entity_type: KbEntityType | string;
  received: number;
  success_count: number;
  failed_count: number;
  auto_assets_created_count: number;
  failed_items: Array<Record<string, unknown>>;
  inserted_ids: string[];
};

export type KbTopicRecommendResponse = {
  status: string;
  created_count: number;
  skipped_count: number;
  topics: KbTopicRecord[];
  source_summary: Record<string, unknown>;
};

export type KbRpaTaskTemplateRecord = {
  id: string;
  domain_id: string;
  task_type: string;
  entity_type: "case" | "asset" | "user_need" | "review" | string;
  octopus_flow_id: string;
  octopus_endpoint: string;
  instruction_schema: Record<string, unknown>;
  is_active: boolean;
  created_by: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KbRpaTaskRunRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  instruction_id: string;
  task_type: string;
  entity_type: "case" | "asset" | "user_need" | "review" | string;
  source_run_id: string;
  status: string;
  octopus_run_id: string;
  octopus_request: Record<string, unknown>;
  octopus_response: Record<string, unknown>;
  callback_payload: Record<string, unknown>;
  accepted_count: number;
  rejected_count: number;
  duplicate_count: number;
  retry_count: number;
  error_message: string | null;
  created_by: string;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type KbRpaRawPayloadRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  run_id: string;
  source_run_id: string;
  entity_type: "case" | "asset" | "user_need" | "review" | string;
  raw_ref: string;
  raw_item: Record<string, unknown>;
  status: "received" | "normalized" | "invalid" | string;
  reject_code: string;
  reject_reason: string;
  created_at: string;
};

export type KbRpaCleanResultRecord = {
  id: string;
  domain_id: string;
  account_id: string | null;
  run_id: string;
  raw_payload_id: string;
  entity_type: "case" | "asset" | "user_need" | "review" | string;
  normalized_item: Record<string, unknown>;
  accepted: boolean;
  duplicate: boolean;
  inserted_entity_id: string | null;
  reject_code: string;
  reject_reason: string;
  created_at: string;
  updated_at: string;
};

export type KbRpaReplayResponse = {
  status: string;
  run_id: string;
  replay_source_run_id: string;
  ingestion_log_id: string;
  entity_type: "case" | "asset" | "user_need" | "review" | string;
  received: number;
  success_count: number;
  failed_count: number;
  failed_items: Array<Record<string, unknown>>;
  inserted_ids: string[];
};
