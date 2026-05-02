import io
import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.constants import ParseMode
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

# Module-level bot instance and username set once the application is built
_bot_instance: Optional[Bot] = None
_bot_username: Optional[str] = None

# States stored in context.user_data
WAITING_FOR_ZIP = "waiting_for_zip"
PENDING_CAMPAIGN_ID = "pending_campaign_id"


def _admin_url() -> str:
    """Return the full admin login URL, avoiding double slashes."""
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    prefix = settings.ADMIN_PATH_PREFIX.rstrip("/")
    return f"{base}{prefix}/login"

# ──────────────────────────────────────────────
# Centralized message texts
# ──────────────────────────────────────────────

MESSAGES = {
    "main_menu": (
        "👋 <b>Bienvenue sur Reflanex20</b>\n\n"
        "Voici ce que je peux faire pour toi :\n\n"
        "🔗 <b>Générer un lien</b> — utilise une campagne déjà uploadée\n"
        "📋 Voir mes campagnes\n"
        "📤 Uploader une nouvelle campagne (zip)\n"
        "🌐 Mes domaines disponibles\n"
        "📊 Statistiques d'une campagne\n"
        "❓ Aide\n\n"
        "Choisis une option ci-dessous :"
    ),
    "upload_start": (
        "📤 <b>Nouvelle campagne</b>\n\n"
        "Tu vas uploader un NOUVEAU contenu (zip).\n\n"
        "💡 <b>Si tu veux juste un nouveau lien pour une campagne déjà uploadée</b>, "
        "utilise plutôt <i>🔗 Générer un lien</i> depuis le menu.\n\n"
        "Pour continuer, envoie-moi le fichier <b>.zip</b> avec le nom de la campagne "
        "dans la <b>légende (caption)</b>.\n"
        "    <i>Exemple : promo-noel-2026</i>\n\n"
        "⚠️ Le zip peut contenir HTML, PHP, images, JS, CSS... (max 50 MB)\n\n"
        "Annule avec /cancel"
    ),
    "no_campaigns": (
        "📋 <b>Aucune campagne</b>\n\n"
        "Tu n'as pas encore de campagne.\n"
        "Commence par uploader un zip !"
    ),
    "help": (
        "❓ <b>Aide — Reflanex20</b>\n\n"
        "<b>🚀 Démarrage</b>\n"
        "• Tape /start ou /menu pour afficher le menu principal\n"
        "• Tous les flows se font via les boutons — pas besoin de mémoriser de commandes !\n\n"
        "<b>🔗 Générer un lien (action la plus fréquente)</b>\n"
        "• Clique sur <i>🔗 Générer un lien</i> depuis le menu principal\n"
        "• Choisis la campagne existante puis le domaine\n"
        "• Copie le lien et partage-le !\n"
        "• Tu peux répéter cette action pour obtenir de nouveaux slugs sans re-uploader.\n\n"
        "<b>📤 Uploader une campagne</b>\n"
        "• Clique sur <i>Nouvelle campagne</i> <b>seulement</b> si tu as un nouveau contenu à uploader\n"
        "• Envoie ton fichier .zip <b>avec une légende</b> (le nom de ta campagne)\n"
        "• Le zip peut contenir HTML, PHP, images, JS, CSS... tout est accepté\n\n"
        "<b>♻️ Pourquoi re-générer un lien ?</b>\n"
        "Si ton lien est <i>cramé</i> (bloqué par les plateformes), génère un nouveau slug pour\n"
        "la même campagne — tu auras une nouvelle URL propre tout en gardant ton contenu.\n\n"
        "<b>🌐 Domaines custom</b>\n"
        "• Le domaine public Render est toujours disponible gratuitement\n"
        "• Pour ajouter un domaine custom :\n"
        "  1. Va sur Render → ton service → Settings → Custom Domain\n"
        "  2. Configure le DNS (CNAME) chez ton registrar\n"
        "  3. Ajoute le domaine dans la variable <code>DOMAINS</code> sur Render\n"
        "  4. Redéploie le service\n\n"
        "<b>❓ Besoin d'aide ?</b>\n"
        "Contacte l'administrateur ou consulte la documentation Render."
    ),
}


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
# Keyboard builders
# ──────────────────────────────────────────────

def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔗 Générer un lien (campagne existante)", callback_data="menu:newlink"),
        ],
        [
            InlineKeyboardButton("📋 Mes campagnes", callback_data="menu:campaigns"),
            InlineKeyboardButton("📤 Nouvelle campagne", callback_data="menu:upload"),
        ],
        [
            InlineKeyboardButton("🌐 Domaines", callback_data="menu:domains"),
            InlineKeyboardButton("📊 Statistiques", callback_data="menu:stats"),
        ],
        [
            InlineKeyboardButton("🔐 URL Admin", callback_data="menu:admin"),
            InlineKeyboardButton("❓ Aide", callback_data="menu:help"),
        ],
    ])


def _back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")]
    ])


def _campaigns_keyboard(campaigns, callback_prefix: str = "campaign:detail") -> InlineKeyboardMarkup:
    buttons = []
    for c in campaigns:
        active_links = sum(1 for l in c.links if l.is_active)
        label = f"📁 {c.name} ({active_links} lien{'s' if active_links != 1 else ''})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{c.id}")])
    buttons.append([InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)


def _campaign_detail_keyboard(campaign_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Nouveau lien", callback_data=f"link:new:{campaign_id}")],
        [InlineKeyboardButton("📊 Statistiques", callback_data=f"stats:show:{campaign_id}")],
        [InlineKeyboardButton("🗑 Supprimer la campagne", callback_data=f"campaign:delete:{campaign_id}")],
        [InlineKeyboardButton("◀ Retour", callback_data="menu:campaigns")],
    ])


def _domain_keyboard(campaign_id: int) -> InlineKeyboardMarkup:
    all_domains = settings.get_all_domains()
    buttons = []
    for d in all_domains:
        label = f"🌟 {d['domain']} (par défaut)" if d["is_default"] else d["domain"]
        domain_val = d["domain"]
        buttons.append([InlineKeyboardButton(label, callback_data=f"link:gen:{campaign_id}:{domain_val}")])
    buttons.append([InlineKeyboardButton("◀ Retour", callback_data=f"campaign:detail:{campaign_id}")])
    return InlineKeyboardMarkup(buttons)


# ──────────────────────────────────────────────
# /start  /menu  (and backward compat /help)
# ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    context.user_data.clear()
    await update.message.reply_html(
        MESSAGES["main_menu"],
        reply_markup=_main_menu_keyboard(),
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    context.user_data.clear()
    await update.message.reply_html(
        MESSAGES["main_menu"],
        reply_markup=_main_menu_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    await update.message.reply_html(
        MESSAGES["help"],
        reply_markup=_back_to_menu_keyboard(),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    context.user_data.clear()
    await update.message.reply_html(
        "❌ Action annulée.\n\n" + MESSAGES["main_menu"],
        reply_markup=_main_menu_keyboard(),
    )


# ──────────────────────────────────────────────
# Main menu callback dispatcher
# ──────────────────────────────────────────────

async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_telegram_admin(query.from_user.id):
        await query.edit_message_text("🚫 Accès refusé.")
        return

    action = query.data.split(":", 1)[1] if ":" in query.data else ""
    logger.info("Callback menu:%s from user %s", action, query.from_user.id)

    if action == "main":
        context.user_data.clear()
        await query.edit_message_text(
            MESSAGES["main_menu"],
            reply_markup=_main_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    elif action == "upload":
        context.user_data[WAITING_FOR_ZIP] = True
        await query.edit_message_text(
            MESSAGES["upload_start"],
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩️ Plutôt générer un lien existant", callback_data="menu:newlink"),
                InlineKeyboardButton("❌ Annuler", callback_data="menu:main"),
            ]]),
            parse_mode=ParseMode.HTML,
        )

    elif action == "campaigns":
        db = SessionLocal()
        try:
            campaigns = db.query(Campaign).all()
        finally:
            db.close()
        if not campaigns:
            await query.edit_message_text(
                MESSAGES["no_campaigns"],
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📤 Uploader", callback_data="menu:upload"),
                    InlineKeyboardButton("◀ Menu", callback_data="menu:main"),
                ]]),
                parse_mode=ParseMode.HTML,
            )
            return
        await query.edit_message_text(
            f"📋 <b>Tes campagnes ({len(campaigns)})</b>\n\nClique sur une campagne pour voir ses détails et liens :",
            reply_markup=_campaigns_keyboard(campaigns),
            parse_mode=ParseMode.HTML,
        )

    elif action == "newlink":
        db = SessionLocal()
        try:
            campaigns = db.query(Campaign).all()
        finally:
            db.close()
        if not campaigns:
            await query.edit_message_text(
                MESSAGES["no_campaigns"],
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📤 Uploader", callback_data="menu:upload"),
                    InlineKeyboardButton("◀ Menu", callback_data="menu:main"),
                ]]),
                parse_mode=ParseMode.HTML,
            )
            return
        await query.edit_message_text(
            "🔗 <b>Générer un lien</b>\n\nChoisis la campagne pour laquelle tu veux générer un lien :",
            reply_markup=_campaigns_keyboard(campaigns, callback_prefix="link:new"),
            parse_mode=ParseMode.HTML,
        )

    elif action == "domains":
        all_domains = settings.get_all_domains()
        lines = []
        for d in all_domains:
            if d["is_default"]:
                lines.append(f"🌟 <code>{d['domain']}</code>  <i>(domaine public Render, toujours disponible)</i>")
            else:
                lines.append(f"✓ <code>{d['domain']}</code>")
        domains_text = "\n".join(lines) if lines else "Aucun domaine configuré."
        msg = (
            f"🌐 <b>Tes domaines disponibles</b>\n\n"
            f"{domains_text}\n\n"
            "ℹ️ <b>Pour ajouter un domaine custom :</b>\n"
            "1. Va sur Render → ton service → Settings → Custom Domain\n"
            "2. Ajoute ton domaine et configure le DNS (CNAME)\n"
            "3. Mets à jour la variable d'env <code>DOMAINS</code> sur Render\n\n"
            "Ces domaines sont configurés via la variable <code>DOMAINS</code> dans Render."
        )
        await query.edit_message_text(
            msg,
            reply_markup=_back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    elif action == "stats":
        try:
            db = SessionLocal()
            try:
                campaigns = db.query(Campaign).all()
            finally:
                db.close()
            if not campaigns:
                await query.edit_message_text(
                    MESSAGES["no_campaigns"],
                    reply_markup=_back_to_menu_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
                return
            await query.edit_message_text(
                "📊 <b>Statistiques</b>\n\nChoisis une campagne :",
                reply_markup=_campaigns_keyboard(campaigns, callback_prefix="stats:show"),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("callback_menu stats error: %s", exc)
            await query.edit_message_text(
                f"⚠️ Erreur lors du chargement des statistiques.\nDétail : {exc}",
                reply_markup=_back_to_menu_keyboard(),
            )

    elif action == "help":
        await query.edit_message_text(
            MESSAGES["help"],
            reply_markup=_back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    elif action == "admin":
        admin_url = _admin_url()
        msg = (
            "🔐 <b>URL d'administration web</b>\n\n"
            f"<code>{admin_url}</code>\n\n"
            "⚠️ Garde cette URL privée. Ne la partage avec personne."
        )
        await query.edit_message_text(
            msg,
            reply_markup=_back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )


# ──────────────────────────────────────────────
# Campaign callbacks
# ──────────────────────────────────────────────

async def callback_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_telegram_admin(query.from_user.id):
        await query.edit_message_text("🚫 Accès refusé.")
        return

    parts = query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    param = parts[2] if len(parts) > 2 else ""
    logger.info("Callback campaign:%s param=%s from user %s", action, param, query.from_user.id)

    if action == "detail":
        campaign_id = int(param)
        db = SessionLocal()
        try:
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            active_links = sum(1 for l in campaign.links if l.is_active)
            total_links = len(campaign.links)
            created = campaign.created_at.strftime("%d/%m/%Y") if campaign.created_at else "—"
            msg = (
                f"📁 <b>{campaign.name}</b>\n\n"
                f"🆔 ID : {campaign.id}\n"
                f"📅 Créée le : {created}\n"
                f"📄 Fichier d'entrée : <code>{campaign.entry_file or '—'}</code>\n"
                f"🔗 Liens actifs : {active_links} / {total_links}\n\n"
                "Que veux-tu faire ?"
            )
        finally:
            db.close()
        await query.edit_message_text(
            msg,
            reply_markup=_campaign_detail_keyboard(campaign_id),
            parse_mode=ParseMode.HTML,
        )

    elif action == "delete":
        campaign_id = int(param)
        db = SessionLocal()
        try:
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            active_links = sum(1 for l in campaign.links if l.is_active)
            total_links = len(campaign.links)
            msg = (
                f"⚠️ <b>Confirmer la suppression</b>\n\n"
                f"Tu vas supprimer la campagne <b>{campaign.name}</b> :\n"
                f"- {total_links} lien(s) seront désactivés\n"
                "- Tous les fichiers seront effacés\n"
                "- Cette action est <b>IRRÉVERSIBLE</b>\n\n"
                "Confirmer ?"
            )
        finally:
            db.close()
        await query.edit_message_text(
            msg,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Oui, supprimer", callback_data=f"campaign:delete_confirm:{campaign_id}"),
                    InlineKeyboardButton("❌ Annuler", callback_data=f"campaign:detail:{campaign_id}"),
                ]
            ]),
            parse_mode=ParseMode.HTML,
        )

    elif action == "delete_confirm":
        campaign_id = int(param)
        db = SessionLocal()
        try:
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            name = campaign.name
            storage_path = campaign.storage_path
            db.delete(campaign)
            db.commit()
        finally:
            db.close()

        try:
            from backend.storage import delete_campaign_files
            delete_campaign_files(storage_path)
        except Exception as exc:
            logger.warning("Error deleting campaign files: %s", exc)

        await query.edit_message_text(
            f"✅ Campagne <b>{name}</b> supprimée avec succès.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Mes campagnes", callback_data="menu:campaigns")],
                [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")],
            ]),
            parse_mode=ParseMode.HTML,
        )


# ──────────────────────────────────────────────
# Link callbacks
# ──────────────────────────────────────────────

async def callback_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_telegram_admin(query.from_user.id):
        await query.edit_message_text("🚫 Accès refusé.")
        return

    parts = query.data.split(":", 3)
    action = parts[1] if len(parts) > 1 else ""
    logger.info("Callback link:%s from user %s", action, query.from_user.id)

    if action == "new":
        campaign_id = int(parts[2]) if len(parts) > 2 else 0
        db = SessionLocal()
        try:
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            name = campaign.name
        finally:
            db.close()
        await query.edit_message_text(
            f"🌐 <b>Choisis le domaine pour ton lien</b>\n\nCampagne : <b>{name}</b>\n\nDomaines configurés :",
            reply_markup=_domain_keyboard(campaign_id),
            parse_mode=ParseMode.HTML,
        )

    elif action == "gen":
        campaign_id = int(parts[2]) if len(parts) > 2 else 0
        domain_val = parts[3] if len(parts) > 3 else ""
        domain: Optional[str] = domain_val if domain_val else None

        db = SessionLocal()
        try:
            campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            link = _create_link_for_campaign(db, campaign, domain)
            full_url = _make_full_url(link.slug, link.domain)
            campaign_name = campaign.name
        finally:
            db.close()

        domain_display = domain or settings.get_public_hostname()
        msg = (
            "✅ <b>Nouveau lien généré !</b>\n\n"
            f"🔗 <code>{full_url}</code>\n\n"
            f"📁 Campagne : {campaign_name}\n"
            f"🆔 Slug : <code>{link.slug}</code>\n"
            f"🌐 Domaine : {domain_display}\n\n"
            "💡 <i>Astuce : appuie sur le lien pour le copier. "
            "Si le lien est cramé, reviens et génère un nouveau slug !</i>"
        )
        await query.edit_message_text(
            msg,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Générer un autre lien", callback_data=f"link:new:{campaign_id}")],
                [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")],
            ]),
            parse_mode=ParseMode.HTML,
        )


# ──────────────────────────────────────────────
# Stats callback
# ──────────────────────────────────────────────

async def callback_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_telegram_admin(query.from_user.id):
        await query.edit_message_text("🚫 Accès refusé.")
        return

    parts = query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    campaign_id = int(parts[2]) if len(parts) > 2 else 0
    logger.info("Callback stats:%s campaign_id=%s from user %s", action, campaign_id, query.from_user.id)

    if action == "show":
        try:
            db = SessionLocal()
            try:
                campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
                if not campaign:
                    await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                    return

                active = [l for l in campaign.links if l.is_active]
                inactive = [l for l in campaign.links if not l.is_active]
                total_clicks = sum(l.clicks for l in campaign.links)
                total_links = len(campaign.links)

                lines = [f"📊 <b>Statistiques — {campaign.name}</b>\n"]
                lines.append(f"🔗 {total_links} lien(s) au total ({len(active)} actif(s), {len(inactive)} désactivé(s))\n")

                if active:
                    lines.append("<b>Liens actifs :</b>")
                    for l in active:
                        domain_label = l.domain or settings.get_public_hostname()
                        lines.append(f"• <code>{l.slug}</code> → {l.clicks} clic(s) ({domain_label})")

                if inactive:
                    lines.append("\n<b>Liens désactivés :</b>")
                    for l in inactive:
                        lines.append(f"• <code>{l.slug}</code> → {l.clicks} clic(s)")

                lines.append(f"\n<b>Total : {total_clicks} clic(s)</b>")

                await query.edit_message_text(
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔗 Nouveau lien", callback_data=f"link:new:{campaign_id}")],
                        [InlineKeyboardButton("◀ Retour", callback_data="menu:stats")],
                    ]),
                    parse_mode=ParseMode.HTML,
                )
            finally:
                db.close()
        except Exception as exc:
            logger.exception("callback_stats error: %s", exc)
            await query.edit_message_text(
                f"⚠️ Erreur lors du chargement des statistiques.\nDétail : {exc}",
                reply_markup=_back_to_menu_keyboard(),
            )


# ──────────────────────────────────────────────
# Document handler (zip upload)
# ──────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        if context.user_data.get(WAITING_FOR_ZIP):
            await update.message.reply_text(
                "⚠️ Merci d'envoyer un fichier <b>.zip</b>.\n\nAnnule avec /cancel",
                parse_mode=ParseMode.HTML,
            )
        return

    if not context.user_data.get(WAITING_FOR_ZIP):
        await update.message.reply_html(
            "ℹ️ Pour uploader une campagne, utilise le menu principal.",
            reply_markup=_main_menu_keyboard(),
        )
        return

    context.user_data[WAITING_FOR_ZIP] = False

    caption = (update.message.caption or "").strip()
    if not caption:
        await update.message.reply_html(
            "⚠️ Merci d'ajouter une <b>légende (caption)</b> au fichier avec le nom de la campagne.\n\n"
            "Exemple : envoie le zip avec la légende <code>promo-noel-2026</code>\n\n"
            "Annule avec /cancel"
        )
        context.user_data[WAITING_FOR_ZIP] = True
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
            await update.message.reply_html(
                f"❌ Une campagne avec le nom <b>« {name} »</b> existe déjà.\n"
                "Choisis un autre nom (modifie la légende du zip).",
            )
            context.user_data[WAITING_FOR_ZIP] = True
            return

        slug_dir = slugify(name)
        try:
            storage_path, entry_file = validate_and_unzip(zip_bytes, slug_dir)
        except StorageError as e:
            await update.message.reply_text(f"❌ Erreur : {e}")
            context.user_data[WAITING_FOR_ZIP] = True
            return

        zip_size_mb = round(len(zip_bytes) / 1024 / 1024, 2)

        campaign = Campaign(
            name=name,
            original_filename=doc.file_name,
            storage_path=storage_path,
            entry_file=entry_file,
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)

        msg = (
            "✅ <b>Campagne créée avec succès !</b>\n\n"
            f"📁 Nom : <b>{campaign.name}</b>\n"
            f"🆔 ID : {campaign.id}\n"
            f"📦 Taille : {zip_size_mb} MB\n"
            f"📄 Fichier d'entrée : <code>{entry_file or '—'}</code>\n\n"
            "Tu peux maintenant générer un lien :"
        )
        await update.message.reply_html(
            msg,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Générer un lien pour cette campagne", callback_data=f"link:new:{campaign.id}")],
                [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")],
            ]),
        )
    finally:
        db.close()


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

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
# Backward-compat legacy commands (redirect to new flows)
# ──────────────────────────────────────────────

async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy /upload — redirect to upload flow."""
    if not await _guard(update, context):
        return
    context.user_data[WAITING_FOR_ZIP] = True
    await update.message.reply_html(
        MESSAGES["upload_start"],
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Annuler", callback_data="menu:main")
        ]]),
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy /list — redirect to campaigns flow."""
    if not await _guard(update, context):
        return
    db = SessionLocal()
    try:
        campaigns = db.query(Campaign).all()
    finally:
        db.close()
    if not campaigns:
        await update.message.reply_html(MESSAGES["no_campaigns"], reply_markup=_main_menu_keyboard())
        return
    await update.message.reply_html(
        f"📋 <b>Tes campagnes ({len(campaigns)})</b>\n\nClique sur une campagne pour voir ses détails :",
        reply_markup=_campaigns_keyboard(campaigns),
    )


async def cmd_newlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy /newlink — redirect to new link flow."""
    if not await _guard(update, context):
        return
    args = context.args or []

    db = SessionLocal()
    try:
        campaigns = db.query(Campaign).all()
        if not campaigns:
            await update.message.reply_html(MESSAGES["no_campaigns"], reply_markup=_main_menu_keyboard())
            return

        # If campaign_id provided, jump straight to domain selection
        if args:
            try:
                campaign_id = int(args[0])
                campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
                if campaign:
                    await update.message.reply_html(
                        f"🌐 <b>Choisis le domaine pour ton lien</b>\n\nCampagne : <b>{campaign.name}</b>",
                        reply_markup=_domain_keyboard(campaign_id),
                    )
                    return
            except ValueError:
                pass

        await update.message.reply_html(
            "🔗 <b>Générer un lien</b>\n\nChoisis la campagne :",
            reply_markup=_campaigns_keyboard(campaigns, callback_prefix="link:new"),
        )
    finally:
        db.close()


async def cmd_setdomain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy /setdomain — kept for backward compat."""
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


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy /delete — kept for backward compat."""
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
        await update.message.reply_html(f"✅ Lien <code>{slug}</code> désactivé.")
    finally:
        db.close()


async def cmd_domains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy /domains — redirect to domains flow."""
    if not await _guard(update, context):
        return
    all_domains = settings.get_all_domains()
    lines = []
    for d in all_domains:
        if d["is_default"]:
            lines.append(f"🌟 <code>{d['domain']}</code>  <i>(domaine public Render)</i>")
        else:
            lines.append(f"✓ <code>{d['domain']}</code>")
    msg = "🌐 <b>Domaines disponibles :</b>\n\n" + "\n".join(lines) if lines else "Aucun domaine configuré."
    await update.message.reply_html(msg, reply_markup=_back_to_menu_keyboard())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy /stats — redirect to stats flow."""
    if not await _guard(update, context):
        return

    args = context.args or []
    if not args:
        # Show campaign list
        db = SessionLocal()
        try:
            campaigns = db.query(Campaign).all()
        finally:
            db.close()
        if not campaigns:
            await update.message.reply_html(MESSAGES["no_campaigns"], reply_markup=_main_menu_keyboard())
            return
        await update.message.reply_html(
            "📊 <b>Statistiques</b>\n\nChoisis une campagne :",
            reply_markup=_campaigns_keyboard(campaigns, callback_prefix="stats:show"),
        )
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

        active = [l for l in campaign.links if l.is_active]
        inactive = [l for l in campaign.links if not l.is_active]
        total_clicks = sum(l.clicks for l in campaign.links)

        lines = [f"📊 <b>Statistiques — {campaign.name}</b>\n"]
        lines.append(f"🔗 {len(campaign.links)} lien(s) ({len(active)} actif(s), {len(inactive)} désactivé(s))\n")
        if active:
            lines.append("<b>Liens actifs :</b>")
            for l in active:
                domain_label = l.domain or settings.get_public_hostname()
                lines.append(f"• <code>{l.slug}</code> → {l.clicks} clic(s) ({domain_label})")
        if inactive:
            lines.append("\n<b>Liens désactivés :</b>")
            for l in inactive:
                lines.append(f"• <code>{l.slug}</code> → {l.clicks} clic(s)")
        lines.append(f"\n<b>Total : {total_clicks} clic(s)</b>")

        await update.message.reply_html(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Nouveau lien", callback_data=f"link:new:{campaign_id}")],
                [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")],
            ]),
        )
    finally:
        db.close()


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the obfuscated admin URL to admins only."""
    if not await _guard(update, context):
        return
    admin_url = _admin_url()
    await update.message.reply_html(
        "🔐 <b>URL d'administration web</b>\n\n"
        f"<code>{admin_url}</code>\n\n"
        "⚠️ Garde cette URL privée. Ne la partage avec personne.",
        reply_markup=_back_to_menu_keyboard(),
    )


# ──────────────────────────────────────────────
# Build application
# ──────────────────────────────────────────────

def build_application() -> Application:
    global _bot_instance, _bot_username
    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .build()
    )
    _bot_instance = app.bot

    # Register menu handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("admin", cmd_admin))

    # Legacy backward-compat command handlers
    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("newlink", cmd_newlink))
    app.add_handler(CommandHandler("setdomain", cmd_setdomain))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("domains", cmd_domains))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # Inline keyboard callback handlers
    app.add_handler(CallbackQueryHandler(callback_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(callback_campaign, pattern=r"^campaign:"))
    app.add_handler(CallbackQueryHandler(callback_link, pattern=r"^link:"))
    app.add_handler(CallbackQueryHandler(callback_stats, pattern=r"^stats:"))

    # Document handler (zip uploads)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    return app


async def setup_bot_ui(bot: Bot) -> None:
    """Configure bot commands list and menu button (called after bot starts)."""
    try:
        await bot.set_my_commands([
            BotCommand("menu", "Ouvrir le menu principal"),
            BotCommand("newlink", "Générer un nouveau lien"),
            BotCommand("campaigns", "Voir mes campagnes"),
            BotCommand("upload", "Uploader une nouvelle campagne"),
            BotCommand("domains", "Voir mes domaines"),
            BotCommand("stats", "Statistiques"),
            BotCommand("admin", "URL du portail admin"),
            BotCommand("help", "Aide"),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Bot commands and menu button configured.")
    except Exception as exc:
        logger.warning("Could not configure bot commands/menu button: %s", exc)


async def set_bot_username(bot: Bot) -> None:
    """Fetch and cache the bot username (called after bot starts)."""
    global _bot_username
    try:
        me = await bot.get_me()
        _bot_username = f"@{me.username}" if me.username else None
    except Exception as exc:
        logger.warning("Could not fetch bot username: %s", exc)


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
