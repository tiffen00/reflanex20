import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    UploadFile,
    File,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth import (
    require_api_token,
    require_auth,
    require_session,
    verify_web_credentials,
    create_session_token,
    verify_session_token,
    get_session_secret,
)
from backend.config import settings, get_resolved_token
from backend.database import Campaign, Link, get_db, init_db
from backend.rate_limit import login_rate_limiter
from backend.storage import StorageError, delete_campaign_files, get_file_path, validate_and_unzip
from backend.utils import generate_slug, slugify

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


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
        # Intentionally printed (not logged) to stdout once so the operator
        # can retrieve the token from service logs and set API_TOKEN as an env var.
        masked = token[:4] + "..." + token[-4:]
        logger.warning(
            "🔑 API_TOKEN not set — auto-generated. "
            "Set API_TOKEN env var. Token prefix: %s",
            masked,
        )
        # Print full token once to stdout so it's visible in deployment logs
        print(f"[reflanex20] STARTUP API_TOKEN={token}", flush=True)
    else:
        logger.info("🔑 API_TOKEN is set from environment.")

    # Warn if WEB_PASSWORD is not set
    if not settings.WEB_PASSWORD:
        logger.warning(
            "⚠️  WEB_PASSWORD not set — web login will be disabled until set in env."
        )

    # Log Telegram bot token and admin IDs presence (never log actual values)
    if settings.TELEGRAM_BOT_TOKEN:
        logger.info("🤖 TELEGRAM_BOT_TOKEN: set")
    else:
        logger.warning("🤖 TELEGRAM_BOT_TOKEN: not set — bot will be disabled")
    admin_ids = settings.get_admin_ids()
    if admin_ids:
        logger.info("👥 TELEGRAM_ADMIN_IDS: set (%d admin(s))", len(admin_ids))
    else:
        logger.warning("👥 TELEGRAM_ADMIN_IDS: not set — no admin will receive login notifications")

    # Start Telegram bot if token is configured
    bot_task = None
    if settings.TELEGRAM_BOT_TOKEN:
        from backend.telegram_bot import build_application
        tg_app = build_application()
        bot_task = asyncio.create_task(_run_bot(tg_app))
        logger.info("🤖 Telegram bot started.")
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
    logger.exception("Unhandled exception on %s: %s", request.url.path, exc)
    return JSONResponse(
        {"detail": "Erreur serveur interne"},
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
# Public routes
# ──────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/diag")
async def diag(db: Session = Depends(get_db)):
    """Public diagnostic endpoint — returns config status without exposing secrets."""
    # Telegram bot status
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


@app.get("/")
async def serve_frontend(request: Request):
    # Redirect to login if no valid session
    session_token = request.cookies.get("session")
    if not session_token or not verify_session_token(session_token):
        return RedirectResponse(url="/login", status_code=302)
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"status": "ok", "message": "Reflanex20 API"})


@app.get("/login")
async def serve_login():
    page = FRONTEND_DIR / "login.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"error": "login.html not found"}, status_code=404)


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
    configured = settings.get_domains()
    if configured and host not in configured:
        logger.warning("Request from unlisted host: %s (configured: %s)", host, configured)

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

    return FileResponse(str(file_path))


# ──────────────────────────────────────────────
# Auth API routes
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


@app.post("/api/auth/login")
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
        return JSONResponse({"detail": "Erreur serveur"}, status_code=500)


@app.post("/api/auth/logout")
async def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key="session", path="/")
    return resp


@app.get("/api/auth/me")
async def auth_me(payload: dict = Depends(require_session)):
    return {"username": payload.get("sub")}


# ──────────────────────────────────────────────
# API routes
# ──────────────────────────────────────────────

@app.post("/api/upload", dependencies=[Depends(require_auth)])
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


@app.get("/api/campaigns", dependencies=[Depends(require_auth)])
def list_campaigns(db: Session = Depends(get_db)):
    campaigns = db.query(Campaign).all()
    return [_campaign_out(c) for c in campaigns]


@app.post("/api/campaigns/{campaign_id}/links", dependencies=[Depends(require_auth)])
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
        configured = settings.get_domains()
        if configured and domain not in configured:
            raise HTTPException(
                status_code=400,
                detail=f"Domaine non configuré. Domaines disponibles: {', '.join(configured)}",
            )

    slug = _unique_slug(db)
    link = Link(slug=slug, campaign_id=campaign.id, domain=domain)
    db.add(link)
    db.commit()
    db.refresh(link)

    return {"slug": slug, "full_url": _make_full_url(slug, domain)}


@app.delete("/api/links/{slug}", dependencies=[Depends(require_auth)])
def deactivate_link(slug: str, db: Session = Depends(get_db)):
    link = db.query(Link).filter(Link.slug == slug).first()
    if not link:
        raise HTTPException(status_code=404, detail="Lien introuvable")
    link.is_active = False
    db.commit()
    return {"slug": slug, "is_active": False}


@app.delete("/api/campaigns/{campaign_id}", dependencies=[Depends(require_auth)])
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne introuvable")

    storage_path = campaign.storage_path
    db.delete(campaign)
    db.commit()

    delete_campaign_files(storage_path)
    return {"deleted": campaign_id}


@app.get("/api/domains", dependencies=[Depends(require_auth)])
def get_domains():
    return {"domains": settings.get_domains()}
