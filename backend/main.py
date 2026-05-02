import asyncio
import logging
import posixpath
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    UploadFile,
    File,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.auth import (
    require_auth,
    require_session,
    verify_web_credentials,
    create_session_token,
    verify_session_token,
    get_session_secret,
)
from backend.config import settings, get_resolved_token
import backend.dao as dao
from backend.db import get_supabase
from backend.geoip import lookup_country
from backend.mime import guess_inline_content_type
from backend.rate_limit import login_rate_limiter
import backend.storage_supabase as storage_sb
from backend.storage import StorageError
from backend.utils import generate_slug, slugify

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


def _serve_html_with_prefix(filename: str) -> HTMLResponse:
    """Serve an HTML file with __ADMIN_PREFIX__ replaced by the configured value."""
    page = FRONTEND_DIR / filename
    if not page.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found")
    content = page.read_text(encoding="utf-8")
    safe_prefix = re.sub(r"[^a-zA-Z0-9/_\-]", "", settings.ADMIN_PATH_PREFIX)
    content = content.replace("__ADMIN_PREFIX__", safe_prefix)
    return HTMLResponse(content)


# ──────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialise session secret (generate + persist if needed)
    get_session_secret()

    # Log / auto-generate API token
    token = get_resolved_token()
    if not settings.API_TOKEN:
        masked = token[:4] + "..."
        logger.warning(
            "🔑 API_TOKEN not set — auto-generated. "
            "Set API_TOKEN env var. Token prefix: %s",
            masked,
        )
        print(f"[reflanex20] STARTUP API_TOKEN={token}", flush=True)
    else:
        logger.info("🔑 API_TOKEN is set from environment.")

    # Boot configuration validation
    missing: list[str] = []
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("❌ TELEGRAM_BOT_TOKEN is missing — login won't work")
        missing.append("TELEGRAM_BOT_TOKEN")
    else:
        logger.info("✅ TELEGRAM_BOT_TOKEN: set")

    admin_ids = settings.get_admin_ids()
    if not admin_ids:
        logger.warning("❌ TELEGRAM_ADMIN_IDS is empty — no one can receive OTPs")
        missing.append("TELEGRAM_ADMIN_IDS")
    else:
        logger.info("✅ TELEGRAM_ADMIN_IDS: set (%d admin(s))", len(admin_ids))

    if not settings.WEB_USERNAME:
        logger.warning("❌ WEB_USERNAME is missing — login disabled")
        missing.append("WEB_USERNAME")
    else:
        logger.info("✅ WEB_USERNAME: set")

    if not settings.WEB_PASSWORD:
        logger.warning("❌ WEB_PASSWORD is missing — login disabled")
        missing.append("WEB_PASSWORD")
    else:
        logger.info("✅ WEB_PASSWORD: set")

    if not settings.SUPABASE_URL:
        logger.warning("❌ SUPABASE_URL is missing — database won't work")
        missing.append("SUPABASE_URL")
    else:
        logger.info("✅ SUPABASE_URL: set")

    if not settings.SUPABASE_SERVICE_KEY:
        logger.warning("❌ SUPABASE_SERVICE_KEY is missing — database won't work")
        missing.append("SUPABASE_SERVICE_KEY")
    else:
        logger.info("✅ SUPABASE_SERVICE_KEY: set")

    if missing:
        logger.warning("⚠️  Missing env vars: %s", ", ".join(missing))
    else:
        logger.info("✅ Configuration looks OK")

    logger.info("🔒 Admin portal prefix: %s", settings.ADMIN_PATH_PREFIX)
    if settings.OTP_FALLBACK_LOG:
        logger.warning("⚠️  OTP_FALLBACK_LOG=true — OTP codes will be logged to stdout (dev mode)")

    # Start Telegram bot if token is configured
    bot_task = None
    if settings.TELEGRAM_BOT_TOKEN:
        from backend.telegram_bot import build_application, set_bot_username, setup_bot_ui
        tg_app = build_application()
        bot_task = asyncio.create_task(_run_bot(tg_app))
        logger.info("🤖 Telegram bot started.")
        try:
            await set_bot_username(tg_app.bot)
        except Exception as exc:
            logger.warning("Could not cache bot username: %s", exc)
        try:
            await setup_bot_ui(tg_app.bot)
        except Exception as exc:
            logger.warning("Could not setup bot UI: %s", exc)
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot disabled.")

    yield

    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass


async def _run_bot(tg_app):
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


# ──────────────────────────────────────────────
# App
# ──────────────────────────────────────────────

app = FastAPI(title="Reflanex20", version="2.0.0", lifespan=lifespan)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ──────────────────────────────────────────────
# Global exception handler
# ──────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "Unhandled exception on %s %s: %s", request.method, request.url.path, exc
    )
    return JSONResponse(
        {
            "detail": f"Erreur serveur : {type(exc).__name__}",
            "path": request.url.path,
        },
        status_code=500,
    )


# ──────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────

class LinkOut(BaseModel):
    id: int
    slug: str
    domain: Optional[str]
    total_clicks: int
    is_active: bool
    full_url: str


class CampaignOut(BaseModel):
    id: int
    name: str
    original_filename: Optional[str]
    created_at: str
    storage_path: str
    entry_file: str
    version: int
    links: List[LinkOut] = []


class NewLinkBody(BaseModel):
    domain: Optional[str] = None


class LoginBody(BaseModel):
    username: str
    password: str


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _make_full_url(slug: str, domain: Optional[str]) -> str:
    if domain:
        return f"https://{domain}/c/{slug}/"
    return f"{settings.PUBLIC_BASE_URL}/c/{slug}/"


def _link_out(link: dict) -> LinkOut:
    stats = dao.get_link_stats(link["id"])
    return LinkOut(
        id=link["id"],
        slug=link["slug"],
        domain=link.get("domain"),
        total_clicks=stats.get("total_clicks", 0),
        is_active=link.get("is_active", True),
        full_url=_make_full_url(link["slug"], link.get("domain")),
    )


def _campaign_out(c: dict) -> CampaignOut:
    links = dao.list_links_for_campaign(c["id"])
    return CampaignOut(
        id=c["id"],
        name=c["name"],
        original_filename=c.get("original_filename"),
        created_at=c.get("created_at", ""),
        storage_path=c.get("storage_path", ""),
        entry_file=c.get("entry_file", ""),
        version=c.get("version", 1),
        links=[_link_out(l) for l in links],
    )


def _unique_slug() -> str:
    for _ in range(20):
        slug = generate_slug()
        if not dao.get_link_by_slug(slug):
            return slug
    raise HTTPException(status_code=500, detail="Could not generate unique slug")


# ──────────────────────────────────────────────
# Public routes
# ──────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/diag")
async def diag():
    """Public diagnostic endpoint — returns config status without exposing secrets."""
    try:
        sb = get_supabase()
        sb.table("campaigns").select("id").limit(1).execute()
        supabase_db = "ok"
    except Exception as e:
        logger.warning("Supabase DB check failed: %s", e)
        supabase_db = f"error: {type(e).__name__}"

    try:
        sb = get_supabase()
        sb.storage.from_("campaigns").list("")
        supabase_storage = "ok"
    except Exception as e:
        logger.warning("Supabase storage check failed: %s", e)
        supabase_storage = f"error: {type(e).__name__}"

    telegram_status: str
    if not settings.TELEGRAM_BOT_TOKEN:
        telegram_status = "not_configured"
    else:
        try:
            from backend.telegram_bot import _bot_instance
            telegram_status = "configured" if _bot_instance is not None else "error"
        except Exception:
            telegram_status = "error"

    admin_ids = settings.get_admin_ids()
    domains = settings.get_domains()
    session_ok = bool(get_session_secret())

    return {
        "supabase_db": supabase_db,
        "supabase_storage": supabase_storage,
        "telegram_bot": telegram_status,
        "telegram_admins_count": len(admin_ids),
        "session_secret": "ok" if session_ok else "missing",
        "domains_count": len(domains),
        "public_base_url": settings.PUBLIC_BASE_URL,
        "version": "2.0.0",
    }


@app.get("/", include_in_schema=False)
async def serve_root():
    return JSONResponse({"detail": "Page introuvable"}, status_code=404)


@app.get("/c/{slug}", include_in_schema=False)
@app.get("/c/{slug}/", include_in_schema=False)
async def serve_slug_root(slug: str, request: Request):
    return await _serve_campaign_file(slug, "", request)


@app.get("/c/{slug}/{path:path}", include_in_schema=False)
async def serve_slug_path(slug: str, path: str, request: Request):
    return await _serve_campaign_file(slug, path, request)


async def _serve_campaign_file(slug: str, path: str, request: Request):
    link = dao.get_link_by_slug(slug)
    if not link or not link.get("is_active"):
        raise HTTPException(status_code=404, detail="Lien introuvable ou désactivé")

    # Check click_limit
    if link.get("click_limit"):
        stats = dao.get_link_stats(link["id"])
        if stats.get("total_clicks", 0) >= link["click_limit"]:
            raise HTTPException(status_code=403, detail="Limite de clics atteinte")

    # Check expiry
    if link.get("expires_at"):
        expires = datetime.fromisoformat(link["expires_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires:
            raise HTTPException(status_code=403, detail="Lien expiré")

    # Geo-blocking
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    geo_rule = dao.get_geo_rule(link["id"])
    country: Optional[str] = None
    if geo_rule:
        country = await lookup_country(ip)
        if country:
            if geo_rule["mode"] == "block" and country in geo_rule["countries"]:
                raise HTTPException(status_code=403, detail="Accès refusé depuis votre pays")
            elif geo_rule["mode"] == "allow" and country not in geo_rule["countries"]:
                raise HTTPException(status_code=403, detail="Accès refusé depuis votre pays")

    campaign = dao.get_campaign(link["campaign_id"])
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne introuvable")

    # Warn if host not in configured domains
    host = request.headers.get("host", "").split(":")[0]
    all_domains = settings.get_all_domains()
    valid_hostnames = [d["domain"] for d in all_domains]
    if valid_hostnames and host not in valid_hostnames:
        logger.warning("Request from unlisted host: %s (configured: %s)", host, valid_hostnames)

    # Resolve file path
    path_stripped = path.strip("/")
    if not path_stripped:
        file_rel = campaign.get("entry_file", "")
        if not file_rel:
            raise HTTPException(status_code=404, detail="Fichier d'entrée non défini pour cette campagne")
    else:
        file_rel = path_stripped

    # Security: prevent path traversal
    normalized = posixpath.normpath(file_rel)
    parts = normalized.split("/")
    if ".." in parts or normalized.startswith("..") or "//" in file_rel:
        raise HTTPException(status_code=403, detail="Chemin invalide")

    # Download from Supabase Storage
    file_bytes = storage_sb.download_file(campaign["storage_path"], file_rel)
    if file_bytes is None:
        raise HTTPException(status_code=404, detail="Fichier introuvable")

    # Record click (non-blocking)
    ua = request.headers.get("user-agent", "")
    referer = request.headers.get("referer")
    if country is None:
        country = await lookup_country(ip)
    asyncio.create_task(_record_click_and_check_alerts(link["id"], ip, ua, country, referer))

    mime = guess_inline_content_type(Path(file_rel))
    return Response(
        content=file_bytes,
        media_type=mime,
        headers={"Content-Disposition": "inline"},
    )


async def _record_click_and_check_alerts(
    link_id: int,
    ip: str,
    ua: str,
    country: Optional[str],
    referer: Optional[str],
):
    try:
        dao.record_click(link_id, ip, ua, country, referer)
        if settings.ENABLE_CLICK_ALERTS:
            await _check_click_alerts(link_id)
    except Exception as e:
        logger.warning("Error recording click: %s", e)


async def _check_click_alerts(link_id: int):
    try:
        alerts = dao.get_alerts_for_link(link_id)
        if not alerts:
            return
        stats = dao.get_link_stats(link_id)
        total = stats.get("total_clicks", 0)
        for alert in alerts:
            if not alert.get("notified") and total >= alert["threshold"]:
                from backend.telegram_bot import _bot_instance
                if _bot_instance:
                    link = dao.get_link_by_id(link_id)
                    campaign = dao.get_campaign(link["campaign_id"]) if link else None
                    for admin_id in settings.get_admin_ids():
                        try:
                            msg = (
                                f"🔔 <b>Alerte clics atteinte</b>\n\n"
                                f"Le lien <code>{link['slug'] if link else link_id}</code>"
                                f"{' (campagne ' + campaign['name'] + ')' if campaign else ''}\n"
                                f"a dépassé {alert['threshold']} clics !\n\n"
                                f"Total actuel : {total}"
                            )
                            await _bot_instance.send_message(admin_id, msg, parse_mode="HTML")
                        except Exception as e:
                            logger.warning("Could not send alert to admin %s: %s", admin_id, e)
                dao.mark_alert_notified(alert["id"])
    except Exception as e:
        logger.warning("Error checking click alerts: %s", e)


# ──────────────────────────────────────────────
# Admin router
# ──────────────────────────────────────────────

admin_router = APIRouter()


def _get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _is_secure(request: Request) -> bool:
    base = settings.PUBLIC_BASE_URL
    return base.startswith("https") or request.url.scheme == "https"


# ──────────────────────────────────────────────
# Admin HTML pages
# ──────────────────────────────────────────────

@admin_router.get("/login", include_in_schema=False)
async def serve_login():
    return _serve_html_with_prefix("login.html")


@admin_router.get("/dashboard", include_in_schema=False)
async def serve_dashboard(request: Request):
    session_token = request.cookies.get("session")
    if not session_token or not verify_session_token(session_token):
        return RedirectResponse(url=f"{settings.ADMIN_PATH_PREFIX}/login", status_code=302)
    return _serve_html_with_prefix("index.html")


# ──────────────────────────────────────────────
# Auth API
# ──────────────────────────────────────────────

@admin_router.post("/api/auth/login")
async def auth_login(body: LoginBody, request: Request):
    try:
        ip = _get_client_ip(request)

        if not login_rate_limiter.is_allowed(ip):
            retry_after = login_rate_limiter.retry_after(ip)
            logger.warning("Rate limit exceeded for login from IP %s", ip)
            return JSONResponse(
                {"detail": "Too many login attempts. Try again later."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        if not verify_web_credentials(body.username, body.password):
            logger.warning("Failed login attempt for username=%r from IP %s", body.username, ip)
            return JSONResponse({"detail": "Invalid username or password"}, status_code=401)

        logger.info("Successful login for username=%r from IP %s", body.username, ip)

        try:
            from backend.telegram_bot import send_login_success
            await send_login_success(body.username, ip)
        except RuntimeError as exc:
            logger.warning("Bot not ready for login notification (RuntimeError): %s.", exc)
        except Exception as exc:
            logger.warning("Could not send login success notification: %s", exc)

        token = create_session_token(body.username)
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


@admin_router.post("/api/auth/logout")
async def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key="session", path="/")
    return resp


@admin_router.get("/api/auth/me")
async def auth_me(payload: dict = Depends(require_session)):
    return {"username": payload.get("sub")}


# ──────────────────────────────────────────────
# Campaign / link API routes
# ──────────────────────────────────────────────

@admin_router.post("/api/upload", dependencies=[Depends(require_auth)])
async def upload_campaign(
    file: UploadFile = File(...),
    name: str = Form(...),
    force_new_version: bool = Form(False),
):
    if not name.strip():
        raise HTTPException(status_code=400, detail="Campaign name is required")

    campaign_name = name.strip()
    zip_bytes = await file.read()

    existing = dao.get_campaign_by_name(campaign_name)
    if existing and not force_new_version:
        raise HTTPException(
            status_code=409,
            detail=f"Une campagne avec ce nom existe déjà (version {existing['version']}). "
                   "Utilisez force_new_version=true pour créer une nouvelle version.",
        )

    version = (existing["version"] + 1) if existing else 1
    slug_dir = slugify(campaign_name)

    try:
        storage_path, entry_file = storage_sb.upload_campaign(zip_bytes, slug_dir, version)
    except StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))

    campaign = dao.create_campaign(
        name=campaign_name,
        storage_path=storage_path,
        entry_file=entry_file,
        original_filename=file.filename or "upload.zip",
        version=version,
    )

    if existing:
        dao.set_current_version(campaign["id"])

    return {"campaign_id": campaign["id"], "name": campaign["name"], "version": version}


@admin_router.get("/api/campaigns", dependencies=[Depends(require_auth)])
def list_campaigns():
    campaigns = dao.list_campaigns()
    return [_campaign_out(c) for c in campaigns]


@admin_router.post("/api/campaigns/{campaign_id}/links", dependencies=[Depends(require_auth)])
def create_link(campaign_id: int, body: NewLinkBody):
    campaign = dao.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne introuvable")

    domain = body.domain or None
    if domain:
        all_domains = settings.get_all_domains()
        valid_domains = [d["domain"] for d in all_domains]
        if valid_domains and domain not in valid_domains:
            raise HTTPException(
                status_code=400,
                detail=f"Domaine non configuré. Domaines disponibles: {', '.join(valid_domains)}",
            )

    slug = _unique_slug()
    link = dao.create_link(slug, campaign_id, domain)
    return {"slug": slug, "full_url": _make_full_url(slug, domain)}


@admin_router.delete("/api/links/{slug}", dependencies=[Depends(require_auth)])
def deactivate_link(slug: str):
    link = dao.get_link_by_slug(slug)
    if not link:
        raise HTTPException(status_code=404, detail="Lien introuvable")
    dao.deactivate_link(slug)
    return {"slug": slug, "is_active": False}


@admin_router.delete("/api/campaigns/{campaign_id}", dependencies=[Depends(require_auth)])
def delete_campaign(campaign_id: int):
    campaign = dao.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne introuvable")

    storage_path = campaign.get("storage_path", "")
    dao.delete_campaign(campaign_id)

    try:
        storage_sb.delete_campaign_storage(storage_path)
    except Exception as exc:
        logger.warning("Error deleting campaign storage: %s", exc)

    return {"deleted": campaign_id}


@admin_router.get("/api/domains", dependencies=[Depends(require_auth)])
def get_domains():
    all_domains = settings.get_all_domains()
    return {
        "domains": all_domains,
        "domains_simple": [d["domain"] for d in all_domains],
    }


@admin_router.get("/api/bot/status", dependencies=[Depends(require_auth)])
async def bot_status():
    if not settings.TELEGRAM_BOT_TOKEN:
        return {
            "configured": False,
            "username": None,
            "admin_ids_count": 0,
            "last_update_seconds_ago": None,
        }
    try:
        from backend.telegram_bot import _bot_instance, _bot_username
        if _bot_instance is None:
            return {
                "configured": False,
                "username": None,
                "admin_ids_count": len(settings.get_admin_ids()),
                "last_update_seconds_ago": None,
            }
        return {
            "configured": True,
            "username": _bot_username,
            "admin_ids_count": len(settings.get_admin_ids()),
            "last_update_seconds_ago": None,
        }
    except Exception as exc:
        logger.warning("Error getting bot status: %s", exc)
        return {
            "configured": False,
            "username": None,
            "admin_ids_count": len(settings.get_admin_ids()),
            "last_update_seconds_ago": None,
        }


@admin_router.post("/api/bot/test", dependencies=[Depends(require_auth)])
async def bot_test():
    if not settings.TELEGRAM_BOT_TOKEN:
        return JSONResponse(
            {"success": False, "error": "Bot non configuré (TELEGRAM_BOT_TOKEN manquant)"},
            status_code=503,
        )

    admin_ids = settings.get_admin_ids()
    if not admin_ids:
        return JSONResponse(
            {"success": False, "error": "Aucun admin configuré (TELEGRAM_ADMIN_IDS manquant)"},
            status_code=503,
        )

    try:
        from backend.telegram_bot import _bot_instance
        if _bot_instance is None:
            return JSONResponse({"success": False, "error": "Bot non démarré"}, status_code=503)

        now_str = datetime.now(tz=timezone.utc).strftime("%d/%m/%Y %H:%M %Z")
        message = (
            "✅ <b>Test depuis le dashboard Reflanex20</b>\n\n"
            "Si tu reçois ce message, le bot fonctionne correctement !\n"
            f"Heure : {now_str}"
        )

        sent = 0
        for admin_id in admin_ids:
            try:
                await _bot_instance.send_message(chat_id=admin_id, text=message, parse_mode="HTML")
                sent += 1
            except Exception as exc:
                logger.warning("Could not send test message to %s: %s", admin_id, exc)

        return {"success": True, "sent_to": sent}

    except Exception as exc:
        logger.exception("Error sending bot test: %s", exc)
        return JSONResponse(
            {"success": False, "error": "Erreur lors de l'envoi du message test"},
            status_code=503,
        )


# Include the admin router with the configured prefix
app.include_router(admin_router, prefix=settings.ADMIN_PATH_PREFIX)
