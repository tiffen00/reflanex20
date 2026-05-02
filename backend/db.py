import logging
from supabase import create_client, Client
from backend.config import settings

logger = logging.getLogger(__name__)
_client: Client | None = None


def get_supabase() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
        logger.info("✅ Supabase client initialized")
    return _client
