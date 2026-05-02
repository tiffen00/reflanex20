"""Compile User-Agent blocklist and whitelist from config files."""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
_BLOCKLIST_FILE = _CONFIG_DIR / "bot_user_agents.txt"
_WHITELIST_FILE = _CONFIG_DIR / "trusted_user_agents.txt"

_blocklist_regex: re.Pattern | None = None
_whitelist_regex: re.Pattern | None = None


def _load_patterns(filepath: Path) -> list[str]:
    """Load non-empty, non-comment lines from a file."""
    if not filepath.exists():
        return []
    patterns = []
    for line in filepath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _get_blocklist_regex() -> re.Pattern | None:
    global _blocklist_regex
    if _blocklist_regex is None:
        patterns = _load_patterns(_BLOCKLIST_FILE)
        if patterns:
            combined = "|".join(f"(?:{p})" for p in patterns)
            try:
                _blocklist_regex = re.compile(combined, re.IGNORECASE)
            except re.error as exc:
                logger.error("Failed to compile UA blocklist regex: %s", exc)
    return _blocklist_regex


def _get_whitelist_regex() -> re.Pattern | None:
    global _whitelist_regex
    if _whitelist_regex is None:
        patterns = _load_patterns(_WHITELIST_FILE)
        if patterns:
            combined = "|".join(f"(?:{p})" for p in patterns)
            try:
                _whitelist_regex = re.compile(combined, re.IGNORECASE)
            except re.error as exc:
                logger.error("Failed to compile UA whitelist regex: %s", exc)
    return _whitelist_regex


def is_blocked_ua(user_agent: str) -> bool:
    """Return True if the User-Agent matches the blocklist (and not the whitelist)."""
    if not user_agent:
        return True

    wl = _get_whitelist_regex()
    if wl and wl.search(user_agent):
        return False

    bl = _get_blocklist_regex()
    if bl and bl.search(user_agent):
        return True

    return False
