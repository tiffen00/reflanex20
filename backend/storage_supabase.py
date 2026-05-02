import io
import logging
import mimetypes
import zipfile
from typing import Optional

from backend.db import get_supabase
from backend.config import settings
from backend.storage import StorageError

logger = logging.getLogger(__name__)
BUCKET = "campaigns"


def _effective_name(raw: str, strip_prefix: Optional[str]) -> str:
    if strip_prefix and raw.startswith(strip_prefix):
        return raw[len(strip_prefix):]
    return raw


def upload_campaign(zip_bytes: bytes, campaign_name: str, version: int) -> tuple[str, str]:
    """
    Validate zip and upload all files to Supabase Storage.
    Returns (storage_path, entry_file).
    """
    max_zip_bytes = settings.MAX_ZIP_SIZE_MB * 1024 * 1024
    if len(zip_bytes) > max_zip_bytes:
        raise StorageError(f"Zip exceeds maximum size of {settings.MAX_ZIP_SIZE_MB} MB")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise StorageError("Fichier invalide : ce n'est pas un zip valide")

    names = zf.namelist()

    for name in names:
        if ".." in name or name.startswith("/"):
            raise StorageError(f"Chemin dangereux détecté dans le zip : {name}")

    regular_files = [n for n in names if not n.endswith("/")]
    if not regular_files:
        raise StorageError("Le fichier zip est vide ou ne contient aucun fichier valide")

    # Determine single top-level directory to strip
    top_dirs: set[str] = set()
    for n in names:
        parts = n.split("/")
        if len(parts) >= 2 and parts[0]:
            top_dirs.add(parts[0])

    strip_prefix: Optional[str] = None
    if len(top_dirs) == 1 and all(n.startswith(list(top_dirs)[0] + "/") for n in names if n):
        strip_prefix = list(top_dirs)[0] + "/"

    root_files = [
        _effective_name(n, strip_prefix) for n in regular_files
        if "/" not in _effective_name(n, strip_prefix)
    ]

    # Auto-detect entry_file
    entry_file = ""
    for candidate in ("index.html", "index.php", "index.htm"):
        if candidate in [f.lower() for f in root_files]:
            entry_file = next(f for f in root_files if f.lower() == candidate)
            break
    if not entry_file:
        html_at_root = sorted(f for f in root_files if f.lower().endswith(".html"))
        if html_at_root:
            entry_file = html_at_root[0]
    if not entry_file:
        php_at_root = sorted(f for f in root_files if f.lower().endswith(".php"))
        if php_at_root:
            entry_file = php_at_root[0]

    storage_path = f"{campaign_name}/v{version}"
    sb = get_supabase()

    for member in zf.infolist():
        if member.filename.endswith("/"):
            continue
        rel = _effective_name(member.filename, strip_prefix)
        if not rel:
            continue
        # Security: block path traversal
        if ".." in rel or rel.startswith("/"):
            logger.warning("Skipping unsafe path: %s", rel)
            continue
        data = zf.read(member.filename)
        upload_path = f"{storage_path}/{rel}"
        mime_type, _ = mimetypes.guess_type(rel)
        content_type = mime_type or "application/octet-stream"
        try:
            sb.storage.from_(BUCKET).upload(
                upload_path,
                data,
                {"content-type": content_type, "upsert": "true"},
            )
        except Exception as e:
            logger.warning("Upload failed for %s: %s — trying update", upload_path, e)
            try:
                sb.storage.from_(BUCKET).update(upload_path, data)
            except Exception as e2:
                logger.error("Could not upload %s: %s", upload_path, e2)

    return storage_path, entry_file


def delete_campaign_storage(storage_path: str) -> None:
    """Delete all files under storage_path in the bucket."""
    sb = get_supabase()
    try:
        files = sb.storage.from_(BUCKET).list(storage_path, {"limit": 1000})
        if files:
            paths = [f"{storage_path}/{f['name']}" for f in files]
            sb.storage.from_(BUCKET).remove(paths)
    except Exception as e:
        logger.error("Error deleting storage path %s: %s", storage_path, e)


def download_file(storage_path: str, relative_path: str) -> Optional[bytes]:
    """Download a single file from Supabase Storage. Returns bytes or None."""
    sb = get_supabase()
    try:
        return sb.storage.from_(BUCKET).download(f"{storage_path}/{relative_path}")
    except Exception as e:
        logger.error("download_file error for %s/%s: %s", storage_path, relative_path, e)
        return None


def list_files(storage_path: str) -> list[str]:
    """List all files under storage_path."""
    sb = get_supabase()
    try:
        files = sb.storage.from_(BUCKET).list(storage_path, {"limit": 1000})
        return [f["name"] for f in (files or [])]
    except Exception as e:
        logger.error("list_files error for %s: %s", storage_path, e)
        return []
