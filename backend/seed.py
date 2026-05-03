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


def _is_schema_error(exc: Exception) -> bool:
    """Return True if the exception indicates a missing DB table (PostgreSQL 42P01).

    The Supabase PostgREST client serialises the SQLSTATE into the exception
    message, so string-matching on ``"42P01"`` is the correct approach here.
    We also check a ``code`` attribute for forward-compatibility.
    """
    # Structured code attribute (some supabase-py versions expose this)
    if str(getattr(exc, "code", "")).upper() == "42P01":
        return True
    msg = str(exc).lower()
    return "42p01" in msg or ("relation" in msg and "does not exist" in msg)


def _ensure_bucket_exists() -> None:
    """
    Verify that the ``campaigns`` storage bucket exists.
    Auto-creates it (private) if it is missing.
    Raises on unrecoverable errors so the caller can abort early.
    """
    sb = get_supabase()
    try:
        buckets = sb.storage.list_buckets()
        bucket_ids = [b.id for b in buckets]
    except Exception as exc:
        logger.warning("Could not list storage buckets (skipping bucket check): %s", exc)
        return

    if BUCKET not in bucket_ids:
        logger.info("Bucket '%s' not found — attempting auto-creation …", BUCKET)
        try:
            sb.storage.create_bucket(BUCKET, {"public": False})
            logger.info("✅ Auto-created bucket '%s'", BUCKET)
        except Exception as exc:
            logger.error("❌ Could not create bucket '%s': %s", BUCKET, exc)
            raise


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
                logger.error("❌ Upload failed: %s reason=%s", upload_path, exc2)

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

    logger.info("🌱 [SEED] Starting AR24 seed check…")
    logger.info("🌱 [SEED] Source dir: %s (exists=%s)", src_dir, src_dir.exists())

    if not src_dir.exists():
        logger.error(
            "🌱 [SEED] ❌ ABORT: AR24 template source missing at %s — cannot seed protected campaign",
            src_dir,
        )
        return

    src_files = [p for p in src_dir.rglob("*") if p.is_file()]
    logger.info(
        "🌱 [SEED] Source contains %d file(s): %s",
        len(src_files),
        [f.name for f in src_files],
    )

    # Ensure the storage bucket is present before any DB/storage operations
    try:
        logger.info("🌱 [SEED] Checking Supabase bucket '%s'…", BUCKET)
        _ensure_bucket_exists()
        logger.info("🌱 [SEED] Bucket '%s': ok", BUCKET)
    except Exception as exc:
        logger.error("🌱 [SEED] ❌ ABORT: bucket check failed: %s", exc)
        return

    # Schema sanity-check: confirm the campaigns table has the is_protected column
    try:
        sb = get_supabase()
        sb.table("campaigns").select("id,is_protected").limit(1).execute()
        logger.info("🌱 [SEED] Schema check: ok (campaigns.is_protected present)")
    except Exception as exc:
        if _is_schema_error(exc):
            logger.error(
                "🌱 [SEED] ❌ ABORT: schema not migrated — run supabase/schema.sql "
                "+ 003_protected_campaigns.sql. Detail: %s",
                exc,
            )
        else:
            logger.error("🌱 [SEED] ❌ ABORT: schema check failed: %s", exc)
        return

    try:
        campaign = dao.get_campaign_by_name(name)
        logger.info(
            "🌱 [SEED] DB lookup '%s': %s",
            name,
            ("FOUND id=" + str(campaign["id"])) if campaign else "not found",
        )
    except Exception as exc:
        if _is_schema_error(exc):
            logger.error(
                "🌱 [SEED] ❌ ABORT: schema not migrated. Run supabase/schema.sql first. Detail: %s",
                exc,
            )
        else:
            logger.error("🌱 [SEED] ❌ ABORT: could not query campaigns table: %s", exc)
        return

    if campaign is None:
        logger.info("🌱 [SEED] Creating AR24 from scratch…")
        try:
            storage_path, entry_file = _upload_directory_to_storage(src_dir, name, version=1)
            logger.info("🌱 [SEED] Files uploaded to: %s, entry=%s", storage_path, entry_file)
            created = dao.create_campaign(
                name=name,
                storage_path=storage_path,
                entry_file=entry_file,
                original_filename="ar24-template",
                is_protected=True,
            )
            logger.info(
                "🌱 [SEED] ✅ AR24 CREATED: id=%s name=%s storage_path=%s",
                created.get("id"),
                created.get("name"),
                created.get("storage_path"),
            )
        except Exception as exc:
            if _is_schema_error(exc):
                logger.error(
                    "🌱 [SEED] ❌ Schema not migrated. Run supabase/schema.sql first. Detail: %s",
                    exc,
                )
            else:
                logger.exception("🌱 [SEED] ❌ Creation failed: %s", exc)
        return

    # Campaign already exists — ensure protection flag is set
    logger.info(
        "🌱 [SEED] AR24 already exists (id=%s, is_protected=%s)",
        campaign["id"],
        campaign.get("is_protected"),
    )
    if not campaign.get("is_protected"):
        try:
            dao.set_campaign_protected(campaign["id"], True)
            logger.info(
                "🌱 [SEED] Set is_protected=true on existing '%s' campaign (id=%s)",
                name,
                campaign["id"],
            )
        except Exception as exc:
            logger.warning("🌱 [SEED] Could not set is_protected on campaign '%s': %s", name, exc)

    # Verify files exist; re-upload if missing
    storage_path = campaign.get("storage_path", "")
    logger.info("🌱 [SEED] Checking storage path: '%s'…", storage_path)
    if storage_path and not _storage_exists(storage_path):
        logger.warning(
            "🌱 [SEED] AR24 storage missing at '%s', re-uploading from template…",
            storage_path,
        )
        try:
            new_storage_path, entry_file = _upload_directory_to_storage(
                src_dir, name, version=campaign.get("version", 1)
            )
            dao.update_campaign_storage(campaign["id"], new_storage_path, entry_file)
            logger.info("🌱 [SEED] ✅ Re-uploaded AR24 storage to '%s'", new_storage_path)
        except Exception as exc:
            logger.error("🌱 [SEED] ❌ Failed to re-upload AR24 storage: %s", exc)
    else:
        logger.info("🌱 [SEED] ✅ Protected campaign '%s' is present and complete.", name)
