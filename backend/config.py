import secrets
import logging
from typing import List, Optional
from urllib.parse import urlparse
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    API_TOKEN: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ADMIN_IDS: str = ""
    DOMAINS: str = ""
    MAX_ZIP_SIZE_MB: int = 50
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # Supabase
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_BUCKET: str = "campaigns"

    # GeoIP
    GEOIP_PROVIDER: str = "ipapi"  # "ipapi" or "none"

    # Features
    ENABLE_CLICK_ALERTS: bool = True
    SEED_DEFAULT_CAMPAIGN: bool = True

    # Web auth
    WEB_USERNAME: str = "admin"
    WEB_PASSWORD: str = ""
    SESSION_SECRET: str = ""
    SESSION_TTL_HOURS: int = 24
    LOGIN_RATE_LIMIT_PER_15MIN: int = 5

    # Admin portal path obfuscation
    ADMIN_PATH_PREFIX: str = "/web/setlink/connect/service/ww/ww/wwww/www"

    # OTP fallback: if True, log OTP codes to server stdout instead of Telegram
    OTP_FALLBACK_LOG: bool = False

    # Link URL style and slug length (SLUG_LENGTH is used by generate_slug default)
    LINK_URL_STYLE: str = "service"  # short | service (any non-short value produces a long URL)
    SLUG_LENGTH: int = 32

    # Anti-bot protection
    ANTIBOT_ENABLED: bool = True
    ANTIBOT_SECRET: str = ""
    ANTIBOT_DEFAULT_LEVEL: str = "standard"  # off | light | standard | maximum
    ANTIBOT_REDIRECT_URL: str = "https://www.google.com/"
    ANTIBOT_RATE_LIMIT_WINDOW_SEC: int = 10
    ANTIBOT_RATE_LIMIT_MAX: int = 5

    def get_domains(self) -> List[str]:
        if not self.DOMAINS:
            return []
        return [d.strip() for d in self.DOMAINS.split(",") if d.strip()]

    def get_public_hostname(self) -> str:
        """Returns the hostname extracted from PUBLIC_BASE_URL."""
        parsed = urlparse(self.PUBLIC_BASE_URL)
        hostname = parsed.hostname or parsed.netloc
        if not hostname:
            logger.warning(
                "PUBLIC_BASE_URL '%s' is malformed or missing a hostname. Falling back to 'localhost'.",
                self.PUBLIC_BASE_URL,
            )
            return "localhost"
        return hostname

    def get_all_domains(self) -> List[dict]:
        """Returns all domains including PUBLIC_BASE_URL as the first/default entry."""
        public_hostname = self.get_public_hostname()
        result: List[dict] = [
            {"domain": public_hostname, "is_default": True, "label": "Domaine public Render"}
        ]
        for d in self.get_domains():
            if d != public_hostname:
                result.append({"domain": d, "is_default": False, "label": None})
        return result

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
_resolved_antibot_secret: Optional[str] = None


def get_resolved_token() -> str:
    global _resolved_token
    if _resolved_token is None:
        _resolved_token = settings.get_api_token()
    return _resolved_token


def get_antibot_secret() -> str:
    """Return the antibot HMAC secret, auto-generating one if not configured."""
    global _resolved_antibot_secret
    if _resolved_antibot_secret is None:
        if settings.ANTIBOT_SECRET:
            _resolved_antibot_secret = settings.ANTIBOT_SECRET
        else:
            _resolved_antibot_secret = secrets.token_hex(32)
    return _resolved_antibot_secret
