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
import backend.dao as dao
from backend.storage import StorageError
import backend.storage_supabase as storage_sb
from backend.utils import generate_slug, slugify

logger = logging.getLogger(__name__)

_bot_instance: Optional[Bot] = None
_bot_username: Optional[str] = None

# States stored in context.user_data
WAITING_FOR_ZIP = "waiting_for_zip"
PENDING_CAMPAIGN_ID = "pending_campaign_id"
WAITING_FOR_GEO_COUNTRIES = "waiting_for_geo_countries"
WAITING_FOR_ALERT_THRESHOLD = "waiting_for_alert_threshold"


def _admin_url() -> str:
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
        [InlineKeyboardButton("🔗 Générer un lien (campagne existante)", callback_data="menu:newlink")],
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
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")]])


def _campaigns_keyboard(
    campaigns: list[dict],
    links_by_campaign: dict[int, list[dict]],
    callback_prefix: str = "campaign:detail",
) -> InlineKeyboardMarkup:
    buttons = []
    for c in campaigns:
        links = links_by_campaign.get(c["id"], [])
        active_links = sum(1 for l in links if l.get("is_active"))
        label = f"📁 {c['name']} ({active_links} lien{'s' if active_links != 1 else ''})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{c['id']}")])
    buttons.append([InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)


def _campaign_detail_keyboard(campaign_id: int, has_versions: bool = False, is_protected: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🔗 Nouveau lien", callback_data=f"link:new:{campaign_id}")],
        [InlineKeyboardButton("📊 Statistiques", callback_data=f"stats:show:{campaign_id}")],
        [InlineKeyboardButton("📊 Graph 7 jours", callback_data=f"graph:campaign:{campaign_id}")],
    ]
    if has_versions:
        buttons.append([InlineKeyboardButton("🔄 Gérer les versions", callback_data=f"version:list:{campaign_id}")])
    if is_protected:
        buttons.append([InlineKeyboardButton("🔒 Campagne protégée", callback_data="campaign:protected_info")])
    else:
        buttons.append([InlineKeyboardButton("🗑 Supprimer la campagne", callback_data=f"campaign:delete:{campaign_id}")])
    buttons.append([InlineKeyboardButton("◀ Retour", callback_data="menu:campaigns")])
    return InlineKeyboardMarkup(buttons)


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
# /start  /menu  /help  /cancel
# ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    context.user_data.clear()
    await update.message.reply_html(MESSAGES["main_menu"], reply_markup=_main_menu_keyboard())


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    context.user_data.clear()
    await update.message.reply_html(MESSAGES["main_menu"], reply_markup=_main_menu_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    await update.message.reply_html(MESSAGES["help"], reply_markup=_back_to_menu_keyboard())


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
        try:
            campaigns = dao.list_campaigns()
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
            links_by_campaign = {c["id"]: dao.list_links_for_campaign(c["id"]) for c in campaigns}
            await query.edit_message_text(
                f"📋 <b>Tes campagnes ({len(campaigns)})</b>\n\nClique sur une campagne pour voir ses détails et liens :",
                reply_markup=_campaigns_keyboard(campaigns, links_by_campaign),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("campaigns error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())

    elif action == "newlink":
        try:
            campaigns = dao.list_campaigns()
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
            links_by_campaign = {c["id"]: dao.list_links_for_campaign(c["id"]) for c in campaigns}
            await query.edit_message_text(
                "🔗 <b>Générer un lien</b>\n\nChoisis la campagne pour laquelle tu veux générer un lien :",
                reply_markup=_campaigns_keyboard(campaigns, links_by_campaign, callback_prefix="link:new"),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("newlink error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())

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
        await query.edit_message_text(msg, reply_markup=_back_to_menu_keyboard(), parse_mode=ParseMode.HTML)

    elif action == "stats":
        try:
            campaigns = dao.list_campaigns()
            if not campaigns:
                await query.edit_message_text(
                    MESSAGES["no_campaigns"],
                    reply_markup=_back_to_menu_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
                return
            links_by_campaign = {c["id"]: dao.list_links_for_campaign(c["id"]) for c in campaigns}
            await query.edit_message_text(
                "📊 <b>Statistiques</b>\n\nChoisis une campagne :",
                reply_markup=_campaigns_keyboard(campaigns, links_by_campaign, callback_prefix="stats:show"),
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
            MESSAGES["help"], reply_markup=_back_to_menu_keyboard(), parse_mode=ParseMode.HTML
        )

    elif action == "admin":
        admin_url = _admin_url()
        msg = (
            "🔐 <b>URL d'administration web</b>\n\n"
            f"<code>{admin_url}</code>\n\n"
            "⚠️ Garde cette URL privée. Ne la partage avec personne."
        )
        await query.edit_message_text(msg, reply_markup=_back_to_menu_keyboard(), parse_mode=ParseMode.HTML)


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
        try:
            campaign = dao.get_campaign(campaign_id)
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            links = dao.list_links_for_campaign(campaign_id)
            active_links = sum(1 for l in links if l.get("is_active"))
            total_links = len(links)
            versions = dao.list_campaign_versions(campaign["name"])
            version_info = f"v{campaign['version']}" + (" (courante)" if campaign.get("is_current") else " (ancienne)")
            created = campaign["created_at"][:10] if campaign.get("created_at") else "—"
            msg = (
                f"📁 <b>{campaign['name']}</b> ({version_info})\n\n"
                f"🆔 ID : {campaign['id']}\n"
                f"📅 Créée le : {created}\n"
                f"📄 Fichier d'entrée : <code>{campaign.get('entry_file') or '—'}</code>\n"
                f"🔗 Liens actifs : {active_links} / {total_links}\n"
            )
            if len(versions) > 1:
                msg += f"\n📦 Versions disponibles : {len(versions)}\n"
            msg += "\nQue veux-tu faire ?"
            await query.edit_message_text(
                msg,
                reply_markup=_campaign_detail_keyboard(
                    campaign_id,
                    len(versions) > 1,
                    is_protected=bool(campaign.get("is_protected")),
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("campaign detail error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())

    elif action == "protected_info":
        await query.edit_message_text(
            "🔒 <b>Campagne protégée</b>\n\n"
            "Cette campagne est protégée et ne peut pas être supprimée.\n\n"
            "Tu peux toujours :\n"
            "• Générer de nouveaux liens dessus\n"
            "• Voir ses statistiques\n"
            "• La désactiver lien par lien",
            reply_markup=_back_to_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    elif action == "delete":
        campaign_id = int(param)
        try:
            campaign = dao.get_campaign(campaign_id)
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            if campaign.get("is_protected"):
                await query.edit_message_text(
                    "🔒 <b>Cette campagne est protégée et ne peut pas être supprimée.</b>\n\n"
                    "Tu peux toujours :\n"
                    "• Générer de nouveaux liens dessus\n"
                    "• Voir ses statistiques\n"
                    "• La désactiver lien par lien",
                    reply_markup=_campaign_detail_keyboard(
                        campaign_id,
                        is_protected=True,
                    ),
                    parse_mode=ParseMode.HTML,
                )
                return
            links = dao.list_links_for_campaign(campaign_id)
            active_links = sum(1 for l in links if l.get("is_active"))
            total_links = len(links)
            msg = (
                f"⚠️ <b>Confirmer la suppression</b>\n\n"
                f"Tu vas supprimer la campagne <b>{campaign['name']}</b> :\n"
                f"- {total_links} lien(s) seront désactivés\n"
                "- Tous les fichiers seront effacés\n"
                "- Cette action est <b>IRRÉVERSIBLE</b>\n\n"
                "Confirmer ?"
            )
            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Oui, supprimer", callback_data=f"campaign:delete_confirm:{campaign_id}"),
                    InlineKeyboardButton("❌ Annuler", callback_data=f"campaign:detail:{campaign_id}"),
                ]]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("campaign delete error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())

    elif action == "delete_confirm":
        campaign_id = int(param)
        try:
            campaign = dao.get_campaign(campaign_id)
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            if campaign.get("is_protected"):
                await query.edit_message_text(
                    "🔒 <b>Cette campagne est protégée et ne peut pas être supprimée.</b>\n\n"
                    "Tu peux toujours :\n"
                    "• Générer de nouveaux liens dessus\n"
                    "• Voir ses statistiques\n"
                    "• La désactiver lien par lien",
                    reply_markup=_campaign_detail_keyboard(
                        campaign_id,
                        is_protected=True,
                    ),
                    parse_mode=ParseMode.HTML,
                )
                return
            name = campaign["name"]
            storage_path = campaign.get("storage_path", "")
            dao.delete_campaign(campaign_id)
        except Exception as exc:
            logger.exception("campaign delete_confirm error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())
            return

        try:
            storage_sb.delete_campaign_storage(storage_path)
        except Exception as exc:
            logger.warning("Error deleting campaign storage: %s", exc)

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
        try:
            campaign = dao.get_campaign(campaign_id)
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            await query.edit_message_text(
                f"🌐 <b>Choisis le domaine pour ton lien</b>\n\nCampagne : <b>{campaign['name']}</b>\n\nDomaines configurés :",
                reply_markup=_domain_keyboard(campaign_id),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("link new error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())

    elif action == "gen":
        campaign_id = int(parts[2]) if len(parts) > 2 else 0
        domain_val = parts[3] if len(parts) > 3 else ""
        domain: Optional[str] = domain_val if domain_val else None

        try:
            campaign = dao.get_campaign(campaign_id)
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            link = _create_link_in_dao(campaign_id, domain)
            full_url = _make_full_url(link["slug"], link.get("domain"))
            campaign_name = campaign["name"]
        except Exception as exc:
            logger.exception("link gen error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())
            return

        domain_display = domain or settings.get_public_hostname()
        msg = (
            "✅ <b>Nouveau lien généré !</b>\n\n"
            f"🔗 <code>{full_url}</code>\n\n"
            f"📁 Campagne : {campaign_name}\n"
            f"🆔 Slug : <code>{link['slug']}</code>\n"
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

    elif action == "detail":
        link_id = int(parts[2]) if len(parts) > 2 else 0
        try:
            link = dao.get_link_by_id(link_id)
            if not link:
                await query.edit_message_text("❌ Lien introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            campaign = dao.get_campaign(link["campaign_id"])
            stats = dao.get_link_stats(link_id)
            total = stats.get("total_clicks", 0)
            unique = stats.get("unique_visitors", 0)
            status = "✅ Actif" if link.get("is_active") else "❌ Désactivé"
            click_limit = link.get("click_limit") or "illimité"
            expires_at = link.get("expires_at") or "aucune"
            campaign_name = campaign["name"] if campaign else "—"
            msg = (
                f"🔗 <b>Lien {link['slug']}</b>\n\n"
                f"📁 Campagne : {campaign_name}\n"
                f"📊 Clics : {total} ({unique} uniques)\n"
                f"🔘 Statut : {status}\n"
                f"🔢 Limite : {click_limit}\n"
                f"⏰ Expiration : {expires_at}\n"
            )
            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 Graph 7 jours", callback_data=f"graph:link:{link_id}")],
                    [InlineKeyboardButton("🌍 Géo-blocage", callback_data=f"geo:show:{link_id}")],
                    [InlineKeyboardButton("🔔 Configurer alerte", callback_data=f"alert:add:{link_id}")],
                    [InlineKeyboardButton("🗑 Désactiver", callback_data=f"link:deactivate:{link_id}")],
                    [InlineKeyboardButton("◀ Retour", callback_data=f"campaign:detail:{link['campaign_id']}")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("link detail error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())

    elif action == "deactivate":
        link_id = int(parts[2]) if len(parts) > 2 else 0
        try:
            link = dao.get_link_by_id(link_id)
            if not link:
                await query.edit_message_text("❌ Lien introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            dao.deactivate_link(link["slug"])
            await query.edit_message_text(
                f"✅ Lien <code>{link['slug']}</code> désactivé.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Retour campagne", callback_data=f"campaign:detail:{link['campaign_id']}")],
                    [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("link deactivate error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())


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
            campaign = dao.get_campaign(campaign_id)
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return

            links = dao.list_links_for_campaign(campaign_id)
            active = [l for l in links if l.get("is_active")]
            inactive = [l for l in links if not l.get("is_active")]
            total_links = len(links)

            total_clicks = 0
            lines = [f"📊 <b>Statistiques — {campaign['name']}</b>\n"]
            lines.append(f"🔗 {total_links} lien(s) au total ({len(active)} actif(s), {len(inactive)} désactivé(s))\n")

            if active:
                lines.append("<b>Liens actifs :</b>")
                for l in active:
                    stats = dao.get_link_stats(l["id"])
                    clicks = stats.get("total_clicks", 0)
                    total_clicks += clicks
                    domain_label = l.get("domain") or settings.get_public_hostname()
                    lines.append(f"• <code>{l['slug']}</code> → {clicks} clic(s) ({domain_label})")

            if inactive:
                lines.append("\n<b>Liens désactivés :</b>")
                for l in inactive:
                    stats = dao.get_link_stats(l["id"])
                    clicks = stats.get("total_clicks", 0)
                    total_clicks += clicks
                    lines.append(f"• <code>{l['slug']}</code> → {clicks} clic(s)")

            lines.append(f"\n<b>Total : {total_clicks} clic(s)</b>")

            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Nouveau lien", callback_data=f"link:new:{campaign_id}")],
                    [InlineKeyboardButton("📊 Graph 7 jours", callback_data=f"graph:campaign:{campaign_id}")],
                    [InlineKeyboardButton("◀ Retour", callback_data="menu:stats")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("callback_stats error: %s", exc)
            await query.edit_message_text(
                f"⚠️ Erreur lors du chargement des statistiques.\nDétail : {exc}",
                reply_markup=_back_to_menu_keyboard(),
            )


# ──────────────────────────────────────────────
# Graph callback
# ──────────────────────────────────────────────

async def callback_graph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_telegram_admin(query.from_user.id):
        await query.edit_message_text("🚫 Accès refusé.")
        return

    parts = query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    param_id = int(parts[2]) if len(parts) > 2 else 0

    try:
        from backend.charts import render_clicks_chart
    except ImportError:
        await query.edit_message_text(
            "⚠️ matplotlib n'est pas installé.",
            reply_markup=_back_to_menu_keyboard(),
        )
        return

    try:
        if action == "campaign":
            campaign = dao.get_campaign(param_id)
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            links = dao.list_links_for_campaign(param_id)
            # Aggregate clicks per day across all links
            from datetime import timedelta
            days = 7
            combined: dict[str, int] = {}
            for link in links:
                data = dao.get_clicks_per_day(link["id"], days)
                for d in data:
                    combined[d["date"]] = combined.get(d["date"], 0) + d["count"]
            chart_data = [{"date": k, "count": v} for k, v in sorted(combined.items())]
            title = f"Clics — {campaign['name']} (7 derniers jours)"
            back_cb = f"campaign:detail:{param_id}"
        elif action == "link":
            link = dao.get_link_by_id(param_id)
            if not link:
                await query.edit_message_text("❌ Lien introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            chart_data = dao.get_clicks_per_day(param_id, 7)
            title = f"Clics — {link['slug']} (7 derniers jours)"
            back_cb = f"link:detail:{param_id}"
        else:
            await query.edit_message_text("❌ Action inconnue.", reply_markup=_back_to_menu_keyboard())
            return

        png_bytes = render_clicks_chart(chart_data, title)
        await query.message.reply_photo(
            photo=io.BytesIO(png_bytes),
            caption=title,
        )
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Retour", callback_data=back_cb)]])
        )
    except Exception as exc:
        logger.exception("callback_graph error: %s", exc)
        await query.edit_message_text(f"⚠️ Erreur lors de la génération du graphique.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())


# ──────────────────────────────────────────────
# Geo callbacks
# ──────────────────────────────────────────────

async def callback_geo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_telegram_admin(query.from_user.id):
        await query.edit_message_text("🚫 Accès refusé.")
        return

    parts = query.data.split(":", 3)
    action = parts[1] if len(parts) > 1 else ""
    param = parts[2] if len(parts) > 2 else ""
    extra = parts[3] if len(parts) > 3 else ""

    if action == "show":
        link_id = int(param)
        try:
            rule = dao.get_geo_rule(link_id)
            if rule:
                countries_str = ", ".join(rule.get("countries", []))
                msg = (
                    f"🌍 <b>Règle de géo-blocage</b>\n\n"
                    f"Mode : <b>{'Bloquer' if rule['mode'] == 'block' else 'Autoriser seulement'}</b>\n"
                    f"Pays : <code>{countries_str or 'aucun'}</code>\n"
                )
            else:
                msg = "🌍 <b>Géo-blocage</b>\n\nAucune règle configurée."
            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚫 Bloquer des pays", callback_data=f"geo:setmode:{link_id}:block")],
                    [InlineKeyboardButton("✅ Autoriser seulement", callback_data=f"geo:setmode:{link_id}:allow")],
                    [InlineKeyboardButton("🗑 Supprimer la règle", callback_data=f"geo:delete:{link_id}")],
                    [InlineKeyboardButton("◀ Retour", callback_data=f"link:detail:{link_id}")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("geo show error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())

    elif action == "setmode":
        link_id = int(param)
        mode = extra
        context.user_data[WAITING_FOR_GEO_COUNTRIES] = {"link_id": link_id, "mode": mode}
        mode_label = "bloquer" if mode == "block" else "autoriser seulement"
        await query.edit_message_text(
            f"🌍 <b>Géo-blocage — {mode_label}</b>\n\n"
            "Envoie les codes pays séparés par des virgules.\n"
            "Exemple : <code>FR,BE,CH</code>\n\n"
            "Annule avec /cancel",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Annuler", callback_data="menu:main")]]),
            parse_mode=ParseMode.HTML,
        )

    elif action == "delete":
        link_id = int(param)
        try:
            dao.delete_geo_rule(link_id)
            await query.edit_message_text(
                "✅ Règle de géo-blocage supprimée.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Retour", callback_data=f"link:detail:{link_id}")]]),
            )
        except Exception as exc:
            logger.exception("geo delete error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())


# ──────────────────────────────────────────────
# Alert callbacks
# ──────────────────────────────────────────────

async def callback_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_telegram_admin(query.from_user.id):
        await query.edit_message_text("🚫 Accès refusé.")
        return

    parts = query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    param = parts[2] if len(parts) > 2 else ""

    if action == "add":
        link_id = int(param)
        context.user_data[WAITING_FOR_ALERT_THRESHOLD] = {"link_id": link_id}
        await query.edit_message_text(
            "🔔 <b>Configurer une alerte de clics</b>\n\n"
            "Envoie le nombre de clics à partir duquel tu veux être notifié.\n"
            "Exemple : <code>100</code>\n\n"
            "Annule avec /cancel",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Annuler", callback_data="menu:main")]]),
            parse_mode=ParseMode.HTML,
        )


# ──────────────────────────────────────────────
# Version callbacks
# ──────────────────────────────────────────────

async def callback_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_telegram_admin(query.from_user.id):
        await query.edit_message_text("🚫 Accès refusé.")
        return

    parts = query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    param = parts[2] if len(parts) > 2 else ""

    if action == "create_new":
        existing_id = int(param)
        existing = dao.get_campaign(existing_id)
        if not existing:
            await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
            return

        zip_bytes = context.user_data.get("pending_zip")
        if not zip_bytes:
            await query.edit_message_text(
                "❌ Les données du zip ont expiré. Recommence l'upload.",
                reply_markup=_back_to_menu_keyboard(),
            )
            return

        name = context.user_data.get("pending_zip_name", existing["name"])
        filename = context.user_data.get("pending_zip_filename", "upload.zip")
        new_version = existing["version"] + 1

        await query.edit_message_text(f"⏳ Création de la version {new_version} de {name}…")

        try:
            slug_dir = slugify(name)
            storage_path, entry_file = storage_sb.upload_campaign(zip_bytes, slug_dir, new_version)
            new_campaign = dao.create_campaign(name, storage_path, entry_file, filename, version=new_version)

            # Migrate active links to new campaign version
            old_links = dao.list_links_for_campaign(existing_id)
            dao.migrate_links_to_campaign([lnk["id"] for lnk in old_links], new_campaign["id"])

            dao.set_current_version(new_campaign["id"])

            context.user_data.pop("pending_zip", None)
            context.user_data.pop("pending_zip_name", None)
            context.user_data.pop("pending_zip_filename", None)
            context.user_data.pop("pending_existing_id", None)
            context.user_data.pop("pending_existing_version", None)

            await query.edit_message_text(
                f"✅ <b>Version {new_version} créée !</b>\n\n"
                f"📁 {name}\n"
                f"🔗 {len(old_links)} lien(s) migré(s) vers v{new_version}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Voir la campagne", callback_data=f"campaign:detail:{new_campaign['id']}")],
                    [InlineKeyboardButton("◀ Menu", callback_data="menu:main")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("Error creating new version: %s", exc)
            await query.edit_message_text(f"❌ Erreur : {exc}", reply_markup=_back_to_menu_keyboard())

    elif action == "list":
        campaign_id = int(param)
        try:
            campaign = dao.get_campaign(campaign_id)
            if not campaign:
                await query.edit_message_text("❌ Campagne introuvable.", reply_markup=_back_to_menu_keyboard())
                return
            versions = dao.list_campaign_versions(campaign["name"])
            lines = [f"📦 <b>Versions de « {campaign['name']} »</b>\n"]
            for v in versions:
                current_marker = " ✅ (courante)" if v.get("is_current") else ""
                lines.append(f"• v{v['version']}{current_marker} — ID {v['id']}")
            buttons = []
            for v in versions:
                if not v.get("is_current"):
                    buttons.append([InlineKeyboardButton(
                        f"🔄 Activer v{v['version']}",
                        callback_data=f"version:switch:{v['id']}",
                    )])
            buttons.append([InlineKeyboardButton("◀ Retour", callback_data=f"campaign:detail:{campaign_id}")])
            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("version list error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())

    elif action == "switch":
        target_id = int(param)
        try:
            dao.set_current_version(target_id)
            campaign = dao.get_campaign(target_id)
            name = campaign["name"] if campaign else "?"
            await query.edit_message_text(
                f"✅ Version v{campaign['version'] if campaign else '?'} de <b>{name}</b> activée !",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Voir la campagne", callback_data=f"campaign:detail:{target_id}")],
                    [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.exception("version switch error: %s", exc)
            await query.edit_message_text(f"⚠️ Erreur.\nDétail : {exc}", reply_markup=_back_to_menu_keyboard())


# ──────────────────────────────────────────────
# Text message handler (for geo countries / alert threshold input)
# ──────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return

    # Handle geo countries input
    geo_state = context.user_data.get(WAITING_FOR_GEO_COUNTRIES)
    if geo_state:
        text = (update.message.text or "").strip().upper()
        raw_countries = [c.strip() for c in text.split(",") if c.strip()]
        invalid = [c for c in raw_countries if len(c) != 2 or not c.isalpha()]
        if invalid:
            await update.message.reply_html(
                f"⚠️ Codes invalides : {', '.join(invalid)}\n"
                "Utilise des codes ISO 2 lettres (ex: FR, BE, CH).\n\nRéessaie ou /cancel"
            )
            return
        try:
            dao.set_geo_rule(geo_state["link_id"], geo_state["mode"], raw_countries)
            context.user_data.pop(WAITING_FOR_GEO_COUNTRIES, None)
            mode_label = "Blocage" if geo_state["mode"] == "block" else "Autorisation"
            await update.message.reply_html(
                f"✅ {mode_label} configuré pour : <code>{', '.join(raw_countries)}</code>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Retour lien", callback_data=f"link:detail:{geo_state['link_id']}")],
                    [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")],
                ]),
            )
        except Exception as exc:
            logger.exception("set geo rule error: %s", exc)
            await update.message.reply_text(f"❌ Erreur : {exc}")
        return

    # Handle alert threshold input
    alert_state = context.user_data.get(WAITING_FOR_ALERT_THRESHOLD)
    if alert_state:
        text = (update.message.text or "").strip()
        try:
            threshold = int(text)
            if threshold <= 0:
                raise ValueError("threshold must be positive")
        except ValueError:
            await update.message.reply_text(
                "⚠️ Envoie un nombre entier positif.\n\nRéessaie ou /cancel"
            )
            return
        try:
            dao.add_alert(alert_state["link_id"], threshold)
            context.user_data.pop(WAITING_FOR_ALERT_THRESHOLD, None)
            await update.message.reply_html(
                f"✅ Alerte configurée : tu seras notifié à <b>{threshold}</b> clics.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Retour lien", callback_data=f"link:detail:{alert_state['link_id']}")],
                    [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")],
                ]),
            )
        except Exception as exc:
            logger.exception("add alert error: %s", exc)
            await update.message.reply_text(f"❌ Erreur : {exc}")
        return


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

    name = caption
    existing = dao.get_campaign_by_name(name)
    if existing:
        context.user_data["pending_zip"] = zip_bytes
        context.user_data["pending_zip_name"] = name
        context.user_data["pending_zip_filename"] = doc.file_name
        context.user_data["pending_existing_id"] = existing["id"]
        context.user_data["pending_existing_version"] = existing["version"]
        await update.message.reply_html(
            f"⚠️ Une campagne <b>« {name} »</b> existe déjà (version {existing['version']}).\n\n"
            "Que veux-tu faire ?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"📦 Créer la version {existing['version'] + 1} (liens → v{existing['version'] + 1})",
                    callback_data=f"version:create_new:{existing['id']}",
                )],
                [InlineKeyboardButton("❌ Annuler", callback_data="menu:main")],
            ]),
        )
        return

    slug_dir = slugify(name)
    try:
        storage_path, entry_file = storage_sb.upload_campaign(zip_bytes, slug_dir, 1)
    except StorageError as e:
        await update.message.reply_text(f"❌ Erreur : {e}")
        context.user_data[WAITING_FOR_ZIP] = True
        return

    try:
        campaign = dao.create_campaign(
            name=name,
            storage_path=storage_path,
            entry_file=entry_file,
            original_filename=doc.file_name,
            version=1,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur lors de la création : {e}")
        return

    zip_size_mb = round(len(zip_bytes) / 1024 / 1024, 2)
    msg = (
        "✅ <b>Campagne créée avec succès !</b>\n\n"
        f"📁 Nom : <b>{campaign['name']}</b>\n"
        f"🆔 ID : {campaign['id']}\n"
        f"📦 Taille : {zip_size_mb} MB\n"
        f"📄 Fichier d'entrée : <code>{entry_file or '—'}</code>\n\n"
        "Tu peux maintenant générer un lien :"
    )
    await update.message.reply_html(
        msg,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Générer un lien pour cette campagne", callback_data=f"link:new:{campaign['id']}")],
            [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")],
        ]),
    )


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _create_link_in_dao(campaign_id: int, domain: Optional[str]) -> dict:
    for _ in range(20):
        slug = generate_slug()
        if not dao.get_link_by_slug(slug):
            return dao.create_link(slug, campaign_id, domain)
    raise RuntimeError("Could not generate unique slug")


def _make_full_url(slug: str, domain: Optional[str]) -> str:
    if domain:
        return f"https://{domain}/c/{slug}/"
    return f"{settings.PUBLIC_BASE_URL}/c/{slug}/"


# ──────────────────────────────────────────────
# Backward-compat legacy commands
# ──────────────────────────────────────────────

async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    if not await _guard(update, context):
        return
    campaigns = dao.list_campaigns()
    if not campaigns:
        await update.message.reply_html(MESSAGES["no_campaigns"], reply_markup=_main_menu_keyboard())
        return
    links_by_campaign = {c["id"]: dao.list_links_for_campaign(c["id"]) for c in campaigns}
    await update.message.reply_html(
        f"📋 <b>Tes campagnes ({len(campaigns)})</b>\n\nClique sur une campagne pour voir ses détails :",
        reply_markup=_campaigns_keyboard(campaigns, links_by_campaign),
    )


async def cmd_newlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    args = context.args or []
    campaigns = dao.list_campaigns()
    if not campaigns:
        await update.message.reply_html(MESSAGES["no_campaigns"], reply_markup=_main_menu_keyboard())
        return

    if args:
        try:
            campaign_id = int(args[0])
            campaign = dao.get_campaign(campaign_id)
            if campaign:
                await update.message.reply_html(
                    f"🌐 <b>Choisis le domaine pour ton lien</b>\n\nCampagne : <b>{campaign['name']}</b>",
                    reply_markup=_domain_keyboard(campaign_id),
                )
                return
        except ValueError:
            pass

    links_by_campaign = {c["id"]: dao.list_links_for_campaign(c["id"]) for c in campaigns}
    await update.message.reply_html(
        "🔗 <b>Générer un lien</b>\n\nChoisis la campagne :",
        reply_markup=_campaigns_keyboard(campaigns, links_by_campaign, callback_prefix="link:new"),
    )


async def cmd_setdomain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage : /setdomain <slug> <domain>")
        return
    slug, domain = args[0], args[1]
    link = dao.get_link_by_slug(slug)
    if not link:
        await update.message.reply_text(f"❌ Lien {slug} introuvable.")
        return
    dao.update_link_domain(slug, domain)
    full_url = _make_full_url(slug, domain)
    await update.message.reply_html(f"✅ Domaine mis à jour.\n🔗 <code>{full_url}</code>")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update, context):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage : /delete <slug>")
        return
    slug = args[0]
    link = dao.get_link_by_slug(slug)
    if not link:
        await update.message.reply_text(f"❌ Lien {slug} introuvable.")
        return
    dao.deactivate_link(slug)
    await update.message.reply_html(f"✅ Lien <code>{slug}</code> désactivé.")


async def cmd_domains(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    if not await _guard(update, context):
        return
    args = context.args or []
    if not args:
        campaigns = dao.list_campaigns()
        if not campaigns:
            await update.message.reply_html(MESSAGES["no_campaigns"], reply_markup=_main_menu_keyboard())
            return
        links_by_campaign = {c["id"]: dao.list_links_for_campaign(c["id"]) for c in campaigns}
        await update.message.reply_html(
            "📊 <b>Statistiques</b>\n\nChoisis une campagne :",
            reply_markup=_campaigns_keyboard(campaigns, links_by_campaign, callback_prefix="stats:show"),
        )
        return

    try:
        campaign_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ campaign_id doit être un entier.")
        return

    campaign = dao.get_campaign(campaign_id)
    if not campaign:
        await update.message.reply_text(f"❌ Campagne {campaign_id} introuvable.")
        return

    links = dao.list_links_for_campaign(campaign_id)
    active = [l for l in links if l.get("is_active")]
    inactive = [l for l in links if not l.get("is_active")]
    total_clicks = 0

    lines = [f"📊 <b>Statistiques — {campaign['name']}</b>\n"]
    lines.append(f"🔗 {len(links)} lien(s) ({len(active)} actif(s), {len(inactive)} désactivé(s))\n")
    if active:
        lines.append("<b>Liens actifs :</b>")
        for l in active:
            stats = dao.get_link_stats(l["id"])
            clicks = stats.get("total_clicks", 0)
            total_clicks += clicks
            domain_label = l.get("domain") or settings.get_public_hostname()
            lines.append(f"• <code>{l['slug']}</code> → {clicks} clic(s) ({domain_label})")
    if inactive:
        lines.append("\n<b>Liens désactivés :</b>")
        for l in inactive:
            stats = dao.get_link_stats(l["id"])
            clicks = stats.get("total_clicks", 0)
            total_clicks += clicks
            lines.append(f"• <code>{l['slug']}</code> → {clicks} clic(s)")
    lines.append(f"\n<b>Total : {total_clicks} clic(s)</b>")

    await update.message.reply_html(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Nouveau lien", callback_data=f"link:new:{campaign_id}")],
            [InlineKeyboardButton("◀ Menu principal", callback_data="menu:main")],
        ]),
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    _bot_instance = app.bot

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("admin", cmd_admin))

    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("newlink", cmd_newlink))
    app.add_handler(CommandHandler("setdomain", cmd_setdomain))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("domains", cmd_domains))
    app.add_handler(CommandHandler("stats", cmd_stats))

    app.add_handler(CallbackQueryHandler(callback_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(callback_campaign, pattern=r"^campaign:"))
    app.add_handler(CallbackQueryHandler(callback_link, pattern=r"^link:"))
    app.add_handler(CallbackQueryHandler(callback_stats, pattern=r"^stats:"))
    app.add_handler(CallbackQueryHandler(callback_graph, pattern=r"^graph:"))
    app.add_handler(CallbackQueryHandler(callback_geo, pattern=r"^geo:"))
    app.add_handler(CallbackQueryHandler(callback_alert, pattern=r"^alert:"))
    app.add_handler(CallbackQueryHandler(callback_version, pattern=r"^version:"))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


async def setup_bot_ui(bot: Bot) -> None:
    try:
        await bot.set_my_commands([
            BotCommand("menu", "Ouvrir le menu principal"),
            BotCommand("newlink", "Générer un nouveau lien"),
            BotCommand("list", "Voir mes campagnes"),
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
            await _bot_instance.send_message(chat_id=admin_id, text=message, parse_mode="HTML")
        except Exception as exc:
            logger.warning("Could not send login-success notification to %s: %s", admin_id, exc)
