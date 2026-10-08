from datetime import datetime
from typing import Any, Dict, List, Optional
from datetime import timedelta, timezone
import re
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator


class Actor(BaseModel):
    user_id: str
    role: str


class StrategyVersionCreateRequest(BaseModel):
    prompt_jsonb: Dict[str, Any]
    reason: str = Field(min_length=3, max_length=4000)


class LeadEventPayload(BaseModel):
    utm_code: str
    form_id: str
    channel: str
    event_time: Optional[datetime] = None
    contact_fields: Dict[str, Any] = Field(default_factory=dict)
    meta_jsonb: Dict[str, Any] = Field(default_factory=dict)


class AuditLogEntry(BaseModel):
    actor: str
    action: str
    target_type: str
    target_id: str
    diff_jsonb: Dict[str, Any]


class DomainRecord(BaseModel):
    id: str
    slug: str
    name: str
    config_jsonb: Dict[str, Any] = Field(default_factory=dict)
    active_strategy_version_id: Optional[str] = None


class DomainListResponse(BaseModel):
    items: List[DomainRecord]


class PipelineTaskCreateRequest(BaseModel):
    domain_slug: str = Field(min_length=2, max_length=120)
    channel: str = Field(default="xiaohongshu")
    content_type: str = Field(default="post", max_length=80)
    account_id: Optional[str] = Field(default=None, max_length=120)
    intent_jsonb: Dict[str, Any] = Field(default_factory=dict)
    payload_jsonb: Dict[str, Any] = Field(default_factory=dict)
    scheduled_at: Optional[datetime] = None


class PipelineTaskRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    channel: str
    content_type: str
    status: str
    stage: str
    intent_jsonb: Dict[str, Any] = Field(default_factory=dict)
    payload_jsonb: Dict[str, Any] = Field(default_factory=dict)
    review_jsonb: Dict[str, Any] = Field(default_factory=dict)
    publish_jsonb: Dict[str, Any] = Field(default_factory=dict)
    metrics_jsonb: Dict[str, Any] = Field(default_factory=dict)
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class PipelineTaskListResponse(BaseModel):
    items: List[PipelineTaskRecord]


class PipelineApproveRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    body: Optional[str] = Field(default=None, max_length=20000)


class PipelineTaskEditRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)

    @field_validator("title", "body")
    @classmethod
    def require_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("内容不能为空")
        return value


class PipelineRejectRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class OpsAnalysisFeedbackRequest(BaseModel):
    pipeline_task_id: str = Field(min_length=8, max_length=80)
    judgement: str = Field(min_length=2, max_length=4000)
    notes: Optional[str] = Field(default=None, max_length=4000)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    decision: Optional[str] = Field(default=None, max_length=120)


class ChannelAccountRecord(BaseModel):
    id: str
    channel: str
    account_name: str
    account_handle: Optional[str] = None
    login_mode: str
    storage_state_path: Optional[str] = None
    user_data_dir: Optional[str] = None
    cookies_json: Optional[str] = None
    login_username: Optional[str] = None
    has_login_password: bool = False
    publish_selector: str = ""
    is_active: bool = True
    tags: List[str] = Field(default_factory=list)
    config_jsonb: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None
    last_login_check_at: Optional[datetime] = None
    last_login_check_status: Optional[str] = None
    last_login_check_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ChannelAccountListResponse(BaseModel):
    items: List[ChannelAccountRecord]


class ChannelAccountCreateRequest(BaseModel):
    channel: str = Field(default="xiaohongshu")
    account_name: str = Field(min_length=1, max_length=120)
    account_handle: Optional[str] = Field(default=None, max_length=120)
    login_mode: str = Field(default="storage_state")
    storage_state_path: Optional[str] = Field(default=None, max_length=1000)
    user_data_dir: Optional[str] = Field(default=None, max_length=1000)
    cookies_json: Optional[str] = Field(default=None, max_length=200000)
    login_username: Optional[str] = Field(default=None, max_length=200)
    login_password: Optional[str] = Field(default=None, max_length=500)
    publish_selector: str = Field(default="", max_length=500)
    is_active: bool = True
    tags: List[str] = Field(default_factory=list)
    config_jsonb: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = Field(default=None, max_length=4000)


class ChannelAccountUpdateRequest(BaseModel):
    account_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    account_handle: Optional[str] = Field(default=None, max_length=120)
    login_mode: Optional[str] = Field(default=None)
    storage_state_path: Optional[str] = Field(default=None, max_length=1000)
    user_data_dir: Optional[str] = Field(default=None, max_length=1000)
    cookies_json: Optional[str] = Field(default=None, max_length=200000)
    login_username: Optional[str] = Field(default=None, max_length=200)
    login_password: Optional[str] = Field(default=None, max_length=500)
    publish_selector: Optional[str] = Field(default=None, max_length=500)
    is_active: Optional[bool] = None
    tags: Optional[List[str]] = None
    config_jsonb: Optional[Dict[str, Any]] = None
    notes: Optional[str] = Field(default=None, max_length=4000)


class ChannelAccountStrategyUpsertRequest(BaseModel):
    strategy_profile: Dict[str, Any] = Field(default_factory=dict)
    feedback_plan: Dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="update account strategy profile", min_length=2, max_length=4000)


class ChannelAccountCollectionPlanUpsertRequest(BaseModel):
    collection_plan: Dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="update collection plan", min_length=2, max_length=4000)


class AccountAuthResetResponse(BaseModel):
    status: str
    account_id: str
    cleared_fields: List[str] = Field(default_factory=list)
    next_step_hint: str


class AccountLoginBootstrapRequest(BaseModel):
    wait_seconds: int = Field(default=90, ge=15, le=600)
    url: Optional[str] = Field(default=None, max_length=1000)


class AccountLoginBootstrapResponse(BaseModel):
    status: str
    account_id: str
    login_mode: str
    output_path: Optional[str] = None
    launched: bool = False
    pid: Optional[int] = None
    command: List[str] = Field(default_factory=list)
    next_step_hint: str


class PromptVersionRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    agent_name: str
    version: str
    system_prompt: str
    status: str
    source: str
    reason: Optional[str] = None
    rolled_back_from: Optional[str] = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class PromptVersionListResponse(BaseModel):
    items: List[PromptVersionRecord]


class PromptVersionCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    agent_name: str = Field(min_length=2, max_length=120)
    version: str = Field(min_length=1, max_length=120)
    system_prompt: str = Field(min_length=1, max_length=100000)
    status: str = Field(default="draft", max_length=40)
    source: str = Field(default="manual", max_length=80)
    reason: str = Field(default="manual prompt update", min_length=2, max_length=4000)
    evidence_jsonb: Dict[str, Any] = Field(default_factory=dict)


class PromptVersionRollbackRequest(BaseModel):
    reason: str = Field(default="rollback prompt version", min_length=2, max_length=4000)


class PromptVersionActivateRequest(BaseModel):
    reason: str = Field(default="activate prompt version", min_length=2, max_length=4000)


class AccountRuntimeContextResponse(BaseModel):
    status: str
    account_id: str
    account_name: Optional[str] = None
    channel: Optional[str] = None
    execution: Dict[str, Any] = Field(default_factory=dict)
    collection_plan: Dict[str, Any] = Field(default_factory=dict)
    latest: Dict[str, Any] = Field(default_factory=dict)


class SopSnapshotRecord(BaseModel):
    version: int
    captured_at: str
    reason: str
    changed_fields: List[str] = Field(default_factory=list)
    actor: str = ""
    context: Dict[str, Any] = Field(default_factory=dict)
    strategy_profile: Dict[str, Any] = Field(default_factory=dict)
    collection_plan: Dict[str, Any] = Field(default_factory=dict)
    feedback_plan: Dict[str, Any] = Field(default_factory=dict)


class SopCurrentView(BaseModel):
    status: str
    account_id: str
    domain_slug: str
    sop_latest: Optional[SopSnapshotRecord] = None
    prompt_versions: List[Dict[str, Any]] = Field(default_factory=list)


class AgentConfigNodeView(BaseModel):
    agent_key: str
    label: str
    role_summary: str
    model_name: str = ""
    temperature: Optional[float] = None
    prompt_source: str = ""
    prompt_version: str = ""
    prompt_preview: str = ""
    upstream_inputs: List[str] = Field(default_factory=list)
    downstream_outputs: List[str] = Field(default_factory=list)
    allowed_tools: List[str] = Field(default_factory=list)
    knowledge_sources: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class AgentConfigView(BaseModel):
    status: str
    account_id: str
    domain_slug: str
    primary_entry: str
    coordinator: str
    nodes: List[AgentConfigNodeView] = Field(default_factory=list)


class LoopExecutionStepView(BaseModel):
    step_key: str
    label: str
    status: str
    summary: str = ""
    method_summary: List[str] = Field(default_factory=list)
    evidence_summary: List[str] = Field(default_factory=list)
    result_summary: List[str] = Field(default_factory=list)


class LoopExecutionView(BaseModel):
    status: str
    account_id: str
    domain_slug: str
    current_stage: str
    completion_score: float = 0
    steps: List[LoopExecutionStepView] = Field(default_factory=list)
    latest_summary: Dict[str, Any] = Field(default_factory=dict)


class ContentMethodSectionView(BaseModel):
    section_key: str
    title: str
    summary: str
    bullets: List[str] = Field(default_factory=list)
    evidence_sources: List[str] = Field(default_factory=list)


class ContentMethodView(BaseModel):
    status: str
    account_id: str
    domain_slug: str
    sections: List[ContentMethodSectionView] = Field(default_factory=list)


class ExecutionRouteStatusView(BaseModel):
    status: str
    account_id: str = ""
    domain_slug: str
    primary_route: str
    backup_route: str
    readonly_bridge: str
    switch_rules: List[str] = Field(default_factory=list)
    user_facing_status: str = ""


class SopSnapshotListResponse(BaseModel):
    status: str
    account_id: str
    domain_slug: str
    current_version: int = 0
    items: List[SopSnapshotRecord] = Field(default_factory=list)


class SopRollbackRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    version: int = Field(ge=1)
    reason: str = Field(default="manual sop rollback", min_length=2, max_length=500)


class SopRollbackResponse(BaseModel):
    status: str
    account_id: str
    domain_slug: str
    active_version: int
    sop_latest: Optional[SopSnapshotRecord] = None


class AccountOnboardingCommitRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    primary_goal: str = Field(min_length=1, max_length=200)
    persona_name: str = Field(min_length=1, max_length=120)
    ip_positioning: str = Field(min_length=1, max_length=500)
    tone_style: str = Field(default="", max_length=500)
    cta_style: str = Field(default="", max_length=500)
    audience: List[str] = Field(default_factory=list, max_length=20)
    pain_points: List[str] = Field(default_factory=list, max_length=20)
    content_pillars: List[str] = Field(default_factory=list, max_length=20)
    forbidden_claims: List[str] = Field(default_factory=list, max_length=30)
    focus_keywords: List[str] = Field(default_factory=list, max_length=40)
    hotspot_queries: List[str] = Field(default_factory=list, max_length=40)
    viewpoint_queries: List[str] = Field(default_factory=list, max_length=40)
    publish_constraints: List[str] = Field(default_factory=list, max_length=30)
    posts_per_day: int = Field(default=1, ge=1, le=10)
    publish_time_slots: List[str] = Field(default_factory=lambda: ["10:00", "20:00"], max_length=8)
    min_case_per_day: int = Field(default=3, ge=0, le=300)
    min_asset_per_day: int = Field(default=3, ge=0, le=300)
    checkpoints_hours: List[int] = Field(default_factory=lambda: [1, 3, 24], max_length=8)
    reason: str = Field(default="commit single account onboarding", min_length=2, max_length=4000)
    lock_onboarding: bool = True


class BrowserIntelItem(BaseModel):
    source_url: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=1, max_length=200)
    raw_text: str = Field(min_length=10, max_length=5000)
    query: str = Field(default="", max_length=300)
    observed_metrics: Dict[str, str] = Field(default_factory=dict, max_length=10)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("source_url")
    @classmethod
    def public_note_url(cls, value: str) -> str:
        url = urlsplit(value.strip())
        match = re.fullmatch(r"/(?:explore|search_result)/([0-9a-fA-F]{24})/?", url.path)
        if (url.scheme != "https" or url.hostname not in {"xiaohongshu.com", "www.xiaohongshu.com"}
                or url.username or url.password or url.port not in {None, 443} or not match):
            raise ValueError("来源必须是小红书公开笔记链接")
        return urlunsplit(("https", "www.xiaohongshu.com", f"/explore/{match.group(1).lower()}", "", ""))

    @field_validator("title", "raw_text")
    @classmethod
    def visible_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("页面内容不能为空")
        return value

    @field_validator("observed_metrics")
    @classmethod
    def visible_metrics(cls, values: Dict[str, str]) -> Dict[str, str]:
        allowed = {"likes", "collects", "comments_count", "views"}
        if any(key not in allowed or len(value) > 80 for key, value in values.items()):
            raise ValueError("只接受页面可见的点赞、收藏、评论和浏览数据")
        return values

    @field_validator("captured_at")
    @classmethod
    def observed_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("采集时间必须包含时区")
        if value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("采集时间不能在未来")
        return value.astimezone(timezone.utc)


class BrowserIntelImportRequest(BaseModel):
    domain_slug: str = Field(min_length=2, max_length=120)
    source_kind: str = Field(default="hotspot", pattern="^(hotspot|viewpoint)$")
    items: List[BrowserIntelItem] = Field(min_length=1, max_length=30)
    reason: str = Field(default="Chrome 页面协助采集", min_length=2, max_length=500)
    collection_method: str = Field(default="session_assisted", pattern="^(session_assisted|project_browser_agent|xiaohongshu_cli)$")
    model_name: str = Field(default="", max_length=120)


class AccountOnboardingCommitResponse(BaseModel):
    status: str
    account_id: str
    domain_slug: str
    onboarding_ready: bool
    strategy_profile: Dict[str, Any] = Field(default_factory=dict)
    collection_plan: Dict[str, Any] = Field(default_factory=dict)
    feedback_plan: Dict[str, Any] = Field(default_factory=dict)
    sop_snapshot: Optional[SopSnapshotRecord] = None


class ExposureKpiGateResult(BaseModel):
    status: str
    passed: bool
    reason: str = ""
    metric_source: str = ""
    impressions: float = 0
    interactions: float = 0
    engagement_rate: float = 0
    thresholds: Dict[str, Any] = Field(default_factory=dict)


class LoopStatus(BaseModel):
    stage: str
    onboarding_ready: bool
    blockers: List[str] = Field(default_factory=list)
    progress: Dict[str, bool] = Field(default_factory=dict)
    collection_health: Dict[str, Any] = Field(default_factory=dict)
    kpi_gate: ExposureKpiGateResult
    latest: Dict[str, Any] = Field(default_factory=dict)
    completion_score: float = 0


class AccountLoopStatusResponse(BaseModel):
    status: str
    account_id: str
    domain_slug: str
    loop: LoopStatus
    sop_latest: Optional[SopSnapshotRecord] = None


class SubAgentRunEnvelope(BaseModel):
    run_id: str
    source: str
    action_type: str
    status: str
    retryable: bool = False
    error_code: str = ""
    error_message: str = ""
    trace_id: str = ""
    account_id: Optional[str] = None
    created_at: Optional[str] = None


class AccountLoopRunDailyRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    flow: str = Field(default="full", max_length=40)
    run_key: str = Field(default="", max_length=200)
    force: bool = False


class AccountLoopRunDailyResponse(BaseModel):
    status: str
    account_id: str
    domain_slug: str
    gate_passed: bool
    blocked_reason: str = ""
    loop_report: Dict[str, Any] = Field(default_factory=dict)
    result: Dict[str, Any] = Field(default_factory=dict)


class ReviewActionRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    pipeline_task_id: str = Field(min_length=8, max_length=120)
    action: str = Field(default="approve", pattern="^(approve|reject)$")
    reason: str = Field(default="", max_length=2000)
    title: Optional[str] = Field(default=None, max_length=300)
    body: Optional[str] = Field(default=None, max_length=20000)
    write_memory: bool = True
    write_review: bool = True


class ReviewActionResponse(BaseModel):
    status: str
    account_id: str
    pipeline_task_id: str
    action: str
    loop_stage: str
    memory_item_id: Optional[str] = None
    review_id: Optional[str] = None
    pipeline_task: Dict[str, Any] = Field(default_factory=dict)


class AccountCollectionRunRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    source_kind: str = Field(default="hotspot", min_length=2, max_length=40)


class PlaywrightToolRunRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    tool_name: str = Field(min_length=1, max_length=120)
    params: Dict[str, Any] = Field(default_factory=dict)


class PublishFeedbackReconcileRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    limit: int = Field(default=20, ge=1, le=50)


class PublishIdentityImportItem(BaseModel):
    task_id: str = Field(default="", max_length=120)
    title: str = Field(default="", max_length=500)
    published_url: str = Field(default="", max_length=2000)
    feed_id: str = Field(default="", max_length=120)
    xsec_token: str = Field(default="", max_length=2000)
    remote_post_id: str = Field(default="", max_length=200)


class PublishIdentityImportRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(min_length=8, max_length=120)
    items: List[PublishIdentityImportItem] = Field(default_factory=list, max_length=100)
    limit_recent: int = Field(default=60, ge=1, le=300)
    trigger_reconcile: bool = True


class PublishHistoryMetricSnapshot(BaseModel):
    captured_at: str = Field(default="", max_length=80)
    impressions: Optional[float] = None
    views: Optional[float] = None
    clicks: Optional[float] = None
    ctr: Optional[float] = None
    likes: Optional[float] = None
    collects: Optional[float] = None
    comments_count: Optional[float] = None
    shares: Optional[float] = None
    follows: Optional[float] = None
    avg_read_seconds: Optional[float] = None
    inquiries: Optional[float] = None


class PublishHistoryImportItem(BaseModel):
    task_id: str = Field(default="", max_length=120)
    title: str = Field(default="", max_length=500)
    published_url: str = Field(default="", max_length=2000)
    feed_id: str = Field(default="", max_length=120)
    xsec_token: str = Field(default="", max_length=2000)
    remote_post_id: str = Field(default="", max_length=200)
    published_at: str = Field(default="", max_length=80)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    snapshots: List[PublishHistoryMetricSnapshot] = Field(default_factory=list, max_length=200)
    tags: List[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=2000)


class PublishHistoryImportRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(min_length=8, max_length=120)
    items: List[PublishHistoryImportItem] = Field(default_factory=list, max_length=300)
    trigger_reconcile: bool = True
    create_task_if_missing: bool = True
    write_pending_memory: bool = True
    write_global_memory: bool = False


class AssistantChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    auto_execute: bool = True
    conversation_history: List[Dict[str, str]] = Field(default_factory=list)


class CoachChatRequest(BaseModel):
    message: str = Field(default="", max_length=4000)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    conversation_history: List[Dict[str, str]] = Field(default_factory=list)


class CoachBriefRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)


class CoachConfirmActionRequest(BaseModel):
    action_id: str = Field(min_length=8, max_length=120)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    confirm: bool = True
    force_reexecute: bool = False


class TaskPlanConfirmRequest(BaseModel):
    task_plan: Dict[str, Any] = Field(default_factory=dict)
    confirm_text: str = Field(default="确认执行", max_length=120)
    second_confirm_text: str = Field(default="", max_length=120)
    actor: str = Field(default="api:assistant", max_length=160)


class TaskPlanRunRequest(BaseModel):
    task_plan: Dict[str, Any] = Field(default_factory=dict)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    triggered_by: str = Field(default="api:task_plan", max_length=160)


class CrawlerTemplateUpsertRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    crawler_template: Dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="update crawler template", min_length=2, max_length=4000)


class IntelAnalyzeRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    limit: int = Field(default=30, ge=5, le=200)
    custom_logic: str = Field(default="", max_length=4000)


class OpsAutoConfigRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    channel: str = Field(default="xiaohongshu", min_length=2, max_length=80)
    limit: int = Field(default=50, ge=10, le=200)
    custom_logic: str = Field(default="", max_length=4000)


class McpReadonlySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=8, ge=1, le=50)
    source_kind: str = Field(default="hotspot", min_length=2, max_length=40)


class McpReadonlySyncMetricsRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    limit: int = Field(default=10, ge=1, le=50)


class McpReadonlyCollectIntelRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    query: str = Field(default="japan immigration", min_length=1, max_length=300)
    account_id: str = Field(default="", max_length=120)
    limit: int = Field(default=12, ge=1, le=100)
    include_home: bool = True
    include_search: bool = True
    include_detail_metrics: bool = False
    persist: bool = True
    tool_profile: Dict[str, Any] = Field(default_factory=dict)


class McpReadonlyCustomCallRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=120)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    source_kind: str = Field(default="hotspot", min_length=2, max_length=40)
    limit: int = Field(default=20, ge=1, le=100)


class McpWriteCustomCallRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=120)
    arguments: Dict[str, Any] = Field(default_factory=dict)


class McpWritePublishRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=20000)
    images: List[str] = Field(default_factory=list, max_length=20)
    tags: List[str] = Field(default_factory=list, max_length=20)
    schedule_at: str = Field(default="", max_length=80)
    video: str = Field(default="", max_length=1000)


class HotPostAnalyzeItem(BaseModel):
    title: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=20000)
    source_url: str = Field(default="", max_length=1000)


class HotPostAnalyzeRequest(BaseModel):
    items: List[HotPostAnalyzeItem] = Field(default_factory=list, max_length=20)
    analysis_prompt: str = Field(default="", max_length=12000)
    account_id: str = Field(default="", max_length=120)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)


class RebuildXhsContentRequest(BaseModel):
    competitor_analysis: List[Dict[str, Any]] = Field(default_factory=list, max_length=30)
    ip_positioning: str = Field(default="", max_length=3000)
    weekly_keywords: List[str] = Field(default_factory=list, max_length=20)
    tone_style: str = Field(default="", max_length=800)
    strategy_prompt: str = Field(default="", max_length=20000)
    copy_prompt: str = Field(default="", max_length=20000)
    account_id: str = Field(default="", max_length=120)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)


class OrchestratorControlUpsertRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    control_jsonb: Dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="update orchestrator control", min_length=2, max_length=4000)


class OrchestratorSelfUpgradeRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    apply: bool = False
    note: str = Field(default="", max_length=2000)


class OrchestratorControlRollbackRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    version: str = Field(min_length=3, max_length=40)
    reason: str = Field(default="manual rollback orchestrator control", min_length=2, max_length=4000)


class ChiefEvolutionRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    milestone_goal: str = Field(default="", max_length=2000)
    competitor_limit: int = Field(default=60, ge=10, le=300)
    performance_limit: int = Field(default=60, ge=10, le=300)
    apply: bool = False


class MemoryItemStatusUpdateRequest(BaseModel):
    status: str = Field(default="active", max_length=40)


class PendingStrategyApplyRequest(BaseModel):
    proposal_fingerprint: str = Field(default='', max_length=64)
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    target: str = Field(default="collection_plan", min_length=2, max_length=80)
    reason: str = Field(default="confirm pending strategy change from ui", min_length=2, max_length=4000)


class KbCaseRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    platform: str
    author: str
    url: str
    title: str
    content: str
    metrics: Dict[str, Any] = Field(default_factory=dict)
    hook: str = ""
    structure: str = ""
    analysis: str = ""
    source_type: str = "manual"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbAssetRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    type: str
    content: str
    source: str
    usable_scene: str
    is_verified: bool
    summary: str
    source_type: str = "manual"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbUserNeedRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    user_type: str
    original_text: str
    scenario: str
    demand_type: str
    emotion: str
    real_problem: str
    source_type: str = "manual"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbTopicRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    title: str
    topic_description: str
    target_user: str
    platform: str
    structure_type: str
    status: str
    reason: str
    source_type: str = "manual"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbReviewRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    topic_id: Optional[str] = None
    pipeline_task_id: Optional[str] = None
    content_item_ref: str
    platform: str
    metrics: Dict[str, Any] = Field(default_factory=dict)
    success_points: str
    failure_points: str
    improvement: str
    summary_text: str
    source_type: str = "manual"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbTagRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    name: str
    category: str
    status: str
    source_type: str = "manual"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbEntityTagRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    entity_type: str
    entity_id: str
    tag_id: str
    source_type: str = "manual"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbIngestionLogRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    source: str
    source_run_id: str
    entity_type: str
    status: str
    request_payload: Dict[str, Any] = Field(default_factory=dict)
    normalized_count: int
    success_count: int
    failed_count: int
    error_message: Optional[str] = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class KbAiJobRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    job_type: str
    status: str
    input_jsonb: Dict[str, Any] = Field(default_factory=dict)
    prompt_version: str = ""
    model_name: str = ""
    output_jsonb: Dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None
    retries: int = 0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    source_type: str = "system"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbRulebookRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    rule_code: str
    rule_name: str
    rule_type: str
    applies_to: List[str] = Field(default_factory=list)
    severity: str
    priority: int
    source_level: str
    effective_at: Optional[datetime] = None
    citation_title: str = ""
    citation_url: str = ""
    rule_text: str = ""
    rule_jsonb: Dict[str, Any] = Field(default_factory=dict)
    status: str
    source_type: str = "manual"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbPlaybookRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    playbook_code: str
    playbook_name: str
    stage: str
    objective: str = ""
    applicability: str = ""
    input_contract: Dict[str, Any] = Field(default_factory=dict)
    method_steps: List[Any] = Field(default_factory=list)
    output_contract: Dict[str, Any] = Field(default_factory=dict)
    quality_checks: Dict[str, Any] = Field(default_factory=dict)
    example_material: Dict[str, Any] = Field(default_factory=dict)
    status: str
    version: int
    source_level: str
    source_type: str = "manual"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbIoRuleRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    rule_code: str
    direction: str
    entity_type: str
    target_agent: str = ""
    required_fields: List[Any] = Field(default_factory=list)
    field_mapping: Dict[str, Any] = Field(default_factory=dict)
    validation_jsonb: Dict[str, Any] = Field(default_factory=dict)
    dedupe_keys: List[Any] = Field(default_factory=list)
    quality_gate_jsonb: Dict[str, Any] = Field(default_factory=dict)
    output_template: Dict[str, Any] = Field(default_factory=dict)
    status: str
    source_type: str = "manual"
    source_ref: str = ""
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbCaseListResponse(BaseModel):
    items: List[KbCaseRecord]


class KbAssetListResponse(BaseModel):
    items: List[KbAssetRecord]


class KbUserNeedListResponse(BaseModel):
    items: List[KbUserNeedRecord]


class KbTopicListResponse(BaseModel):
    items: List[KbTopicRecord]


class KbReviewListResponse(BaseModel):
    items: List[KbReviewRecord]


class KbTagListResponse(BaseModel):
    items: List[KbTagRecord]


class KbEntityTagListResponse(BaseModel):
    items: List[KbEntityTagRecord]


class KbIngestionLogListResponse(BaseModel):
    items: List[KbIngestionLogRecord]


class KbAiJobListResponse(BaseModel):
    items: List[KbAiJobRecord]


class KbRulebookListResponse(BaseModel):
    items: List[KbRulebookRecord]


class KbPlaybookListResponse(BaseModel):
    items: List[KbPlaybookRecord]


class KbIoRuleListResponse(BaseModel):
    items: List[KbIoRuleRecord]


class KbCaseCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    platform: str = Field(default="xiaohongshu", max_length=80)
    author: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=2000)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(default="", max_length=20000)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    hook: str = Field(default="", max_length=1000)
    structure: str = Field(default="", max_length=2000)
    analysis: str = Field(default="", max_length=10000)
    source_type: str = Field(default="manual", max_length=80)
    source_ref: str = Field(default="", max_length=1000)


class KbCasePatchRequest(BaseModel):
    account_id: Optional[str] = Field(default=None, max_length=120)
    platform: Optional[str] = Field(default=None, max_length=80)
    author: Optional[str] = Field(default=None, max_length=200)
    url: Optional[str] = Field(default=None, max_length=2000)
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    content: Optional[str] = Field(default=None, max_length=20000)
    metrics: Optional[Dict[str, Any]] = None
    hook: Optional[str] = Field(default=None, max_length=1000)
    structure: Optional[str] = Field(default=None, max_length=2000)
    analysis: Optional[str] = Field(default=None, max_length=10000)
    source_type: Optional[str] = Field(default=None, max_length=80)
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    deleted: Optional[bool] = None


class KbAssetCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    type: str = Field(default="insight", max_length=80)
    content: str = Field(min_length=1, max_length=20000)
    source: str = Field(default="", max_length=1000)
    usable_scene: str = Field(default="", max_length=1000)
    is_verified: bool = False
    summary: str = Field(default="", max_length=5000)
    source_type: str = Field(default="manual", max_length=80)
    source_ref: str = Field(default="", max_length=1000)


class KbAssetPatchRequest(BaseModel):
    account_id: Optional[str] = Field(default=None, max_length=120)
    type: Optional[str] = Field(default=None, max_length=80)
    content: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    source: Optional[str] = Field(default=None, max_length=1000)
    usable_scene: Optional[str] = Field(default=None, max_length=1000)
    is_verified: Optional[bool] = None
    summary: Optional[str] = Field(default=None, max_length=5000)
    source_type: Optional[str] = Field(default=None, max_length=80)
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    deleted: Optional[bool] = None


class KbUserNeedCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    user_type: str = Field(default="", max_length=120)
    original_text: str = Field(min_length=1, max_length=20000)
    scenario: str = Field(default="", max_length=1000)
    demand_type: str = Field(default="", max_length=120)
    emotion: str = Field(default="", max_length=120)
    real_problem: str = Field(default="", max_length=2000)
    source_type: str = Field(default="manual", max_length=80)
    source_ref: str = Field(default="", max_length=1000)


class KbUserNeedPatchRequest(BaseModel):
    account_id: Optional[str] = Field(default=None, max_length=120)
    user_type: Optional[str] = Field(default=None, max_length=120)
    original_text: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    scenario: Optional[str] = Field(default=None, max_length=1000)
    demand_type: Optional[str] = Field(default=None, max_length=120)
    emotion: Optional[str] = Field(default=None, max_length=120)
    real_problem: Optional[str] = Field(default=None, max_length=2000)
    source_type: Optional[str] = Field(default=None, max_length=80)
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    deleted: Optional[bool] = None


class KbTopicCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    title: str = Field(min_length=1, max_length=500)
    topic_description: str = Field(default="", max_length=10000)
    target_user: str = Field(default="", max_length=1000)
    platform: str = Field(default="xiaohongshu", max_length=80)
    structure_type: str = Field(default="", max_length=120)
    status: str = Field(default="todo", max_length=40)
    reason: str = Field(default="", max_length=2000)
    source_type: str = Field(default="manual", max_length=80)
    source_ref: str = Field(default="", max_length=1000)


class KbTopicPatchRequest(BaseModel):
    account_id: Optional[str] = Field(default=None, max_length=120)
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    topic_description: Optional[str] = Field(default=None, max_length=10000)
    target_user: Optional[str] = Field(default=None, max_length=1000)
    platform: Optional[str] = Field(default=None, max_length=80)
    structure_type: Optional[str] = Field(default=None, max_length=120)
    status: Optional[str] = Field(default=None, max_length=40)
    reason: Optional[str] = Field(default=None, max_length=2000)
    source_type: Optional[str] = Field(default=None, max_length=80)
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    deleted: Optional[bool] = None


class KbReviewCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    topic_id: str = Field(default="", max_length=120)
    pipeline_task_id: str = Field(default="", max_length=120)
    content_item_ref: str = Field(default="", max_length=2000)
    platform: str = Field(default="xiaohongshu", max_length=80)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    success_points: str = Field(default="", max_length=10000)
    failure_points: str = Field(default="", max_length=10000)
    improvement: str = Field(default="", max_length=10000)
    summary_text: str = Field(default="", max_length=5000)
    source_type: str = Field(default="manual", max_length=80)
    source_ref: str = Field(default="", max_length=1000)


class KbReviewPatchRequest(BaseModel):
    account_id: Optional[str] = Field(default=None, max_length=120)
    topic_id: Optional[str] = Field(default=None, max_length=120)
    pipeline_task_id: Optional[str] = Field(default=None, max_length=120)
    content_item_ref: Optional[str] = Field(default=None, max_length=2000)
    platform: Optional[str] = Field(default=None, max_length=80)
    metrics: Optional[Dict[str, Any]] = None
    success_points: Optional[str] = Field(default=None, max_length=10000)
    failure_points: Optional[str] = Field(default=None, max_length=10000)
    improvement: Optional[str] = Field(default=None, max_length=10000)
    summary_text: Optional[str] = Field(default=None, max_length=5000)
    source_type: Optional[str] = Field(default=None, max_length=80)
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    deleted: Optional[bool] = None


class KbTagCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default="general", min_length=1, max_length=120)
    status: str = Field(default="active", max_length=40)
    source_type: str = Field(default="manual", max_length=80)
    source_ref: str = Field(default="", max_length=1000)


class KbTagPatchRequest(BaseModel):
    account_id: Optional[str] = Field(default=None, max_length=120)
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    category: Optional[str] = Field(default=None, min_length=1, max_length=120)
    status: Optional[str] = Field(default=None, max_length=40)
    source_type: Optional[str] = Field(default=None, max_length=80)
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    deleted: Optional[bool] = None


class KbEntityTagCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    entity_type: str = Field(min_length=2, max_length=40)
    entity_id: str = Field(min_length=8, max_length=120)
    tag_id: str = Field(min_length=8, max_length=120)
    source_type: str = Field(default="manual", max_length=80)
    source_ref: str = Field(default="", max_length=1000)


class KbEntityTagPatchRequest(BaseModel):
    tag_id: Optional[str] = Field(default=None, min_length=8, max_length=120)
    source_type: Optional[str] = Field(default=None, max_length=80)
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    deleted: Optional[bool] = None


class KbRulebookCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    rule_code: str = Field(min_length=2, max_length=120)
    rule_name: str = Field(default="", max_length=300)
    rule_type: str = Field(default="platform_policy", max_length=40)
    applies_to: List[str] = Field(default_factory=list, max_length=20)
    severity: str = Field(default="warn", max_length=20)
    priority: int = Field(default=100, ge=1, le=1000)
    source_level: str = Field(default="manual", max_length=40)
    effective_at: Optional[datetime] = None
    citation_title: str = Field(default="", max_length=300)
    citation_url: str = Field(default="", max_length=2000)
    rule_text: str = Field(default="", max_length=12000)
    rule_jsonb: Dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="active", max_length=20)
    source_type: str = Field(default="manual", max_length=80)
    source_ref: str = Field(default="", max_length=1000)


class KbRulebookPatchRequest(BaseModel):
    account_id: Optional[str] = Field(default=None, max_length=120)
    rule_name: Optional[str] = Field(default=None, max_length=300)
    rule_type: Optional[str] = Field(default=None, max_length=40)
    applies_to: Optional[List[str]] = Field(default=None, max_length=20)
    severity: Optional[str] = Field(default=None, max_length=20)
    priority: Optional[int] = Field(default=None, ge=1, le=1000)
    source_level: Optional[str] = Field(default=None, max_length=40)
    effective_at: Optional[datetime] = None
    citation_title: Optional[str] = Field(default=None, max_length=300)
    citation_url: Optional[str] = Field(default=None, max_length=2000)
    rule_text: Optional[str] = Field(default=None, max_length=12000)
    rule_jsonb: Optional[Dict[str, Any]] = None
    status: Optional[str] = Field(default=None, max_length=20)
    source_type: Optional[str] = Field(default=None, max_length=80)
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    deleted: Optional[bool] = None


class KbPlaybookCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    playbook_code: str = Field(min_length=2, max_length=120)
    playbook_name: str = Field(default="", max_length=300)
    stage: str = Field(default="analysis", max_length=40)
    objective: str = Field(default="", max_length=4000)
    applicability: str = Field(default="", max_length=2000)
    input_contract: Dict[str, Any] = Field(default_factory=dict)
    method_steps: List[Any] = Field(default_factory=list, max_length=100)
    output_contract: Dict[str, Any] = Field(default_factory=dict)
    quality_checks: Dict[str, Any] = Field(default_factory=dict)
    example_material: Dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="active", max_length=20)
    version: int = Field(default=1, ge=1, le=999)
    source_level: str = Field(default="manual", max_length=40)
    source_type: str = Field(default="manual", max_length=80)
    source_ref: str = Field(default="", max_length=1000)


class KbPlaybookPatchRequest(BaseModel):
    account_id: Optional[str] = Field(default=None, max_length=120)
    playbook_name: Optional[str] = Field(default=None, max_length=300)
    stage: Optional[str] = Field(default=None, max_length=40)
    objective: Optional[str] = Field(default=None, max_length=4000)
    applicability: Optional[str] = Field(default=None, max_length=2000)
    input_contract: Optional[Dict[str, Any]] = None
    method_steps: Optional[List[Any]] = Field(default=None, max_length=100)
    output_contract: Optional[Dict[str, Any]] = None
    quality_checks: Optional[Dict[str, Any]] = None
    example_material: Optional[Dict[str, Any]] = None
    status: Optional[str] = Field(default=None, max_length=20)
    version: Optional[int] = Field(default=None, ge=1, le=999)
    source_level: Optional[str] = Field(default=None, max_length=40)
    source_type: Optional[str] = Field(default=None, max_length=80)
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    deleted: Optional[bool] = None


class KbIoRuleCreateRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    rule_code: str = Field(min_length=2, max_length=120)
    direction: str = Field(default="ingest", max_length=20)
    entity_type: str = Field(default="case", max_length=40)
    target_agent: str = Field(default="", max_length=120)
    required_fields: List[Any] = Field(default_factory=list, max_length=50)
    field_mapping: Dict[str, Any] = Field(default_factory=dict)
    validation_jsonb: Dict[str, Any] = Field(default_factory=dict)
    dedupe_keys: List[Any] = Field(default_factory=list, max_length=30)
    quality_gate_jsonb: Dict[str, Any] = Field(default_factory=dict)
    output_template: Dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="active", max_length=20)
    source_type: str = Field(default="manual", max_length=80)
    source_ref: str = Field(default="", max_length=1000)


class KbIoRulePatchRequest(BaseModel):
    account_id: Optional[str] = Field(default=None, max_length=120)
    target_agent: Optional[str] = Field(default=None, max_length=120)
    required_fields: Optional[List[Any]] = Field(default=None, max_length=50)
    field_mapping: Optional[Dict[str, Any]] = None
    validation_jsonb: Optional[Dict[str, Any]] = None
    dedupe_keys: Optional[List[Any]] = Field(default=None, max_length=30)
    quality_gate_jsonb: Optional[Dict[str, Any]] = None
    output_template: Optional[Dict[str, Any]] = None
    status: Optional[str] = Field(default=None, max_length=20)
    source_type: Optional[str] = Field(default=None, max_length=80)
    source_ref: Optional[str] = Field(default=None, max_length=1000)
    deleted: Optional[bool] = None


class KbOctopusImportItem(BaseModel):
    title: str = Field(default="", max_length=500)
    content: str = Field(default="", max_length=20000)
    url: str = Field(default="", max_length=2000)
    author: str = Field(default="", max_length=200)
    platform: str = Field(default="xiaohongshu", max_length=80)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list, max_length=30)
    captured_at: str = Field(default="", max_length=80)
    raw: Dict[str, Any] = Field(default_factory=dict)
    source_ref: str = Field(default="", max_length=1000)


class KbOctopusImportRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    source: str = Field(default="octopus", min_length=2, max_length=80)
    source_run_id: str = Field(default="", max_length=120)
    entity_type: str = Field(default="case", min_length=2, max_length=40)
    items: List[KbOctopusImportItem] = Field(default_factory=list, max_length=500)


class KbOctopusImportResult(BaseModel):
    status: str
    ingestion_log_id: str
    entity_type: str
    received: int
    success_count: int
    failed_count: int
    auto_assets_created_count: int = 0
    failed_items: List[Dict[str, Any]] = Field(default_factory=list)
    inserted_ids: List[str] = Field(default_factory=list)


class KbTopicRecommendRequest(BaseModel):
    domain_slug: str = Field(default="japan_immigration", min_length=2, max_length=120)
    account_id: str = Field(default="", max_length=120)
    case_ids: List[str] = Field(default_factory=list, max_length=100)
    asset_ids: List[str] = Field(default_factory=list, max_length=100)
    need_ids: List[str] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=10, ge=1, le=30)
    platform: str = Field(default="xiaohongshu", max_length=80)


class KbTopicRecommendResponse(BaseModel):
    status: str
    created_count: int
    skipped_count: int
    topics: List[KbTopicRecord] = Field(default_factory=list)
    source_summary: Dict[str, Any] = Field(default_factory=dict)


class KbRpaTaskTemplateRecord(BaseModel):
    id: str
    domain_id: str
    task_type: str
    entity_type: str
    octopus_flow_id: str
    octopus_endpoint: str
    instruction_schema: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbRpaTaskRunRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    instruction_id: str
    task_type: str
    entity_type: str
    source_run_id: str
    status: str
    octopus_run_id: str
    octopus_request: Dict[str, Any] = Field(default_factory=dict)
    octopus_response: Dict[str, Any] = Field(default_factory=dict)
    callback_payload: Dict[str, Any] = Field(default_factory=dict)
    accepted_count: int = 0
    rejected_count: int = 0
    duplicate_count: int = 0
    retry_count: int = 0
    error_message: Optional[str] = None
    created_by: str
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class KbRpaRawPayloadRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    run_id: str
    source_run_id: str
    entity_type: str
    raw_ref: str
    raw_item: Dict[str, Any] = Field(default_factory=dict)
    status: str
    reject_code: str = ""
    reject_reason: str = ""
    created_at: datetime


class KbRpaCleanResultRecord(BaseModel):
    id: str
    domain_id: str
    account_id: Optional[str] = None
    run_id: str
    raw_payload_id: str
    entity_type: str
    normalized_item: Dict[str, Any] = Field(default_factory=dict)
    accepted: bool
    duplicate: bool
    inserted_entity_id: Optional[str] = None
    reject_code: str = ""
    reject_reason: str = ""
    created_at: datetime
    updated_at: datetime


class KbRpaTaskTemplateListResponse(BaseModel):
    items: List[KbRpaTaskTemplateRecord]


class KbRpaTaskRunListResponse(BaseModel):
    items: List[KbRpaTaskRunRecord]


class KbRpaRawPayloadListResponse(BaseModel):
    items: List[KbRpaRawPayloadRecord]


class KbRpaCleanResultListResponse(BaseModel):
    items: List[KbRpaCleanResultRecord]


class KbRpaReplayResponse(BaseModel):
    status: str
    run_id: str
    replay_source_run_id: str
    ingestion_log_id: str
    entity_type: str
    received: int
    success_count: int
    failed_count: int
    failed_items: List[Dict[str, Any]] = Field(default_factory=list)
    inserted_ids: List[str] = Field(default_factory=list)


