"""
backend/auth_routes.py
======================
FastAPI router for admin authentication with full login traceability.

Endpoints
---------
POST /api/auth/login
    Authenticate username + password.
    On every attempt (success **or** failure):
      - Resolves full client IP (Cloudflare / reverse-proxy aware via
        X-Forwarded-For header)
      - Reads the HTTP User-Agent
      - Geolocates the IP via ip-api.com (country, city, ISP; 4 s timeout)
      - Writes an audit row to the ``login_attempts`` table (with in-memory
        fallback if the table doesn't exist yet)
      - Sends a real-time Telegram notification to the configured audit
        channel / admin DMs (fire-and-forget, never blocks the response)
    Responses:
      - 429 JSON  when rate-limit is exceeded (Retry-After header included)
      - 401 JSON  {"detail": "..."} on bad credentials
      - 200 JSON  {"ok": true} + httpOnly session cookie on success

POST /api/auth/logout
    Clear the session cookie.

GET  /api/auth/me
    Return the current session's username (requires session cookie).

Notes
-----
- Rate limiting: 5 attempts per IP per 15 minutes (configurable via
  LOGIN_RATE_LIMIT_PER_15MIN env var).
- Password logging: plain-text submitted passwords are **never** persisted by
  default. The ``password`` column is only written when the ``log_password``
  field is ``True`` on the matching user account — not applicable to the
  current single-admin setup (field omitted).
- Do **not** use this router for password-reset flows; those must never log
  the new password.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.auth import (
    create_session_token,
    require_session,
    verify_session_token,
    verify_web_credentials,
)
from backend.config import settings
from backend.geoip import lookup_full_geo
from backend.rate_limit import login_rate_limiter
import backend.dao as dao

logger = logging.getLogger(__name__)

router = APIRouter()


class LoginBody(BaseModel):
    """Expected JSON body for POST /api/auth/login."""
    username: str
    password: str


# ──────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    """
    Resolve the real client IP, honouring X-Forwarded-For (Cloudflare /
    reverse-proxy).  Returns the **leftmost** (most-client) IP in the chain.
    Falls back to the direct connection host, then "unknown".
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _is_secure(request: Request) -> bool:
    """Return True if the connection is HTTPS (used for the secure cookie flag)."""
    base = settings.PUBLIC_BASE_URL
    return base.startswith("https") or request.url.scheme == "https"


# ──────────────────────────────────────────────
# Auth routes
# ──────────────────────────────────────────────

@router.post("/api/auth/login", summary="Login with username + password")
async def auth_login(body: LoginBody, request: Request):
    """
    Authenticate an admin user.

    Expected JSON body:
        { "username": "<str>", "password": "<str>" }

    Returns
    -------
    200  {"ok": true}  + httpOnly session cookie  — on success
    401  {"detail": "..."}                        — bad credentials
    429  {"detail": "..."}  + Retry-After header  — rate limit exceeded

    Side-effects (always, regardless of outcome):
    - Writes a row to ``login_attempts`` (Supabase or in-memory fallback).
    - Sends a Telegram notification to the audit channel / admin DMs.
    """
    # ── 1. Extract credentials ────────────────────────────────────────────
    username: str = (body.username or "").strip()
    password: str = body.password or ""

    # ── 2. Resolve IP + User-Agent ────────────────────────────────────────
    ip: str = _get_client_ip(request)
    user_agent: str = request.headers.get("user-agent", "")

    try:
        # ── 3. Rate-limit check (before any heavy work) ───────────────────
        if not login_rate_limiter.is_allowed(ip):
            retry_after = login_rate_limiter.retry_after(ip)
            logger.warning("Rate limit exceeded for login from IP %s", ip)

            # Fire-and-forget audit + notification (rate_limited status)
            _fire_audit(
                username=username, ip=ip, user_agent=user_agent,
                status="rate_limited",
            )

            return JSONResponse(
                {"detail": "Trop de tentatives. Réessayez plus tard."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        # ── 4. Geolocation (ip-api.com, 4 s timeout) ─────────────────────
        #    Done before credential check so both success + failure rows
        #    contain the same geo data.
        geo = await lookup_full_geo(ip)

        # ── 5. Credential verification (bcrypt) ───────────────────────────
        success = verify_web_credentials(username, password)
        status_str = "success" if success else "failure"

        # ── 6. Persist audit row (always) ─────────────────────────────────
        try:
            dao.log_login_attempt(
                username=username,
                ip=ip,
                user_agent=user_agent,
                status=status_str,
                country=geo.get("country") or None,
                country_name=geo.get("country_name") or None,
                city=geo.get("city") or None,
                isp=geo.get("isp") or None,
                # NOTE: plain-text password logging is intentionally disabled.
                # To enable it on a per-user opt-in basis, pass:
                #   password=password  (only when user.log_password is True)
                # Never enable for password-reset flows.
            )
        except Exception as exc:
            # Never let audit failure block the auth response
            logger.error("Failed to log login attempt: %s", exc)

        # ── 7. Telegram notification (fire-and-forget, always) ────────────
        try:
            from backend.telegram_bot import send_login_audit
            import asyncio
            asyncio.ensure_future(
                send_login_audit(
                    username=username,
                    ip=ip,
                    user_agent=user_agent,
                    status=status_str,
                    country=geo.get("country", ""),
                    country_name=geo.get("country_name", ""),
                    city=geo.get("city", ""),
                    isp=geo.get("isp", ""),
                )
            )
        except Exception as exc:
            logger.warning("Could not schedule login audit notification: %s", exc)

        # ── 8. Return response ────────────────────────────────────────────
        if not success:
            logger.warning(
                "Failed login for username=%r ip=%s country=%s city=%s",
                username, ip, geo.get("country"), geo.get("city"),
            )
            return JSONResponse(
                {"detail": "Identifiant ou mot de passe incorrect."},
                status_code=401,
            )

        logger.info(
            "Successful login for username=%r ip=%s country=%s city=%s",
            username, ip, geo.get("country"), geo.get("city"),
        )

        token = create_session_token(username)
        secure = _is_secure(request)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            key="session",
            value=token,
            httponly=True,
            secure=secure,
            samesite="lax",
            max_age=settings.SESSION_TTL_HOURS * 3600,
            path="/",
        )
        return resp

    except Exception as exc:
        logger.exception("Unexpected error in auth_login: %s", exc)
        return JSONResponse(
            {"detail": f"Erreur serveur : {type(exc).__name__}"},
            status_code=500,
        )


@router.post("/api/auth/logout", summary="Invalidate session cookie")
async def auth_logout():
    """Clear the session cookie, effectively logging the user out."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key="session", path="/")
    return resp


@router.get("/api/auth/me", summary="Return current session username")
async def auth_me(payload: dict = Depends(require_session)):
    """Return the username associated with the current session cookie."""
    return {"username": payload.get("sub")}


# ──────────────────────────────────────────────
# Internal: non-blocking helper for rate-limit audit
# ──────────────────────────────────────────────

def _fire_audit(
    username: str, ip: str, user_agent: str, status: str
) -> None:
    """Synchronously log in-memory; geo is skipped for rate-limited requests."""
    try:
        dao.log_login_attempt(
            username=username, ip=ip, user_agent=user_agent, status=status
        )
    except Exception as exc:
        logger.error("Failed to log rate-limited login attempt: %s", exc)

    try:
        from backend.telegram_bot import send_login_audit
        import asyncio
        asyncio.ensure_future(
            send_login_audit(username=username, ip=ip, user_agent=user_agent, status=status)
        )
    except Exception as exc:
        logger.warning("Could not schedule rate-limit notification: %s", exc)
