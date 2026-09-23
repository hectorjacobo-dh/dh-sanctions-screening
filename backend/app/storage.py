"""Evidence storage backends.

Why this exists
---------------
Evidence documents were written straight to local disk. That is correct on a
workstation and silently destructive on a managed platform: Cloud Run, App
Runner, Kubernetes and friends give a container an ephemeral filesystem, so
every attached document would disappear on the next restart or scale-to-zero —
and the audit chain would still claim, truthfully but uselessly, that a
document with that digest once existed.

Two backends, one interface:

* ``local``  — a directory. Workstation and plain-VM deployments.
* ``gcs``    — a Google Cloud Storage bucket. Managed, stateless platforms.

Objects are keyed by their own SHA-256 in both, which keeps the integrity and
de-duplication properties identical either way.

A GCS FUSE volume mount would let the local backend work unchanged on Cloud
Run, but ``store()`` relies on an atomic rename to avoid publishing a truncated
file, and rename is neither atomic nor reliably supported over FUSE. A real
client is the safer choice for something that has to be evidential.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredFile:
    sha256: str
    stored_filename: str
    size_bytes: int
    deduplicated: bool


class EvidenceStore:
    """Content-addressed store for evidence documents."""

    def save(self, payload: bytes, suffix: str) -> StoredFile:
        raise NotImplementedError

    def read(self, stored_filename: str) -> bytes | None:
        raise NotImplementedError

    def verify(self, stored_filename: str, expected_sha256: str) -> bool:
        data = self.read(stored_filename)
        return data is not None and hashlib.sha256(data).hexdigest() == expected_sha256

    def describe(self) -> str:
        raise NotImplementedError


class LocalStore(EvidenceStore):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, stored_filename: str) -> Path | None:
        """Resolve a key, refusing anything outside the store.

        Keys are digests we generated, but this is defence in depth: a crafted
        key must never be able to read an arbitrary file off the host.
        """
        root = self.root.resolve()
        candidate = (root / stored_filename).resolve()
        if root not in candidate.parents or not candidate.is_file():
            logger.warning("refused evidence path outside the store: %r", stored_filename)
            return None
        return candidate

    def save(self, payload: bytes, suffix: str) -> StoredFile:
        digest = hashlib.sha256(payload).hexdigest()
        name = f"{digest}{suffix}"
        path = self.root / name
        existed = path.exists()
        if not existed:
            # Write then rename: a crash mid-write must not leave a truncated
            # file sitting under a valid digest.
            temporary = path.with_suffix(path.suffix + ".partial")
            temporary.write_bytes(payload)
            temporary.replace(path)
        return StoredFile(digest, name, len(payload), existed)

    def read(self, stored_filename: str) -> bytes | None:
        path = self._resolve(stored_filename)
        return path.read_bytes() if path else None

    def describe(self) -> str:
        return f"local directory {self.root}"


class GcsStore(EvidenceStore):
    def __init__(self, bucket_name: str, prefix: str = "evidence") -> None:
        try:
            from google.cloud import storage  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "EVIDENCE_BACKEND=gcs requires google-cloud-storage. "
                "Install it with: pip install google-cloud-storage"
            ) from exc
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self.bucket_name = bucket_name
        self.prefix = prefix.strip("/")

    def _blob(self, stored_filename: str):  # noqa: ANN202
        # Keys are generated digests; reject anything with path structure.
        if "/" in stored_filename or "\\" in stored_filename or ".." in stored_filename:
            raise ValueError(f"invalid evidence key: {stored_filename!r}")
        return self._bucket.blob(f"{self.prefix}/{stored_filename}")

    def save(self, payload: bytes, suffix: str) -> StoredFile:
        digest = hashlib.sha256(payload).hexdigest()
        name = f"{digest}{suffix}"
        blob = self._blob(name)
        if blob.exists():
            return StoredFile(digest, name, len(payload), True)
        blob.upload_from_string(payload, content_type="application/octet-stream")
        return StoredFile(digest, name, len(payload), False)

    def read(self, stored_filename: str) -> bytes | None:
        try:
            blob = self._blob(stored_filename)
            return blob.download_as_bytes() if blob.exists() else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read evidence %r: %s", stored_filename, exc)
            return None

    def describe(self) -> str:
        return f"gs://{self.bucket_name}/{self.prefix}"


_store: EvidenceStore | None = None


def get_store() -> EvidenceStore:
    """The configured backend, built once."""
    global _store
    if _store is not None:
        return _store

    settings = get_settings()
    backend = (settings.evidence_backend or "local").strip().lower()
    if backend == "gcs":
        if not settings.evidence_bucket:
            raise RuntimeError("EVIDENCE_BACKEND=gcs requires EVIDENCE_BUCKET to be set.")
        _store = GcsStore(settings.evidence_bucket, settings.evidence_prefix)
    elif backend == "local":
        _store = LocalStore(settings.data_dir / "evidence")
    else:
        raise RuntimeError(f"Unknown EVIDENCE_BACKEND {backend!r}; expected 'local' or 'gcs'.")

    logger.info("evidence store: %s", _store.describe())
    return _store


def reset_store_cache() -> None:
    """Test hook."""
    global _store
    _store = None
