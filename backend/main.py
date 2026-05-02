import asyncio
import logging
from contextlib import asynccontextmanager
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth import (
    require_auth,
    require_session,
    verify_web_credentials,
    create_session_token,
    verify_session_token,
    get_session_secret,
)
from backend.config import settings, get_resolved_token
from backend.database import Campaign, Link, get_db, init_db
from backend.mime import guess_inline_content_type
from backend.rate_limit import login_rate_limiter
from backend.storage import StorageError, delete_campaign_files, get_file_path, validate_and_unzip
from backend.utils import generate_slug, slugify

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


def _serve_html_with_prefix(filename: str) -> HTMLResponse:
    """Serve an HTML file with __ADMIN_PREFIX__ replaced by the configured value."""
    page = FRONTEND_DIR / filename
    if not page.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found")
    content = page.read_text(encoding="utf-8")
    content = content.replace("__ADMIN_PREFIX__", settings.ADMIN_PATH_PREFIX)
    return HTMLResponse(content)


# ──────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure DB tables exist
    init_db()

    # Initialise session secret (generate + persist if needed)
    get_session_secret()

    # Log / auto-generate API token
    token = get_resolved_token()
    if not settings.API_TOKEN:
        masked = token[:4] + "..." + token[-4:]
        logger.warning(
            "🔑 API_TOKEN not set — auto-generated. "
            "Set API_TOKEN env var. Token prefix: %s",
            masked,
        )
        print(f"[reflanex20] STARTUP API_TOKEN={token}", flush=True)
    else:
        logger.info("🔑 API_TOKEN is set from environment.")

    # Boot configuration validation — log clearly what is missing
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
        # Fetch and cache bot username (best-effort)
        try:
            await set_bot_username(tg_app.bot)
        except Exception as exc:
            logger.warning("Could not cache bot username: %s", exc)
        # Configure bot commands and menu button (best-effort)
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
        await asyncio.Event().wait()  # run forever
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


# ──────────────────────────────────────────────
# App
# ──────────────────────────────────────────────

app = FastAPI(title="Reflanex20", version="1.0.0", lifespan=lifespan)

# Static frontend
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ──────────────────────────────────────────────
# Global exception handler — always return JSON
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
    clicks: int
    is_active: bool
    full_url: str

    class Config:
        from_attributes = True


class CampaignOut(BaseModel):
    id: int
    name: str
    original_filename: str
    created_at: str
    storage_path: str
    entry_file: str
    links: List[LinkOut] = []

    class Config:
        from_attributes = True


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


def _link_out(link: Link) -> LinkOut:
    return LinkOut(
        id=link.id,
        slug=link.slug,
        domain=link.domain,
        clicks=link.clicks,
        is_active=link.is_active,
        full_url=_make_full_url(link.slug, link.domain),
    )


def _campaign_out(c: Campaign) -> CampaignOut:
    return CampaignOut(
        id=c.id,
        name=c.name,
        original_filename=c.original_filename,
        created_at=c.created_at.isoformat() if c.created_at else "",
        storage_path=c.storage_path,
        entry_file=c.entry_file,
        links=[_link_out(l) for l in c.links],
    )


def _unique_slug(db: Session) -> str:
    for _ in range(20):
        slug = generate_slug()
        if not db.query(Link).filter(Link.slug == slug).first():
            return slug
    raise HTTPException(status_code=500, detail="Could not generate unique slug")


# ──────────────────────────────────────────────
# Public routes (always at root)
# ──────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/diag")
async def diag(db: Session = Depends(get_db)):
    """Public diagnostic endpoint — returns config status without exposing secrets."""
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
        "telegram_bot": telegram_status,
        "telegram_admins_count": len(admin_ids),
        "session_secret": "ok" if session_ok else "missing",
        "domains_count": len(domains),
        "public_base_url": settings.PUBLIC_BASE_URL,
        "version": "1.0.0",
    }


@app.get("/", include_in_schema=False)
async def serve_root():
    """Root returns a generic 404 — no hint that an admin panel exists."""
    return JSONResponse({"detail": "Page introuvable"}, status_code=404)


@app.get("/c/{slug}", include_in_schema=False)
@app.get("/c/{slug}/", include_in_schema=False)
async def serve_slug_root(slug: str, request: Request, db: Session = Depends(get_db)):
    return await _serve_campaign_file(slug, "", request, db)


@app.get("/c/{slug}/{path:path}", include_in_schema=False)
async def serve_slug_path(slug: str, path: str, request: Request, db: Session = Depends(get_db)):
    return await _serve_campaign_file(slug, path, request, db)


async def _serve_campaign_file(slug: str, path: str, request: Request, db: Session):
    link: Optional[Link] = db.query(Link).filter(Link.slug == slug).first()
    if not link or not link.is_active:
        raise HTTPException(status_code=404, detail="Lien introuvable ou désactivé")

    # Warn if host not in configured domains
    host = request.headers.get("host", "").split(":")[0]
    all_domains = settings.get_all_domains()
    valid_hostnames = [d["domain"] for d in all_domains]
    if valid_hostnames and host not in valid_hostnames:
        logger.warning("Request from unlisted host: %s (configured: %s)", host, valid_hostnames)

    campaign: Optional[Campaign] = link.campaign
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne introuvable")

    # Increment clicks
    link.clicks += 1
    db.commit()

    # Resolve file
    path_stripped = path.strip("/")
    if not path_stripped:
        # Root access — use entry_file
        if not campaign.entry_file:
            raise HTTPException(
                status_code=404,
                detail="Fichier d'entrée non défini pour cette campagne",
            )
        file_rel = campaign.entry_file
    else:
        file_rel = path_stripped

    file_path = get_file_path(campaign.storage_path, file_rel)

    if file_path is None:
        raise HTTPException(status_code=404, detail="Fichier introuvable")

    # Detect MIME and always serve inline (never force download)
    mime = guess_inline_content_type(file_path)
    return Response(
        content=file_path.read_bytes(),
        media_type=mime,
        headers={"Content-Disposition": "inline"},
    )


# ──────────────────────────────────────────────
# Admin router — all routes behind ADMIN_PATH_PREFIX
# ──────────────────────────────────────────────

admin_router = APIRouter()


# ──────────────────────────────────────────────
# Auth helpers
# ──────────────────────────────────────────────

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
        return RedirectResponse(
            url=f"{settings.ADMIN_PATH_PREFIX}/login", status_code=302
        )
    return _serve_html_with_prefix("index.html")


# ──────────────────────────────────────────────
# Auth API
# ──────────────────────────────────────────────

@admin_router.post("/api/auth/login")
async def auth_login(body: LoginBody, request: Request):
    try:
        ip = _get_client_ip(request)

        # Rate limit per IP
        if not login_rate_limiter.is_allowed(ip):
            retry_after = login_rate_limiter.retry_after(ip)
            logger.warning("Rate limit exceeded for login from IP %s", ip)
            return JSONResponse(
                {"detail": "Too many login attempts. Try again later."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        # Verify credentials (constant-time)
        if not verify_web_credentials(body.username, body.password):
            logger.warning("Failed login attempt for username=%r from IP %s", body.username, ip)
            return JSONResponse({"detail": "Invalid username or password"}, status_code=401)

        logger.info("Successful login for username=%r from IP %s", body.username, ip)

        # Send success notification (best-effort — never blocks login)
        try:
            from backend.telegram_bot import send_login_success
            await send_login_success(body.username, ip)
        except RuntimeError as exc:
            logger.warning(
                "Bot not ready for login notification (RuntimeError): %s. "
                "Vérifiez TELEGRAM_BOT_TOKEN et redéployez.",
                exc,
            )
        except Exception as exc:
            logger.warning("Could not send login success notification: %s", exc)

        # Issue session JWT cookie
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
    db: Session = Depends(get_db),
):
    if not name.strip():
        raise HTTPException(status_code=400, detail="Campaign name is required")

    if db.query(Campaign).filter(Campaign.name == name.strip()).first():
        raise HTTPException(status_code=409, detail="Une campagne avec ce nom existe déjà")

    zip_bytes = await file.read()
    try:
        slug_dir = slugify(name.strip())
        storage_path, entry_file = validate_and_unzip(zip_bytes, slug_dir)
    except StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))

    campaign = Campaign(
        name=name.strip(),
        original_filename=file.filename or "upload.zip",
        storage_path=storage_path,
        entry_file=entry_file,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)

    return {"campaign_id": campaign.id, "name": campaign.name}


@admin_router.get("/api/campaigns", dependencies=[Depends(require_auth)])
def list_campaigns(db: Session = Depends(get_db)):
    campaigns = db.query(Campaign).all()
    return [_campaign_out(c) for c in campaigns]


@admin_router.post("/api/campaigns/{campaign_id}/links", dependencies=[Depends(require_auth)])
def create_link(
    campaign_id: int,
    body: NewLinkBody,
    db: Session = Depends(get_db),
):
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
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

    slug = _unique_slug(db)
    link = Link(slug=slug, campaign_id=campaign.id, domain=domain)
    db.add(link)
    db.commit()
    db.refresh(link)

    return {"slug": slug, "full_url": _make_full_url(slug, domain)}


@admin_router.delete("/api/links/{slug}", dependencies=[Depends(require_auth)])
def deactivate_link(slug: str, db: Session = Depends(get_db)):
    link = db.query(Link).filter(Link.slug == slug).first()
    if not link:
        raise HTTPException(status_code=404, detail="Lien introuvable")
    link.is_active = False
    db.commit()
    return {"slug": slug, "is_active": False}


@admin_router.delete("/api/campaigns/{campaign_id}", dependencies=[Depends(require_auth)])
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne introuvable")

    storage_path = campaign.storage_path
    db.delete(campaign)
    db.commit()

    delete_campaign_files(storage_path)
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
    """Returns Telegram bot status information."""
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
    """Send a test message to all Telegram admins."""
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
            return JSONResponse(
                {"success": False, "error": "Bot non démarré"},
                status_code=503,
            )

        from datetime import datetime, timezone
        now_str = datetime.now(tz=timezone.utc).strftime("%d/%m/%Y %H:%M %Z")
        message = (
            "✅ <b>Test depuis le dashboard Reflanex20</b>\n\n"
            "Si tu reçois ce message, le bot fonctionne correctement !\n"
            f"Heure : {now_str}"
        )

        sent = 0
        for admin_id in admin_ids:
            try:
                await _bot_instance.send_message(
                    chat_id=admin_id,
                    text=message,
                    parse_mode="HTML",
                )
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
