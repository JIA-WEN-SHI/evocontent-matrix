from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    allowed_origins: str = Field(
        default="http://localhost:3001,http://127.0.0.1:3001",
        alias="ALLOWED_ORIGINS",
    )
    supabase_url: str = Field(alias="SUPABASE_URL")
    supabase_service_role_key: str = Field(alias="SUPABASE_SERVICE_ROLE_KEY")
    supabase_postgrest_timeout_sec: float = Field(default=8, gt=0, alias="SUPABASE_POSTGREST_TIMEOUT_SEC")
    supabase_http_proxy: str = Field(default="", alias="SUPABASE_HTTP_PROXY", repr=False)
    supabase_trust_env: bool = Field(default=True, alias="SUPABASE_TRUST_ENV")
    webhook_shared_secret: str = Field(alias="WEBHOOK_SHARED_SECRET")
    pii_encryption_key: str = Field(alias="PII_ENCRYPTION_KEY")
    agent_service_url: str = Field(default="http://localhost:8100", alias="AGENT_SERVICE_URL")
    internal_service_token: str = Field(default="", alias="INTERNAL_SERVICE_TOKEN")
    browser_model_name: str = Field(default="", alias="BROWSER_MODEL_NAME")
    browser_model_base_url: str = Field(default="", alias="BROWSER_MODEL_BASE_URL")
    model_draft_name: str = Field(default="openai/gpt-4.1-mini", alias="OPENAI_MODEL_DRAFT")
    model_api_base_url: str = Field(default="", alias="OPENAI_BASE_URL")
    orchestrator_v2_enabled: bool = Field(default=True, alias="ORCHESTRATOR_V2_ENABLED")
    ui_v2_enabled: bool = Field(default=True, alias="UI_V2_ENABLED")
    publish_guard_mode: str = Field(default="manual_only", alias="PUBLISH_GUARD_MODE")
    allow_auto_publish: bool = Field(default=False, alias="ALLOW_AUTO_PUBLISH")

    @property
    def cors_origins(self) -> List[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
