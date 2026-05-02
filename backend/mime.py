"""
Inline MIME-type detection for campaign file serving.

Never returns a Content-Disposition: attachment type – every mapping is
intentionally chosen so the browser displays the file inline.
"""

from pathlib import Path

_MIME_MAP: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".eot": "application/vnd.ms-fontobject",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    # PHP files are served as HTML (no PHP execution – just static content)
    ".php": "text/html; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
}


def guess_inline_content_type(path: Path) -> str:
    """Return the MIME type for *path*, defaulting to application/octet-stream."""
    return _MIME_MAP.get(path.suffix.lower(), "application/octet-stream")
