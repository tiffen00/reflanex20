"""Multi-layer bot detection for Reflanex20."""

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from .headers import score_headers
from .rate_limit import antibot_rate_limiter
from .ua_blocklist import is_blocked_ua

logger = logging.getLogger(__name__)

# Headers for non-HTML assets that should skip rate limiting
_ASSET_EXTENSIONS = frozenset({
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".webp", ".avif", ".mp4",
    ".webm", ".ogg", ".mp3", ".json", ".xml", ".txt", ".pdf",
})

# Threshold for suspicious headers score
_HEADERS_SCORE_THRESHOLD = 3


@dataclass
class BotDetection:
    is_bot: bool
    reason: Optional[str]
    score: int


def _get_real_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _is_asset_path(path: str) -> bool:
    """Return True if the path looks like a static asset (skip rate limiting)."""
    lower = path.lower()
    for ext in _ASSET_EXTENSIONS:
        if lower.endswith(ext):
            return True
    return False


def detect_bot(request: Request, protection_level: str = "standard") -> BotDetection:
    """
    Run Layer 1 checks against the request.

    protection_level:
      "off"      — no filtering
      "light"    — UA + headers only
      "standard" — UA + headers + rate limit + method
      "maximum"  — same as standard (cookie check is handled separately)
    """
    if protection_level == "off":
        return BotDetection(False, None, 0)

    ua = request.headers.get("user-agent", "")

    # 1. Missing UA
    if not ua:
        return BotDetection(True, "missing_ua", 100)

    # 2. UA blocklist
    if is_blocked_ua(ua):
        return BotDetection(True, f"blocked_ua:{ua[:80]}", 100)

    # 3. Headers scoring
    score = score_headers(request.headers)
    if score >= _HEADERS_SCORE_THRESHOLD:
        return BotDetection(True, f"suspicious_headers:{score}", score)

    if protection_level == "light":
        return BotDetection(False, None, score)

    # 4. Rate limiting (skip for assets)
    if not _is_asset_path(request.url.path):
        ip = _get_real_ip(request)
        if antibot_rate_limiter.is_rate_limited(ip):
            return BotDetection(True, "rate_limit", 100)

    # 5. HTTP method
    if request.method not in ("GET", "HEAD"):
        # HEAD is allowed since some browsers/proxies use it;
        # only block non-GET/HEAD methods on campaign routes
        pass

    if request.method not in ("GET",):
        # We only allow GET on campaign content routes;
        # HEAD is suspicious on campaign routes (scanners)
        if request.method == "HEAD":
            return BotDetection(True, f"non_get_method:{request.method}", 100)

    return BotDetection(False, None, score)
