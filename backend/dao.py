import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from backend.db import get_supabase

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Campaigns
# ──────────────────────────────────────────────

def list_campaigns() -> list[dict]:
    try:
        sb = get_supabase()
        result = sb.table("campaigns").select("*").eq("is_current", True).order("created_at", desc=True).execute()
        return result.data or []
    except Exception as e:
        logger.error("list_campaigns error: %s", e)
        return []


def get_campaign(id: int) -> dict | None:
    try:
        sb = get_supabase()
        result = sb.table("campaigns").select("*").eq("id", id).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("get_campaign error: %s", e)
        return None


def get_campaign_by_name(name: str) -> dict | None:
    """Return the latest version (by version number) for a given campaign name."""
    try:
        sb = get_supabase()
        result = (
            sb.table("campaigns")
            .select("*")
            .eq("name", name)
            .order("version", desc=True)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("get_campaign_by_name error: %s", e)
        return None


def create_campaign(
    name: str,
    storage_path: str,
    entry_file: str,
    original_filename: str,
    version: int = 1,
) -> dict:
    try:
        sb = get_supabase()
        result = sb.table("campaigns").insert({
            "name": name,
            "storage_path": storage_path,
            "entry_file": entry_file,
            "original_filename": original_filename,
            "version": version,
            "is_current": True,
        }).execute()
        return result.data[0]
    except Exception as e:
        logger.error("create_campaign error: %s", e)
        raise


def delete_campaign(id: int) -> None:
    try:
        sb = get_supabase()
        sb.table("campaigns").delete().eq("id", id).execute()
    except Exception as e:
        logger.error("delete_campaign error: %s", e)
        raise


def list_campaign_versions(name: str) -> list[dict]:
    try:
        sb = get_supabase()
        result = sb.table("campaigns").select("*").eq("name", name).order("version", desc=True).execute()
        return result.data or []
    except Exception as e:
        logger.error("list_campaign_versions error: %s", e)
        return []


def set_current_version(campaign_id: int) -> None:
    try:
        sb = get_supabase()
        result = sb.table("campaigns").select("name").eq("id", campaign_id).execute()
        if not result.data:
            return
        name = result.data[0]["name"]
        sb.table("campaigns").update({"is_current": False}).eq("name", name).execute()
        sb.table("campaigns").update({"is_current": True}).eq("id", campaign_id).execute()
    except Exception as e:
        logger.error("set_current_version error: %s", e)
        raise


# ──────────────────────────────────────────────
# Links
# ──────────────────────────────────────────────

def list_links_for_campaign(campaign_id: int) -> list[dict]:
    try:
        sb = get_supabase()
        result = sb.table("links").select("*").eq("campaign_id", campaign_id).order("created_at", desc=True).execute()
        return result.data or []
    except Exception as e:
        logger.error("list_links_for_campaign error: %s", e)
        return []


def get_link_by_slug(slug: str) -> dict | None:
    try:
        sb = get_supabase()
        result = sb.table("links").select("*").eq("slug", slug).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("get_link_by_slug error: %s", e)
        return None


def get_link_by_id(link_id: int) -> dict | None:
    try:
        sb = get_supabase()
        result = sb.table("links").select("*").eq("id", link_id).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("get_link_by_id error: %s", e)
        return None


def create_link(
    slug: str,
    campaign_id: int,
    domain: Optional[str] = None,
    click_limit: Optional[int] = None,
    expires_at: Optional[str] = None,
) -> dict:
    try:
        sb = get_supabase()
        payload: dict = {"slug": slug, "campaign_id": campaign_id}
        if domain is not None:
            payload["domain"] = domain
        if click_limit is not None:
            payload["click_limit"] = click_limit
        if expires_at is not None:
            payload["expires_at"] = expires_at
        result = sb.table("links").insert(payload).execute()
        return result.data[0]
    except Exception as e:
        logger.error("create_link error: %s", e)
        raise


def deactivate_link(slug: str) -> None:
    try:
        sb = get_supabase()
        sb.table("links").update({"is_active": False}).eq("slug", slug).execute()
    except Exception as e:
        logger.error("deactivate_link error: %s", e)
        raise


# ──────────────────────────────────────────────
# Clicks
# ──────────────────────────────────────────────

def record_click(
    link_id: int,
    ip: str,
    user_agent: str,
    country: Optional[str],
    referer: Optional[str],
) -> dict:
    try:
        sb = get_supabase()
        result = sb.table("clicks").insert({
            "link_id": link_id,
            "ip": ip,
            "user_agent": user_agent,
            "country": country,
            "referer": referer,
        }).execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        logger.error("record_click error: %s", e)
        return {}


def get_link_stats(link_id: int) -> dict:
    try:
        sb = get_supabase()
        result = sb.table("link_stats").select("*").eq("link_id", link_id).execute()
        return result.data[0] if result.data else {"total_clicks": 0, "unique_visitors": 0}
    except Exception as e:
        logger.error("get_link_stats error: %s", e)
        return {"total_clicks": 0, "unique_visitors": 0}


def get_clicks_per_day(link_id: int, days: int = 7) -> list[dict]:
    try:
        sb = get_supabase()
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        result = sb.table("clicks").select("clicked_at").eq("link_id", link_id).gte("clicked_at", since).execute()
        counts: dict[str, int] = {}
        for row in result.data or []:
            date = row["clicked_at"][:10]
            counts[date] = counts.get(date, 0) + 1
        output = []
        for i in range(days):
            day = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
            output.append({"date": day, "count": counts.get(day, 0)})
        return output
    except Exception as e:
        logger.error("get_clicks_per_day error: %s", e)
        return []


def get_total_clicks_for_link(link_id: int) -> int:
    try:
        sb = get_supabase()
        result = sb.table("clicks").select("id", count="exact").eq("link_id", link_id).execute()
        return result.count or 0
    except Exception as e:
        logger.error("get_total_clicks_for_link error: %s", e)
        return 0


# ──────────────────────────────────────────────
# Geo rules
# ──────────────────────────────────────────────

def get_geo_rule(link_id: int) -> dict | None:
    try:
        sb = get_supabase()
        result = sb.table("geo_rules").select("*").eq("link_id", link_id).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error("get_geo_rule error: %s", e)
        return None


def set_geo_rule(link_id: int, mode: str, countries: list[str]) -> dict:
    try:
        sb = get_supabase()
        existing = sb.table("geo_rules").select("id").eq("link_id", link_id).execute()
        if existing.data:
            result = sb.table("geo_rules").update({
                "mode": mode,
                "countries": countries,
            }).eq("link_id", link_id).execute()
        else:
            result = sb.table("geo_rules").insert({
                "link_id": link_id,
                "mode": mode,
                "countries": countries,
            }).execute()
        return result.data[0]
    except Exception as e:
        logger.error("set_geo_rule error: %s", e)
        raise


def delete_geo_rule(link_id: int) -> None:
    try:
        sb = get_supabase()
        sb.table("geo_rules").delete().eq("link_id", link_id).execute()
    except Exception as e:
        logger.error("delete_geo_rule error: %s", e)
        raise


# ──────────────────────────────────────────────
# Click alerts
# ──────────────────────────────────────────────

def get_alerts_for_link(link_id: int) -> list[dict]:
    try:
        sb = get_supabase()
        result = sb.table("click_alerts").select("*").eq("link_id", link_id).execute()
        return result.data or []
    except Exception as e:
        logger.error("get_alerts_for_link error: %s", e)
        return []


def add_alert(link_id: int, threshold: int) -> dict:
    try:
        sb = get_supabase()
        result = sb.table("click_alerts").insert({
            "link_id": link_id,
            "threshold": threshold,
            "notified": False,
        }).execute()
        return result.data[0]
    except Exception as e:
        logger.error("add_alert error: %s", e)
        raise


def mark_alert_notified(alert_id: int) -> None:
    try:
        sb = get_supabase()
        sb.table("click_alerts").update({"notified": True}).eq("id", alert_id).execute()
    except Exception as e:
        logger.error("mark_alert_notified error: %s", e)


# ──────────────────────────────────────────────
# Helpers for direct field updates
# ──────────────────────────────────────────────

def migrate_links_to_campaign(link_ids: list[int], new_campaign_id: int) -> None:
    """Reassign a list of links to a new campaign (used during versioning)."""
    if not link_ids:
        return
    try:
        sb = get_supabase()
        for link_id in link_ids:
            sb.table("links").update({"campaign_id": new_campaign_id}).eq("id", link_id).execute()
    except Exception as e:
        logger.error("migrate_links_to_campaign error: %s", e)
        raise


def update_link_domain(slug: str, domain: str) -> None:
    """Update the custom domain of a link identified by slug."""
    try:
        sb = get_supabase()
        sb.table("links").update({"domain": domain}).eq("slug", slug).execute()
    except Exception as e:
        logger.error("update_link_domain error: %s", e)
        raise


def update_link_protection_level(slug: str, level: str) -> None:
    """Update the protection_level of a link identified by slug."""
    try:
        sb = get_supabase()
        sb.table("links").update({"protection_level": level}).eq("slug", slug).execute()
    except Exception as e:
        logger.error("update_link_protection_level error: %s", e)
        raise


# ──────────────────────────────────────────────
# Bot hits
# ──────────────────────────────────────────────

# In-memory fallback when the bot_hits table doesn't exist yet
_bot_hits_memory: list[dict] = []
_BOT_HITS_MEMORY_MAX = 1000


def log_bot_hit(
    slug: str,
    ip: str,
    user_agent: str,
    country: str | None,
    reason: str,
    score: int,
    link_id: int | None = None,
) -> None:
    """Record a bot hit. Falls back to in-memory if the Supabase table doesn't exist."""
    try:
        sb = get_supabase()
        payload: dict = {
            "slug": slug,
            "ip": ip,
            "user_agent": user_agent[:500] if user_agent else "",
            "country": country,
            "reason": reason,
            "score": score,
        }
        if link_id is not None:
            payload["link_id"] = link_id
        sb.table("bot_hits").insert(payload).execute()
        return
    except Exception as e:
        err_str = str(e).lower()
        if "does not exist" in err_str or "undefined table" in err_str or "42p01" in err_str:
            logger.warning("bot_hits table not found — using in-memory fallback (run migration 002_antibot.sql)")
        else:
            logger.error("log_bot_hit error: %s", e)

    # In-memory fallback
    entry = {
        "slug": slug,
        "ip": ip,
        "user_agent": user_agent[:500] if user_agent else "",
        "country": country,
        "reason": reason,
        "score": score,
        "link_id": link_id,
        "hit_at": datetime.now(timezone.utc).isoformat(),
    }
    _bot_hits_memory.append(entry)
    if len(_bot_hits_memory) > _BOT_HITS_MEMORY_MAX:
        del _bot_hits_memory[: len(_bot_hits_memory) - _BOT_HITS_MEMORY_MAX]


def get_bot_hits_stats(hours: int = 24) -> dict:
    """Return aggregated bot hit stats for the last N hours."""
    try:
        sb = get_supabase()
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        result = sb.table("bot_hits").select("reason").gte("hit_at", since).execute()
        rows = result.data or []
        total = len(rows)
        by_reason: dict[str, int] = {}
        for row in rows:
            r = row.get("reason", "unknown")
            # Normalise: keep only the prefix before ':'
            key = r.split(":")[0] if ":" in r else r
            by_reason[key] = by_reason.get(key, 0) + 1
        return {"total": total, "by_reason": by_reason}
    except Exception as e:
        err_str = str(e).lower()
        if "does not exist" in err_str or "42p01" in err_str:
            # Fall back to in-memory
            pass
        else:
            logger.error("get_bot_hits_stats error: %s", e)

    # In-memory fallback
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = [
        r for r in _bot_hits_memory
        if datetime.fromisoformat(r["hit_at"]) >= cutoff
    ]
    total = len(rows)
    by_reason: dict[str, int] = {}
    for row in rows:
        r = row.get("reason", "unknown")
        key = r.split(":")[0] if ":" in r else r
        by_reason[key] = by_reason.get(key, 0) + 1
    return {"total": total, "by_reason": by_reason}


def get_bot_hits_recent(limit: int = 50) -> list[dict]:
    """Return the most recent bot hits."""
    try:
        sb = get_supabase()
        result = (
            sb.table("bot_hits")
            .select("slug,ip,user_agent,country,reason,score,hit_at")
            .order("hit_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as e:
        err_str = str(e).lower()
        if "does not exist" in err_str or "42p01" in err_str:
            pass
        else:
            logger.error("get_bot_hits_recent error: %s", e)

    return list(reversed(_bot_hits_memory[-limit:]))
