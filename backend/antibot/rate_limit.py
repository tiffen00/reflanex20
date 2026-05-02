"""In-memory sliding-window rate limiter for campaign routes (/c/*)."""

import time
from collections import deque
from typing import Deque, Dict


class AntibotRateLimiter:
    """Sliding-window rate limiter keyed by IP address."""

    def __init__(self, max_requests: int = 5, window_seconds: int = 10) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        # Cap memory usage: evict oldest entries when dict exceeds this size
        self._max_entries = 50_000
        self._hits: Dict[str, Deque[float]] = {}

    def is_rate_limited(self, ip: str) -> bool:
        """Return True if the IP has exceeded the rate limit."""
        now = time.monotonic()
        window_start = now - self._window_seconds

        if ip not in self._hits:
            if len(self._hits) >= self._max_entries:
                # Simple eviction: remove one arbitrary entry
                oldest_key = next(iter(self._hits))
                del self._hits[oldest_key]
            self._hits[ip] = deque()

        hits = self._hits[ip]
        # Purge old entries
        while hits and hits[0] < window_start:
            hits.popleft()

        if len(hits) >= self._max_requests:
            return True

        hits.append(now)
        return False

    def configure(self, max_requests: int, window_seconds: int) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds


# Module-level singleton
antibot_rate_limiter = AntibotRateLimiter()
