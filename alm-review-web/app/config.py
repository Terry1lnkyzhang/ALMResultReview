from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    database_url: str
    app_timezone: str = "Asia/Shanghai"
    initial_import_path: str = ""
    alm_username: str = ""
    alm_password: str = ""
    ai_api_key: str = ""
    scheduler_enabled: bool = True
    app_role: Literal["combined", "web", "worker"] = "combined"
    worker_id: str = ""
    worker_poll_seconds: int = 5
    worker_lease_seconds: int = 900
    web_auth_enabled: bool = False
    web_auth_username: str = ""
    web_auth_password: str = ""

    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def import_path(self) -> Path | None:
        configured_path = self.initial_import_path.strip()
        if not configured_path:
            return None
        path = Path(configured_path)
        return path if path.is_absolute() else (PROJECT_DIR / path).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]