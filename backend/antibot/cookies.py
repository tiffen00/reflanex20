"""HMAC-based cookie signing for anti-bot verification."""

import hashlib
import hmac
import time


COOKIE_NAME = "_v_ok"
COOKIE_TTL = 3600  # 1 hour in seconds


def _secret(antibot_secret: str) -> bytes:
    return antibot_secret.encode("utf-8")


def make_cookie(slug: str, antibot_secret: str) -> str:
    """Create a signed cookie value for the given slug."""
    ts = int(time.time())
    payload = f"{slug}:{ts}"
    sig = hmac.new(_secret(antibot_secret), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{ts}.{sig}"


def verify_cookie(slug: str, cookie: str | None, antibot_secret: str) -> bool:
    """Return True if the cookie is valid and not expired for the given slug."""
    if not cookie or "." not in cookie:
        return False
    try:
        ts_str, sig = cookie.split(".", 1)
        ts = int(ts_str)
        if abs(time.time() - ts) > COOKIE_TTL:
            return False
        expected = hmac.new(
            _secret(antibot_secret),
            f"{slug}:{ts}".encode(),
            hashlib.sha256,
        ).hexdigest()[:24]
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False
