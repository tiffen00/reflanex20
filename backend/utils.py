import random
import re
import string

# Alphabet without confusing chars: i, l, o, 0, 1
_SLUG_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"
SLUG_LENGTH = 8


def generate_slug() -> str:
    """Generate a random 8-char slug from a confusion-safe alphabet."""
    return "".join(random.choices(_SLUG_CHARS, k=SLUG_LENGTH))


def slugify(name: str) -> str:
    """Convert a campaign name to a safe directory name."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s_-]+", "-", name)
    name = re.sub(r"^-+|-+$", "", name)
    return name or "campaign"
