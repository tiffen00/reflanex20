import io
import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from backend.config import settings


MAX_ZIP_BYTES = settings.MAX_ZIP_SIZE_MB * 1024 * 1024


class StorageError(Exception):
    pass


def _storage_root() -> Path:
    p = Path(settings.STORAGE_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _is_safe_path(base: Path, target: Path) -> bool:
    """Return True if target is safely inside base (no path traversal).

    Uses os.path.realpath for symlink-aware resolution and a strict
    string-prefix check to prevent zip-slip / directory traversal.
    """
    real_base = os.path.realpath(base) + os.sep
    real_target = os.path.realpath(target)
    return real_target.startswith(real_base)


def validate_and_unzip(
    zip_bytes: bytes,
    campaign_slug: str,
) -> tuple[str, str]:
    """
    Validate zip, extract to storage dir, return (storage_path, entry_file).
    Raises StorageError on any validation failure.
    """
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise StorageError(
            f"Zip exceeds maximum size of {settings.MAX_ZIP_SIZE_MB} MB"
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise StorageError("Fichier invalide : ce n'est pas un zip valide")

    names = zf.namelist()

    # Security: block path traversal
    for name in names:
        if ".." in name or name.startswith("/"):
            raise StorageError(f"Chemin dangereux détecté dans le zip : {name}")

    # Find HTML entry file at root level
    root_html_files = [
        n for n in names
        if "/" not in n.strip("/") and n.lower().endswith(".html")
    ]
    # Also accept files in a single top-level directory
    if not root_html_files:
        # Try single-directory zip (e.g., campaign/index.html)
        top_dirs = set()
        for n in names:
            parts = n.split("/")
            if len(parts) >= 2 and parts[0]:
                top_dirs.add(parts[0])
        if len(top_dirs) == 1:
            top_dir = list(top_dirs)[0]
            root_html_files = [
                n for n in names
                if n.startswith(top_dir + "/")
                and n.lower().endswith(".html")
                and n.count("/") == 1
            ]

    if not root_html_files:
        raise StorageError(
            "Le zip ne contient aucun fichier .html à la racine ou dans un dossier racine unique"
        )

    # Prefer index.html, fallback to first .html
    entry_candidates = [f for f in root_html_files if Path(f).name.lower() == "index.html"]
    entry_name = (entry_candidates or root_html_files)[0]
    # entry_file is the bare filename only; the strip_prefix logic below ensures
    # the file lands at dest/<entry_file> after extraction (top-level dir is stripped).
    entry_file = Path(entry_name).name

    dest = _storage_root() / campaign_slug
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    # Extract, stripping top-level dir if needed
    top_dirs = set()
    for n in names:
        parts = n.split("/")
        if len(parts) >= 2 and parts[0]:
            top_dirs.add(parts[0])

    strip_prefix: Optional[str] = None
    if (
        len(top_dirs) == 1
        and all(n.startswith(list(top_dirs)[0] + "/") for n in names if n)
    ):
        strip_prefix = list(top_dirs)[0] + "/"

    # Pre-compute the resolved destination so path checks are fast and consistent
    resolved_dest = os.path.realpath(dest) + os.sep

    for member in zf.infolist():
        name = member.filename
        if name.endswith("/"):
            continue  # skip directories

        if strip_prefix and name.startswith(strip_prefix):
            rel = name[len(strip_prefix):]
        else:
            rel = name

        if not rel:
            continue

        # Resolve the target path relative to dest and verify it stays inside
        target = (dest / rel).resolve()
        if not os.path.realpath(target).startswith(resolved_dest):
            continue  # skip unsafe paths (zip-slip protection)

        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(target, "wb") as dst:
            dst.write(src.read())

    return str(dest), entry_file


def delete_campaign_files(storage_path: str) -> None:
    p = Path(storage_path)
    if p.exists():
        shutil.rmtree(p)


def get_file_path(storage_path: str, relative_path: str) -> Optional[Path]:
    """Return resolved file path if safe, else None."""
    base = Path(os.path.realpath(storage_path))
    # Resolve the joined path to catch any traversal attempts
    target = Path(os.path.realpath(base / relative_path))
    # Verify the resolved target is strictly inside the base directory
    if not str(target).startswith(str(base) + os.sep):
        return None
    if target.is_file():
        return target
    return None
