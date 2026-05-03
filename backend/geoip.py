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
# Full-geo cache: ip → {"country": str, "country_name": str, "city": str, "isp": str}
_full_geo_cache: dict[str, dict] = {}


def _is_private_ip(ip: str) -> bool:
    """Return True if the IP is a private/loopback address."""
    for prefix in PRIVATE_PREFIXES:
        if ip.startswith(prefix):
            return True
    return False


async def lookup_country(ip: str) -> Optional[str]:
    """Return ISO 2-letter country code or None. Cached in memory. Skips private IPs."""
    if not ip or ip == "unknown":
        return None

    if _is_private_ip(ip):
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
                    # Evict a fixed batch of 1000 entries to reduce per-insertion overhead
                    evict_count = 1000
                    keys_to_delete = list(_cache.keys())[:evict_count]
                    for key in keys_to_delete:
                        del _cache[key]
                return country
    except Exception as e:
        logger.debug("GeoIP lookup failed for %s: %s", ip, e)

    _cache[ip] = None
    return None


async def lookup_full_geo(ip: str) -> dict:
    """
    Return a dict with full geo info for the given IP via ip-api.com.

    Returns:
        {
            "country": "FR",            # ISO 2-letter code (may be empty string)
            "country_name": "France",   # Human-readable country name
            "city": "Paris",            # City name
            "isp": "Free SAS",          # ISP / org name
        }

    Falls back to empty strings on any error or for private IPs.
    Timeout: 4 seconds as per spec.
    Results are cached in-memory per IP.
    """
    empty: dict = {"country": "", "country_name": "", "city": "", "isp": ""}

    if not ip or ip == "unknown":
        return empty

    if _is_private_ip(ip):
        return {**empty, "city": "local", "isp": "private network"}

    if ip in _full_geo_cache:
        return _full_geo_cache[ip]

    if settings.GEOIP_PROVIDER == "none":
        return empty

    try:
        # ip-api.com free tier: up to 45 req/min, no API key needed.
        # Fields: status, countryCode, country, city, isp
        url = f"http://ip-api.com/json/{ip}?fields=status,countryCode,country,city,isp"
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(url)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success":
                result = {
                    "country": data.get("countryCode", ""),
                    "country_name": data.get("country", ""),
                    "city": data.get("city", ""),
                    "isp": data.get("isp", ""),
                }
                _full_geo_cache[ip] = result
                # Evict oldest entries when cache grows large
                if len(_full_geo_cache) > 10000:
                    evict_keys = list(_full_geo_cache.keys())[:1000]
                    for k in evict_keys:
                        del _full_geo_cache[k]
                return result
    except Exception as e:
        logger.debug("Full GeoIP lookup failed for %s: %s", ip, e)

    _full_geo_cache[ip] = empty
    return empty
