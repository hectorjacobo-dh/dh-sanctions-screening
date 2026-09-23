"""Evidence document validation and access.

Storage itself lives in ``storage.py`` behind a backend interface, so the same
code path works on a workstation (local directory) and on a managed platform
with an ephemeral filesystem (object storage). Documents are keyed by their own
SHA-256 in both, which gives three things at once:

* **No path traversal.** A supplied filename never reaches the filesystem, so
  ``../../etc/passwd`` is structurally impossible rather than filtered.
* **De-duplication.** The same trade licence attached to twelve alerts is
  stored once.
* **Integrity.** The key *is* the digest, so substitution or corruption is
  detectable by re-hashing — which the download path does before serving.

The original filename is kept in the database for display only.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .storage import StoredFile, get_store

logger = logging.getLogger(__name__)

# Document types a diligence file plausibly is. Deliberately excludes anything
# executable or script-like; this is an allowlist, not a blocklist.
ALLOWED_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff",
        ".txt", ".csv", ".md", ".rtf",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".msg", ".eml",
    }
)

MAX_EVIDENCE_BYTES = 25 * 1024 * 1024  # 25 MB per document


def validate_upload(filename: str, payload: bytes) -> str | None:
    """Return an error message when a document must not be accepted."""
    if not payload:
        return "The uploaded file is empty."
    if len(payload) > MAX_EVIDENCE_BYTES:
        return (
            f"'{filename}' is {len(payload) / 1024 / 1024:.1f} MB; the limit is "
            f"{MAX_EVIDENCE_BYTES // (1024 * 1024)} MB per document."
        )
    suffix = Path(filename or "").suffix.lower()
    if not suffix:
        return "The file needs an extension so its type can be checked."
    if suffix not in ALLOWED_SUFFIXES:
        return (
            f"'{suffix}' files are not accepted as evidence. Allowed: "
            + ", ".join(sorted(ALLOWED_SUFFIXES))
        )
    return None


def store(payload: bytes, filename: str) -> StoredFile:
    """Persist a document under its digest. Idempotent for identical content."""
    return get_store().save(payload, Path(filename or "").suffix.lower())


def read(stored_filename: str) -> bytes | None:
    """Fetch a stored document, or None if it is missing."""
    return get_store().read(stored_filename)


def verify(stored_filename: str, expected_sha256: str) -> bool:
    """Re-hash a stored document and compare it to the recorded digest."""
    return get_store().verify(stored_filename, expected_sha256)


def describe_backend() -> str:
    """Human-readable description of where evidence is being written."""
    return get_store().describe()
