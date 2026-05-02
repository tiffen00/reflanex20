"""
backend/seed.py — Boot-time seeding of protected campaigns.
"""
import logging
import mimetypes
from pathlib import Path

import backend.dao as dao
import backend.storage_supabase as storage_sb
from backend.db import get_supabase

logger = logging.getLogger(__name__)

BUCKET = "campaigns"
AR24_TEMPLATE_DIR = Path(__file__).parent.parent / "examples" / "ar24-template"


def _upload_directory_to_storage(src_dir: Path, campaign_name: str, version: int) -> tuple[str, str]:
    """
    Upload all files from src_dir into Supabase Storage under
    ``{campaign_name}/v{version}/``.

    Returns (storage_path, entry_file).
    """
    storage_path = f"{campaign_name}/v{version}"
    sb = get_supabase()
    entry_file = ""

    files = [p for p in src_dir.rglob("*") if p.is_file()]
    for filepath in files:
        rel = filepath.relative_to(src_dir).as_posix()
        # Skip hidden files and non-web assets
        if any(part.startswith(".") for part in filepath.parts):
            continue
        data = filepath.read_bytes()
        upload_path = f"{storage_path}/{rel}"
        mime_type, _ = mimetypes.guess_type(str(filepath))
        content_type = mime_type or "application/octet-stream"
        try:
            sb.storage.from_(BUCKET).upload(
                upload_path,
                data,
                {"content-type": content_type, "upsert": "true"},
            )
            logger.debug("Seeded file: %s", upload_path)
        except Exception as exc:
            logger.warning("Upload failed for %s: %s — retrying via update", upload_path, exc)
            try:
                sb.storage.from_(BUCKET).update(upload_path, data)
            except Exception as exc2:
                logger.error("Could not seed file %s: %s", upload_path, exc2)

        if not entry_file and filepath.name.lower() in ("index.html", "index.php", "index.htm"):
            entry_file = rel

    if not entry_file:
        # Fallback: first .html at root
        for filepath in sorted(src_dir.iterdir()):
            if filepath.is_file() and filepath.suffix.lower() == ".html":
                entry_file = filepath.name
                break

    return storage_path, entry_file


def _storage_exists(storage_path: str) -> bool:
    """Return True if there is at least one file under storage_path in Supabase Storage."""
    try:
        files = storage_sb.list_files(storage_path)
        return len(files) > 0
    except Exception:
        return False


async def ensure_protected_campaign() -> None:
    """
    Ensure the 'ar24' protected campaign always exists.
    Idempotent: creates if missing, restores files if missing.
    """
    name = "ar24"
    src_dir = AR24_TEMPLATE_DIR
    if not src_dir.exists():
        logger.error(
            "AR24 template source missing at %s - cannot seed protected campaign",
            src_dir,
        )
        return

    campaign = dao.get_campaign_by_name(name)

    if campaign is None:
        logger.info("Seeding protected campaign '%s' from %s …", name, src_dir)
        try:
            storage_path, entry_file = _upload_directory_to_storage(src_dir, name, version=1)
            dao.create_campaign(
                name=name,
                storage_path=storage_path,
                entry_file=entry_file,
                original_filename="ar24-template",
                is_protected=True,
            )
            logger.info("✅ Seeded protected campaign '%s' (storage_path=%s)", name, storage_path)
        except Exception as exc:
            logger.error("Failed to seed protected campaign '%s': %s", name, exc)
        return

    # Campaign already exists — ensure protection flag is set
    if not campaign.get("is_protected"):
        try:
            dao.set_campaign_protected(campaign["id"], True)
            logger.info("Set is_protected=true on existing '%s' campaign (id=%s)", name, campaign["id"])
        except Exception as exc:
            logger.warning("Could not set is_protected on campaign '%s': %s", name, exc)

    # Verify files exist; re-upload if missing
    storage_path = campaign.get("storage_path", "")
    if storage_path and not _storage_exists(storage_path):
        logger.warning(
            "AR24 storage missing at '%s', re-uploading from template …", storage_path
        )
        try:
            new_storage_path, entry_file = _upload_directory_to_storage(
                src_dir, name, version=campaign.get("version", 1)
            )
            dao.update_campaign_storage(campaign["id"], new_storage_path, entry_file)
            logger.info("✅ Re-uploaded AR24 storage to '%s'", new_storage_path)
        except Exception as exc:
            logger.error("Failed to re-upload AR24 storage: %s", exc)
    else:
        logger.info("✅ Protected campaign '%s' is present and complete.", name)
