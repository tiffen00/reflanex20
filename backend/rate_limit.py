"""Simple sliding-window rate limiter in memory, keyed by IP address."""

import logging
import time
from collections import deque
from typing import Dict, Deque

from backend.config import settings

logger = logging.getLogger(__name__)

# Window duration in seconds (15 minutes)
_WINDOW_SECONDS = 15 * 60


class RateLimiter:
    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = {}

    def is_allowed(self, ip: str) -> bool:
        """Return True if the request should be allowed, False if rate-limited."""
        now = time.monotonic()
        window_start = now - _WINDOW_SECONDS

        if ip not in self._hits:
            self._hits[ip] = deque()

        hits = self._hits[ip]
        # Purge old entries
        while hits and hits[0] < window_start:
            hits.popleft()

        if len(hits) >= settings.LOGIN_RATE_LIMIT_PER_15MIN:
            return False

        hits.append(now)
        return True

    def retry_after(self, ip: str) -> int:
        """Seconds until the oldest entry in the window expires."""
        now = time.monotonic()
        window_start = now - _WINDOW_SECONDS
        hits = self._hits.get(ip, deque())
        # Find the oldest hit still in the window
        for ts in hits:
            if ts >= window_start:
                # This hit expires at ts + _WINDOW_SECONDS
                return max(1, int(ts + _WINDOW_SECONDS - now) + 1)
        return 1


# Module-level singleton
login_rate_limiter = RateLimiter()
