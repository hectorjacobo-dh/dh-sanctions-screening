"""Shared ingestion plumbing.

Each regulator publishes a different shape; every adapter flattens onto
``CanonicalTarget``, which mirrors the ``Master_*`` schema already used by the
Compliance team's Power Query model so downstream reports stay familiar.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import httpx

from ..config import get_settings


@dataclass(slots=True)
class CanonicalTarget:
    """One designated party in the shared schema."""

    source: str
    target_name: str
    watchlist_id: str = ""
    entity_type: str = ""
    sanctions_program: str = ""
    remarks: str = ""
    geography: str = ""
    dob: str = ""
    aliases: list[str] = field(default_factory=list)

    # --- structured identity -------------------------------------------------
    # Held apart from ``geography`` on purpose. Geography mixes a registered
    # address with an operating country; nationality is a legal assertion about
    # the person. Collapsing them made it impossible to tell an officer whether
    # the list actually claims a nationality, which is one of the three fields
    # they use to separate a true match from a namesake.
    nationality: str = ""
    place_of_birth: str = ""
    gender: str = ""
    position: str = ""
    identifiers: str = ""

    # --- why the party is listed --------------------------------------------
    # The regulator's own words where it publishes them: the UK's Statement of
    # Reasons, the UN's narrative summary, the EU's remark. Never paraphrased —
    # an adjudication has to be able to quote the source.
    listing_reason: str = ""
    linked_to: str = ""
    listed_on: str = ""

    def alias_string(self) -> str:
        seen: dict[str, None] = {}
        for alias in self.aliases:
            cleaned = (alias or "").strip()
            if cleaned and cleaned.casefold() != self.target_name.strip().casefold():
                seen.setdefault(cleaned, None)
        return " / ".join(seen)


@dataclass(slots=True)
class FetchResult:
    content: bytes
    url: str
    sha256: str

    @property
    def size(self) -> int:
        return len(self.content)

    def text(self, encoding: str = "utf-8") -> str:
        return self.content.decode(encoding, errors="replace")


def fetch(url: str, *, timeout: int | None = None) -> FetchResult:
    """Download a watchlist payload and fingerprint it.

    The SHA-256 is stored against the refresh record so an auditor can prove
    which exact published file a screening run was performed against.
    """
    settings = get_settings()
    timeout_seconds = timeout or settings.http_timeout_seconds
    headers = {
        # Several regulator endpoints reject requests without a UA string.
        "User-Agent": "DH-Sanctions-Screening/1.0 (compliance batch ingestion)",
        "Accept": "*/*",
    }
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        content = response.content

    return FetchResult(
        content=content,
        url=str(response.url),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def clean(value: object) -> str:
    """Trim a raw field and normalise the regulators' null sentinels."""
    if value is None:
        return ""
    text = str(value).strip()
    if text in {"-0-", "N/A", "NA", "None", "null", "nan"}:
        return ""
    return text


def decode_text(payload: bytes) -> str:
    """Decode a published CSV, tolerating the encoding each regulator ships.

    OFSI publishes Windows-1252, not UTF-8. Decoding it as UTF-8 with
    ``errors="replace"`` turned every curly apostrophe into U+FFFD, so a
    Statement of Reasons read "the National Assembly<?>s Special Commission" —
    quoted verbatim into an adjudication record. Strict UTF-8 first (it is the
    common case and its failure is unambiguous), then cp1252, which cannot
    fail and is a superset of latin-1 for the bytes in practice.
    """
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("cp1252", errors="replace")


def join_unique(values, separator: str = " / ") -> str:
    """Join distinct non-empty values, preserving first-seen order."""
    seen: dict[str, None] = {}
    for value in values:
        cleaned = clean(value)
        if cleaned:
            seen.setdefault(cleaned, None)
    return separator.join(seen)
