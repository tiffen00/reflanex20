from fastapi import Header, HTTPException, status

from backend.config import get_resolved_token, settings


def require_api_token(x_api_token: str = Header(..., alias="X-API-Token")):
    """Dependency that validates the X-API-Token header."""
    if x_api_token != get_resolved_token():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
        )


def is_telegram_admin(user_id: int) -> bool:
    admin_ids = settings.get_admin_ids()
    if not admin_ids:
        return False
    return user_id in admin_ids
