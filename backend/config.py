import secrets
import logging
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    API_TOKEN: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ADMIN_IDS: str = ""
    DOMAINS: str = ""
    DATABASE_URL: str = "sqlite:///./storage/app.db"
    STORAGE_DIR: str = "./storage/campaigns"
    MAX_ZIP_SIZE_MB: int = 50
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # Web auth
    WEB_USERNAME: str = "admin"
    WEB_PASSWORD: str = ""
    SESSION_SECRET: str = ""
    SESSION_TTL_HOURS: int = 24
    LOGIN_RATE_LIMIT_PER_15MIN: int = 5

    def get_domains(self) -> List[str]:
        if not self.DOMAINS:
            return []
        return [d.strip() for d in self.DOMAINS.split(",") if d.strip()]

    def get_admin_ids(self) -> List[int]:
        if not self.TELEGRAM_ADMIN_IDS:
            return []
        try:
            return [int(x.strip()) for x in self.TELEGRAM_ADMIN_IDS.split(",") if x.strip()]
        except ValueError:
            return []

    def get_api_token(self) -> str:
        if not self.API_TOKEN:
            token = secrets.token_urlsafe(32)
            return token
        return self.API_TOKEN


settings = Settings()
_resolved_token: Optional[str] = None


def get_resolved_token() -> str:
    global _resolved_token
    if _resolved_token is None:
        _resolved_token = settings.get_api_token()
    return _resolved_token
