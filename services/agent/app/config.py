from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    agent_host: str = Field(default="0.0.0.0", alias="AGENT_HOST")
    agent_port: int = Field(default=8100, alias="AGENT_PORT")
    supabase_url: str = Field(alias="SUPABASE_URL")
    supabase_service_role_key: str = Field(alias="SUPABASE_SERVICE_ROLE_KEY")
    supabase_postgrest_timeout_sec: float = Field(default=8, gt=0, alias="SUPABASE_POSTGREST_TIMEOUT_SEC")
    supabase_http_proxy: str = Field(default="", alias="SUPABASE_HTTP_PROXY", repr=False)
    supabase_trust_env: bool = Field(default=True, alias="SUPABASE_TRUST_ENV")
    internal_service_token: str = Field(default="", alias="INTERNAL_SERVICE_TOKEN")

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="", alias="OPENAI_BASE_URL")
    openai_model_draft: str = Field(default="openai/gpt-4.1-mini", alias="OPENAI_MODEL_DRAFT")
    openai_model_reflection: str = Field(default="openai/gpt-4.1", alias="OPENAI_MODEL_REFLECTION")
    openrouter_site_url: str = Field(default="", alias="OPENROUTER_SITE_URL")
    openrouter_app_name: str = Field(default="EvoContent Matrix", alias="OPENROUTER_APP_NAME")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    model_router_mode: str = Field(default="multi", alias="MODEL_ROUTER_MODE")
    browser_bridge_api_url: str = Field(default="http://127.0.0.1:8000", alias="BROWSER_BRIDGE_API_URL")
    browser_model_name: str = Field(default="", alias="BROWSER_MODEL_NAME")
    browser_model_api_key: str = Field(default="", alias="BROWSER_MODEL_API_KEY", repr=False)
    browser_model_base_url: str = Field(default="", alias="BROWSER_MODEL_BASE_URL")

    playwright_headless: bool = Field(default=True, alias="PLAYWRIGHT_HEADLESS")
    playwright_dry_run: bool = Field(default=True, alias="PLAYWRIGHT_DRY_RUN")
    playwright_storage_state_path: str = Field(default="", alias="PLAYWRIGHT_STORAGE_STATE_PATH")
    playwright_user_data_dir: str = Field(default="", alias="PLAYWRIGHT_USER_DATA_DIR")
    playwright_session_cookies_json: str = Field(default="", alias="PLAYWRIGHT_SESSION_COOKIES_JSON")
    playwright_proxy_server: str = Field(default="", alias="PLAYWRIGHT_PROXY_SERVER")
    playwright_proxy_username: str = Field(default="", alias="PLAYWRIGHT_PROXY_USERNAME")
    playwright_proxy_password: str = Field(default="", alias="PLAYWRIGHT_PROXY_PASSWORD")
    playwright_artifacts_dir: str = Field(default="artifacts/playwright", alias="PLAYWRIGHT_ARTIFACTS_DIR")
    playwright_publish_retries: int = Field(default=2, alias="PLAYWRIGHT_PUBLISH_RETRIES")
    playwright_session_reuse_enabled: bool = Field(default=True, alias="PLAYWRIGHT_SESSION_REUSE_ENABLED")
    playwright_session_idle_ttl_minutes: int = Field(default=20, alias="PLAYWRIGHT_SESSION_IDLE_TTL_MINUTES")
    playwright_action_pause_min_ms: int = Field(default=1000, alias="PLAYWRIGHT_ACTION_PAUSE_MIN_MS")
    playwright_action_pause_max_ms: int = Field(default=2000, alias="PLAYWRIGHT_ACTION_PAUSE_MAX_MS")
    publish_method_order: str = Field(default="playwright,mcp", alias="PUBLISH_METHOD_ORDER")

    daily_hotspot_max_per_query: int = Field(default=5, alias="DAILY_HOTSPOT_MAX_PER_QUERY")
    daily_collect_limit: int = Field(default=20, alias="DAILY_COLLECT_LIMIT")
    daily_hotspot_queries: str = Field(
        default="日本移民,日本经营管理签证,日本永住,日本留学",
        alias="DAILY_HOTSPOT_QUERIES",
    )
    daily_hotspot_enable_xhs: bool = Field(default=True, alias="DAILY_HOTSPOT_ENABLE_XHS")
    daily_xhs_max_per_query: int = Field(default=5, alias="DAILY_XHS_MAX_PER_QUERY")
    daily_xhs_wait_ms: int = Field(default=2400, alias="DAILY_XHS_WAIT_MS")
    daily_viewpoint_enabled: bool = Field(default=True, alias="DAILY_VIEWPOINT_ENABLED")
    daily_viewpoint_max_per_query: int = Field(default=3, alias="DAILY_VIEWPOINT_MAX_PER_QUERY")
    daily_viewpoint_collect_limit: int = Field(default=8, alias="DAILY_VIEWPOINT_COLLECT_LIMIT")
    daily_viewpoint_queries: str = Field(
        default="日本移民 观点,日本经营管理签证 劝退,日本移民 避坑,日本留学 真实经历",
        alias="DAILY_VIEWPOINT_QUERIES",
    )
    daily_auto_approve_publish: bool = Field(default=False, alias="DAILY_AUTO_APPROVE_PUBLISH")
    allow_auto_publish: bool = Field(default=False, alias="ALLOW_AUTO_PUBLISH")
    publish_guard_mode: str = Field(default="manual_only", alias="PUBLISH_GUARD_MODE")
    orchestrator_v2_enabled: bool = Field(default=True, alias="ORCHESTRATOR_V2_ENABLED")
    ui_v2_enabled: bool = Field(default=True, alias="UI_V2_ENABLED")
    daily_default_channel: str = Field(default="xiaohongshu", alias="DAILY_DEFAULT_CHANNEL")
    daily_run_max_attempts: int = Field(default=2, alias="DAILY_RUN_MAX_ATTEMPTS")
    daily_retry_backoff_seconds: int = Field(default=8, alias="DAILY_RETRY_BACKOFF_SECONDS")
    daily_scheduler_enabled: bool = Field(default=False, alias="DAILY_SCHEDULER_ENABLED")
    daily_scheduler_domain_slug: str = Field(default="japan_immigration", alias="DAILY_SCHEDULER_DOMAIN_SLUG")
    daily_scheduler_hour: int = Field(default=9, alias="DAILY_SCHEDULER_HOUR")
    daily_scheduler_minute: int = Field(default=0, alias="DAILY_SCHEDULER_MINUTE")
    daily_scheduler_utc_offset: str = Field(default="+09:00", alias="DAILY_SCHEDULER_UTC_OFFSET")
    publish_feedback_scheduler_enabled: bool = Field(
        default=True,
        alias="PUBLISH_FEEDBACK_SCHEDULER_ENABLED",
    )
    publish_feedback_domain_slug: str = Field(
        default="japan_immigration",
        alias="PUBLISH_FEEDBACK_DOMAIN_SLUG",
    )
    publish_feedback_poll_minutes: int = Field(
        default=30,
        alias="PUBLISH_FEEDBACK_POLL_MINUTES",
    )

    xhs_mcp_readonly_enabled: bool = Field(default=False, alias="XHS_MCP_READONLY_ENABLED")
    xhs_mcp_write_enabled: bool = Field(default=False, alias="XHS_MCP_WRITE_ENABLED")
    xhs_mcp_write_tool_allowlist: str = Field(
        default="publish_content,publish_with_video,post_comment_to_feed,reply_comment_in_feed,like_feed,favorite_feed",
        alias="XHS_MCP_WRITE_TOOL_ALLOWLIST",
    )
    xhs_mcp_base_url: str = Field(default="", alias="XHS_MCP_BASE_URL")
    xhs_mcp_api_key: str = Field(default="", alias="XHS_MCP_API_KEY")
    xhs_mcp_timeout_sec: int = Field(default=20, alias="XHS_MCP_TIMEOUT_SEC")
    xhs_mcp_search_tool: str = Field(default="xhs.search", alias="XHS_MCP_SEARCH_TOOL")
    xhs_mcp_metrics_tool: str = Field(default="xhs.post.metrics", alias="XHS_MCP_METRICS_TOOL")
    xhs_mcp_feed_tool: str = Field(default="list_feeds", alias="XHS_MCP_FEED_TOOL")
    xhs_mcp_bridge_to_playwright: bool = Field(default=True, alias="XHS_MCP_BRIDGE_TO_PLAYWRIGHT")
    daily_xhs_home_limit: int = Field(default=8, alias="DAILY_XHS_HOME_LIMIT")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
