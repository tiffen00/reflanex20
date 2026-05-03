import re
import secrets
import string
from typing import Optional

# Full alphabet (uppercase + lowercase + digits) for maximum entropy — 62 chars
_SLUG_CHARS_LONG = string.ascii_letters + string.digits

# Default slug length: 32 chars from 62-char alphabet → entropy ≈ 190 bits
DEFAULT_SLUG_LENGTH = 32

# URL path prefix pool — realistic-looking multi-segment prefixes
_URL_PREFIXES = [
    "secure/account/verify",
    "secure/session/auth",
    "portal/document/track",
    "portal/access/confirm",
    "app/v2/sessions",
    "app/auth/verify",
    "api/v2/notifications",
    "api/secure/access",
    "auth/session/verify",
    "auth/confirm/account",
    "track/delivery/notice",
    "track/document/secure",
    "view/secure/document",
    "download/secure/file",
    "document/recommande/track",
    "document/secure/view",
    "mail/notification/view",
    "mail/secure/access",
    "service/client/portal",
    "service/account/verify",
    "client/space/secure",
    "client/portal/access",
    "member/account/verify",
    "delivery/track/notice",
    "delivery/secure/view",
]

# URL path suffix pool — action-like endings
_URL_SUFFIXES = [
    "auth",
    "confirm",
    "verify",
    "view",
    "open",
    "access",
    "download",
    "secure",
    "session",
    "redirect",
    "continue",
    "process",
]


def generate_slug(length: int = DEFAULT_SLUG_LENGTH) -> str:
    """Generate a random slug (default 32 chars, mixed-case + digits, ~190-bit entropy)."""
    return "".join(secrets.choice(_SLUG_CHARS_LONG) for _ in range(length))


def build_url_template(slug: str, style: Optional[str] = None) -> Optional[str]:
    """Build the URL path template for a given slug.

    Returns a path string like ``secure/account/verify/<slug>/auth`` for
    non-short styles, or ``None`` for the legacy ``/c/<slug>/`` style.

    Import of settings is deferred to avoid circular imports at module load.
    """
    if style is None:
        from backend.config import settings
        style = settings.LINK_URL_STYLE
    if style == "short":
        return None
    prefix = secrets.choice(_URL_PREFIXES)
    suffix = secrets.choice(_URL_SUFFIXES)
    return f"{prefix}/{slug}/{suffix}"


def make_public_url_for_slug(
    slug: str,
    domain: Optional[str],
    url_template: Optional[str] = None,
) -> str:
    """Build the public URL for a given slug.

    If *url_template* is provided (stored on the link row) it is used directly.
    Falls back to the legacy ``/c/<slug>/`` format when template is absent.
    """
    from backend.config import settings
    base = f"https://{domain}" if domain else settings.PUBLIC_BASE_URL.rstrip("/")
    if url_template:
        return f"{base}/{url_template}"
    return f"{base}/c/{slug}/"


def slugify(name: str) -> str:
    """Convert a campaign name to a safe directory name."""
    # Limit length before regex to avoid potential DoS on crafted inputs
    name = name[:200].lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)
    # Replace runs of whitespace / underscores / hyphens with a single hyphen
    parts = name.split()
    name = "-".join(p.strip("_-") for p in parts if p.strip("_-"))
    return name or "campaign"
