import io
import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from backend.auth import is_telegram_admin
from backend.config import settings
from backend.database import Campaign, Link, SessionLocal
from backend.storage import StorageError, validate_and_unzip
from backend.utils import generate_slug, slugify

logger = logging.getLogger(__name__)

# Module-level bot instance set once the application is built
_bot_instance: Optional[Bot] = None

# States for conversation-like flow (stored in user_data)
WAITING_FOR_ZIP = "waiting_for_zip"


# ──────────────────────────────────────────────
# Auth guard
# ──────────────────────────────────────────────

async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None or not is_telegram_admin(user.id):
        await update.effective_message.reply_text("🚫 Accès refusé.")
        return False
    return True


# ──────────────────────────────────────────────
# /start  /help
# ──────────────────────────────────────────────

HELP_TEXT = """
📋 <b>Reflanex20 — Commandes disponibles</b>

/upload — Préparer l'upload d'un zip de campagne
/list — Lister les campagnes
/newlink &lt;campaign_id&gt; [domain] — Générer un nouveau lien
/setdomain &lt;slug&gt; &lt;domain&gt; — Changer le domaine d'un lien
/delete &lt;slug&gt; — Désactiver un lien
/domains — Lister les domaines configurés
/stats &lt;campaign_id&gt; — Stats d'une campagne
"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    await update.message.reply_html(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    await update.message.reply_html(HELP_TEXT)


# ──────────────────────────────────────────────
# /upload
# ──────────────────────────────────────────────

async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    context.user_data[WAITING_FOR_ZIP] = True
    await update.message.reply_text(
        "📤 Envoie-moi maintenant le fichier .zip.\n"
        "La légende (caption) du fichier sera utilisée comme nom de campagne."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        if context.user_data.get(WAITING_FOR_ZIP):
            await update.message.reply_text("⚠️ Merci d'envoyer un fichier .zip.")
        return

    if not context.user_data.get(WAITING_FOR_ZIP):
        await update.message.reply_text(
            "ℹ️ Envoie /upload d'abord si tu veux uploader une campagne."
        )
        return

    context.user_data[WAITING_FOR_ZIP] = False

    caption = (update.message.caption or "").strip()
    if not caption:
        await update.message.reply_text(
            "⚠️ Merci d'ajouter une légende (caption) au fichier avec le nom de la campagne."
        )
        return

    await update.message.reply_text("⏳ Téléchargement et traitement du zip…")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        zip_bytes = buf.getvalue()
    except Exception as e:
        logger.error("Error downloading zip from Telegram: %s", e)
        await update.message.reply_text(f"❌ Erreur lors du téléchargement : {e}")
        return

    db = SessionLocal()
    try:
        name = caption
        if db.query(Campaign).filter(Campaign.name == name).first():
            await update.message.reply_text(
                f"❌ Une campagne avec le nom « {name} » existe déjà."
            )
            return

        slug_dir = slugify(name)
        try:
            storage_path, entry_file = validate_and_unzip(zip_bytes, slug_dir)
        except StorageError as e:
            await update.message.reply_text(f"❌ Erreur : {e}")
            return

        campaign = Campaign(
            name=name,
            original_filename=doc.file_name,
            storage_path=storage_path,
            entry_file=entry_file,
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)

        await update.message.reply_text(
            f"✅ Campagne <b>{campaign.name}</b> créée (ID: {campaign.id}).\n"
            f"Fichier d'entrée : <code>{entry_file}</code>\n\n"
            f"Génère un lien avec : /newlink {campaign.id}",
            parse_mode="HTML",
        )
    finally:
        db.close()


# ──────────────────────────────────────────────
# /list
# ──────────────────────────────────────────────

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    db = SessionLocal()
    try:
        campaigns = db.query(Campaign).all()
        if not campaigns:
            await update.message.reply_text("Aucune campagne pour l'instant.")
            return
        lines = []
        for c in campaigns:
            active_links = sum(1 for l in c.links if l.is_active)
            lines.append(f"• <b>{c.name}</b> (ID: {c.id}) — {active_links} lien(s) actif(s)")
        await update.message.reply_html("📋 <b>Campagnes :</b>\n" + "\n".join(lines))
    finally:
        db.close()


# ──────────────────────────────────────────────
# /newlink <campaign_id> [domain]
# ──────────────────────────────────────────────

async def cmd_newlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage : /newlink <campaign_id> [domain]")
        return

    try:
        campaign_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ campaign_id doit être un entier.")
        return

    domain: Optional[str] = args[1] if len(args) > 1 else None

    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        if not campaign:
            await update.message.reply_text(f"❌ Campagne {campaign_id} introuvable.")
            return

        configured_domains = settings.get_domains()

        if domain is None and configured_domains:
            # Propose inline keyboard
            buttons = [
                [InlineKeyboardButton(d, callback_data=f"newlink:{campaign_id}:{d}")]
                for d in configured_domains
            ]
            buttons.append(
                [InlineKeyboardButton("Sans domaine (URL par défaut)", callback_data=f"newlink:{campaign_id}:")]
            )
            await update.message.reply_text(
                f"Choisis un domaine pour la campagne <b>{campaign.name}</b> :",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML",
            )
            return

        # Generate link directly
        link = _create_link_for_campaign(db, campaign, domain)
        full_url = _make_full_url(link.slug, link.domain)
        await update.message.reply_html(
            f"🔗 Nouveau lien généré :\n<code>{full_url}</code>\n\nSlug : <code>{link.slug}</code>"
        )
    finally:
        db.close()


async def callback_newlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_telegram_admin(query.from_user.id):
        await query.edit_message_text("🚫 Accès refusé.")
        return

    data = query.data  # "newlink:<campaign_id>:<domain>"
    parts = data.split(":", 2)
    if len(parts) < 3:
        return

    try:
        campaign_id = int(parts[1])
    except ValueError:
        return
    domain = parts[2] or None

    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        if not campaign:
            await query.edit_message_text("❌ Campagne introuvable.")
            return

        link = _create_link_for_campaign(db, campaign, domain)
        full_url = _make_full_url(link.slug, link.domain)
        await query.edit_message_text(
            f"🔗 Nouveau lien généré :\n{full_url}\n\nSlug : {link.slug}"
        )
    finally:
        db.close()


def _create_link_for_campaign(db, campaign: Campaign, domain: Optional[str]) -> Link:
    for _ in range(20):
        slug = generate_slug()
        if not db.query(Link).filter(Link.slug == slug).first():
            break
    else:
        raise RuntimeError("Could not generate unique slug")

    link = Link(slug=slug, campaign_id=campaign.id, domain=domain)
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def _make_full_url(slug: str, domain: Optional[str]) -> str:
    if domain:
        return f"https://{domain}/c/{slug}/"
    return f"{settings.PUBLIC_BASE_URL}/c/{slug}/"


# ──────────────────────────────────────────────
# /setdomain <slug> <domain>
# ──────────────────────────────────────────────

async def cmd_setdomain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage : /setdomain <slug> <domain>")
        return

    slug, domain = args[0], args[1]
    db = SessionLocal()
    try:
        link = db.query(Link).filter(Link.slug == slug).first()
        if not link:
            await update.message.reply_text(f"❌ Lien {slug} introuvable.")
            return
        link.domain = domain
        db.commit()
        full_url = _make_full_url(link.slug, link.domain)
        await update.message.reply_html(
            f"✅ Domaine mis à jour.\n🔗 <code>{full_url}</code>"
        )
    finally:
        db.close()


# ──────────────────────────────────────────────
# /delete <slug>
# ──────────────────────────────────────────────

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage : /delete <slug>")
        return

    slug = args[0]
    db = SessionLocal()
    try:
        link = db.query(Link).filter(Link.slug == slug).first()
        if not link:
            await update.message.reply_text(f"❌ Lien {slug} introuvable.")
            return
        link.is_active = False
        db.commit()
        await update.message.reply_text(f"✅ Lien <code>{slug}</code> désactivé.", parse_mode="HTML")
    finally:
        db.close()


# ──────────────────────────────────────────────
# /domains
# ──────────────────────────────────────────────

async def cmd_domains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    domains = settings.get_domains()
    if not domains:
        await update.message.reply_text("Aucun domaine configuré (var DOMAINS vide).")
    else:
        await update.message.reply_text("🌐 Domaines configurés :\n" + "\n".join(f"• {d}" for d in domains))


# ──────────────────────────────────────────────
# /stats <campaign_id>
# ──────────────────────────────────────────────

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage : /stats <campaign_id>")
        return

    try:
        campaign_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ campaign_id doit être un entier.")
        return

    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        if not campaign:
            await update.message.reply_text(f"❌ Campagne {campaign_id} introuvable.")
            return

        lines = [f"📊 <b>{campaign.name}</b> (ID: {campaign.id})\n"]
        for link in campaign.links:
            status_icon = "🟢" if link.is_active else "🔴"
            domain_label = link.domain or "(défaut)"
            lines.append(
                f"{status_icon} <code>{link.slug}</code> — {domain_label} — {link.clicks} clic(s)"
            )
        if not campaign.links:
            lines.append("Aucun lien pour cette campagne.")

        await update.message.reply_html("\n".join(lines))
    finally:
        db.close()


# ──────────────────────────────────────────────
# Build application
# ──────────────────────────────────────────────

def build_application() -> Application:
    global _bot_instance
    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )
    _bot_instance = app.bot

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("newlink", cmd_newlink))
    app.add_handler(CommandHandler("setdomain", cmd_setdomain))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("domains", cmd_domains))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(callback_newlink, pattern=r"^newlink:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    return app


# ──────────────────────────────────────────────
# Auth notification helpers
# ──────────────────────────────────────────────

async def send_login_success(username: str, ip: str) -> None:
    """Notify all admins of a successful login (best-effort, does not raise)."""
    if _bot_instance is None:
        return

    admin_ids = settings.get_admin_ids()
    if not admin_ids:
        return

    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = (
        f"✅ <b>Connexion réussie</b>\n\n"
        f"Utilisateur : <code>{username}</code>\n"
        f"IP : <code>{ip}</code>\n"
        f"Heure : {now_str}"
    )

    for admin_id in admin_ids:
        try:
            await _bot_instance.send_message(
                chat_id=admin_id,
                text=message,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.warning("Could not send login-success notification to %s: %s", admin_id, exc)
