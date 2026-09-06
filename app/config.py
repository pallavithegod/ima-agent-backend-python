from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    app_name: str = "RecallOps Agent API"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    database_url: str = "postgresql+psycopg://recallops:recallops@localhost:5433/recallops"
    cors_origins: str = "http://localhost:5173"
    frontend_url: str = "http://localhost:5173"

    # Auth — values must match what the retired Node backend used so existing
    # sessions and encrypted credentials keep working.
    auth_jwt_secret: str = "development-only-secret-change-me"
    credential_encryption_key: str = ""
    firebase_service_account_base64: str = ""
    firebase_service_account_path: str = "./service-account.json"

    # Vercel OAuth
    vercel_client_id: str = ""
    vercel_client_secret: str = ""
    vercel_callback_url: str = "http://localhost:8000/api/integrations/vercel/callback"
    vercel_scopes: str = "openid profile email offline_access"

    # AI pipeline — Azure OpenAI is the primary LLM; DeepSeek is the fallback.
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = "gpt-5.5"
    azure_openai_api_version: str = "2024-12-01-preview"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-pro"
    # Personal-access-token fallback for PR creation when no GitHub connection
    # is stored (i.e. before the first Firebase GitHub sign-in).
    github_token: str = ""
    hindsight_api_url: str = ""
    hindsight_api_key: str = ""
    hindsight_bank_id: str = "incident-memory-agent"
    github_default_branch: str = "main"
    vercel_target: str = "production"

    # Commit monitor (0 disables)
    monitor_interval_ms: int = 60000

    # Clone-based fixer
    workspaces_dir: str = "./storage/workspaces"
    fixer_max_files: int = 5
    fixer_max_changed_lines: int = 400

    # VM monitoring
    vm_event_cooldown_seconds: int = 600
    vm_heartbeat_timeout_seconds: int = 60

    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def workspaces_path(self) -> Path:
        path = Path(self.workspaces_dir)
        if not path.is_absolute():
            path = BACKEND_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def deepseek_configured(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def azure_openai_configured(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_api_key)

    @property
    def vercel_oauth_configured(self) -> bool:
        return bool(self.vercel_client_id and self.vercel_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
