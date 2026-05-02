"""Challenge token generation and challenge page rendering."""

import hashlib
import hmac
import time
from pathlib import Path

_CHALLENGE_HTML_PATH = Path(__file__).parent.parent.parent / "frontend" / "challenge.html"

# Max age for a challenge token (seconds)
CHALLENGE_TOKEN_TTL = 60


def _secret(antibot_secret: str) -> bytes:
    return antibot_secret.encode("utf-8")


def make_challenge_token(slug: str, antibot_secret: str) -> str:
    """Generate a time-limited HMAC challenge token for the given slug."""
    ts = int(time.time())
    payload = f"chal:{slug}:{ts}"
    sig = hmac.new(_secret(antibot_secret), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{ts}.{sig}"


def verify_challenge_token(slug: str, token: str, antibot_secret: str) -> bool:
    """Return True if the challenge token is valid and not expired."""
    if not token or "." not in token:
        return False
    try:
        ts_str, sig = token.split(".", 1)
        ts = int(ts_str)
        if time.time() - ts > CHALLENGE_TOKEN_TTL:
            return False
        expected = hmac.new(
            _secret(antibot_secret),
            f"chal:{slug}:{ts}".encode(),
            hashlib.sha256,
        ).hexdigest()[:24]
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def render_challenge_page(slug: str, token: str) -> str:
    """Load challenge.html and inject slug + token."""
    if not _CHALLENGE_HTML_PATH.exists():
        # Minimal fallback if file is missing
        return (
            "<!DOCTYPE html><html><head><title>Loading...</title></head>"
            "<body style='background:#fff'>"
            "<noscript><meta http-equiv='refresh' content='0;url=https://www.google.com/'></noscript>"
            "</body></html>"
        )
    content = _CHALLENGE_HTML_PATH.read_text(encoding="utf-8")
    content = content.replace("__TOKEN__", token).replace("__SLUG__", slug)
    return content
