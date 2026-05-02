import logging
import secrets
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import jwt
from fastapi import Cookie, Header, HTTPException, status
from passlib.context import CryptContext

from backend.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# In-memory resolved (possibly bcrypt-hashed) password
_resolved_password_hash: Optional[str] = None


def _get_password_hash() -> str:
    global _resolved_password_hash
    if _resolved_password_hash is None:
        raw = settings.WEB_PASSWORD
        if not raw:
            # No password set — allow no-op (will be rejected at verify time)
            _resolved_password_hash = ""
        elif raw.startswith("$2b$") or raw.startswith("$2a$"):
            # Already a bcrypt hash
            _resolved_password_hash = raw
        else:
            # Plain-text — hash in memory and warn
            logger.warning(
                "⚠️  WEB_PASSWORD is stored as plain text. "
                "Consider replacing it with a bcrypt hash: "
                "python -c \"from passlib.hash import bcrypt; print(bcrypt.hash('yourpassword'))\""
            )
            _resolved_password_hash = _pwd_context.hash(raw)
    return _resolved_password_hash


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    return _pwd_context.verify(plain, hashed)


def verify_web_credentials(username: str, password: str) -> bool:
    if username != settings.WEB_USERNAME:
        # Constant-time dummy verify to avoid username enumeration
        _pwd_context.dummy_verify()
        return False
    return verify_password(password, _get_password_hash())


# ---------------------------------------------------------------------------
# SESSION_SECRET — persisted to disk so it survives restarts
# ---------------------------------------------------------------------------

_SECRET_FILE = Path("storage/.session_secret")
_resolved_secret: Optional[str] = None


def get_session_secret() -> str:
    global _resolved_secret
    if _resolved_secret is not None:
        return _resolved_secret

    # 1. Use env var if set
    if settings.SESSION_SECRET:
        _resolved_secret = settings.SESSION_SECRET
        return _resolved_secret

    # 2. Try to read from persisted file
    try:
        if _SECRET_FILE.exists():
            _resolved_secret = _SECRET_FILE.read_text().strip()
            if _resolved_secret:
                return _resolved_secret
    except OSError:
        pass

    # 3. Generate a new secret and persist it
    _resolved_secret = secrets.token_hex(32)
    try:
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(_resolved_secret)
        logger.info("Generated new SESSION_SECRET and persisted to %s", _SECRET_FILE)
    except OSError as exc:
        logger.warning("Could not persist SESSION_SECRET to disk: %s", exc)

    return _resolved_secret


# ---------------------------------------------------------------------------
# JWT session tokens
# ---------------------------------------------------------------------------

_ALGORITHM = "HS256"


def create_session_token(username: str) -> str:
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(hours=settings.SESSION_TTL_HOURS),
    }
    return jwt.encode(payload, get_session_secret(), algorithm=_ALGORITHM)


def verify_session_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, get_session_secret(), algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def require_session(session: Optional[str] = Cookie(default=None)) -> dict:
    """FastAPI dependency — requires a valid session cookie."""
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    payload = verify_session_token(session)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
        )
    return payload


from backend.config import get_resolved_token  # noqa: E402 — avoid circular at top level


def require_auth(
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token"),
    session: Optional[str] = Cookie(default=None),
) -> dict:
    """FastAPI dependency — accepts either a valid X-API-Token header OR a valid session cookie."""
    # Try API token first (backward-compat for scripts/curl)
    if x_api_token is not None:
        if x_api_token == get_resolved_token():
            return {"sub": "__api_token__", "via": "api_token"}
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
        )

    # Fall back to session cookie
    if session is not None:
        payload = verify_session_token(session)
        if payload is not None:
            return payload

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


# ---------------------------------------------------------------------------
# Legacy helpers (kept for Telegram bot)
# ---------------------------------------------------------------------------

def require_api_token(x_api_token: str = Header(..., alias="X-API-Token")):
    """Dependency that validates the X-API-Token header (legacy)."""
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

