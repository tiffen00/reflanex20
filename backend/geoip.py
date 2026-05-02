import asyncio
import logging
from typing import Optional
import httpx
from backend.config import settings

logger = logging.getLogger(__name__)

PRIVATE_PREFIXES = (
    "10.", "192.168.", "127.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.",
    "172.27.", "172.28.", "172.29.", "172.30.", "172.31.", "::1", "fd", "fc",
)

_cache: dict[str, Optional[str]] = {}


async def lookup_country(ip: str) -> Optional[str]:
    """Return ISO 2-letter country code or None. Cached in memory. Skips private IPs."""
    if not ip or ip == "unknown":
        return None

    for prefix in PRIVATE_PREFIXES:
        if ip.startswith(prefix):
            return None

    if ip in _cache:
        return _cache[ip]

    if settings.GEOIP_PROVIDER == "none":
        return None

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"https://ipapi.co/{ip}/country/")
            if r.status_code == 200 and len(r.text.strip()) == 2:
                country = r.text.strip().upper()
                _cache[ip] = country
                if len(_cache) > 10000:
                    # Evict oldest 10% to avoid per-call eviction churn
                    evict_count = max(1, len(_cache) // 10)
                    for _ in range(evict_count):
                        oldest = next(iter(_cache))
                        del _cache[oldest]
                return country
    except Exception as e:
        logger.debug("GeoIP lookup failed for %s: %s", ip, e)

    _cache[ip] = None
    return None
