from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    app_name: str = "Incident Memory Agent API"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    database_path: str = "./storage/incidents.db"
    cors_origins: str = "http://localhost:5173"
    auth_jwt_secret: str = "development-only-secret-change-me"
    node_backend_url: str = "http://localhost:4000"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-pro"
    hindsight_api_url: str = ""
    hindsight_api_key: str = ""
    hindsight_bank_id: str = "incident-memory-agent"
    github_mcp_url: str = "https://api.githubcopilot.com/mcp/"
    github_default_branch: str = "main"
    vercel_target: str = "production"

    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def database_file(self) -> Path:
        path = Path(self.database_path)
        if not path.is_absolute():
            path = BACKEND_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def deepseek_configured(self) -> bool:
        return bool(self.deepseek_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
