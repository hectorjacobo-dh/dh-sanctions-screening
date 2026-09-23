"""Resolving a regulator's download URL when the published one breaks.

Two of these URLs were already dead once during this project, and the EU's
carries a ``token=`` parameter that can be rotated without notice. A feed that
breaks is the most likely maintenance event this system will face, and it fails
quietly: the list simply stops refreshing while everything else keeps working.

What this can and cannot do
---------------------------
It can try a list of known-good mirrors, check that what came back is really a
sanctions list, and remember which one worked. It cannot invent a URL that does
not exist, and it cannot survive a *format* change — if OFAC restructures its
CSV, an adapter needs editing by a person.

Why every candidate is validated, not merely fetched
----------------------------------------------------
``https://ofac.treasury.gov/media/5926/download`` answers 200, is not an error
page, and is not the SDN list: 199 KB against the real file's 5.7 MB. A
fallback chain that trusted the status code would have silently replaced the
watchlist with something 96% smaller, and every subsequent screening would have
come back clean for the wrong reason.

So a candidate is accepted only if it parses with the source's own adapter AND
yields a plausible number of records against what that source produced last
time. A fallback that quietly halves the watchlist is worse than a stale list,
because staleness is visible on the dashboard and a shrunken list is not.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# A replacement must produce at least this share of the records the source
# produced at its last successful refresh. Designations are added and removed
# continually, but never by 40% between two runs — a drop that large means the
# wrong file, not a quiet day at OFAC.
MIN_RECORD_RATIO = 0.60

# Below this, a response is a redirect stub or an error page, whatever it says.
MIN_PAYLOAD_BYTES = 10_000

_HTML_PREFIXES = (b"<!doctype html", b"<html", b"<!DOCTYPE HTML")


@dataclass(frozen=True)
class Endpoint:
    """Where one regulator's list can be obtained, in order of preference."""

    key: str
    candidates: tuple[str, ...]
    # Restricts discovery to the regulator's own domains. A link harvested from
    # a page must never be able to point the ingest at somewhere else.
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    # Pages to scrape for a current link when every candidate has failed.
    discovery_pages: tuple[str, ...] = ()
    link_pattern: str = ""


# Verified reachable and byte-identical to the primary on 23 September 2026,
# except where noted. Ordered: the published URL first, mirrors after.
ENDPOINTS: dict[str, Endpoint] = {
    "OFAC SDN": Endpoint(
        key="OFAC SDN",
        candidates=(
            "https://www.treasury.gov/ofac/downloads/sdn.csv",
            # Same 5,695,725 bytes as the primary.
            "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV",
        ),
        allowed_hosts=frozenset({"treasury.gov", "ofac.treas.gov", "ofac.treasury.gov"}),
        discovery_pages=(
            "https://ofac.treasury.gov/specially-designated-nationals-and-blocked-persons-list-sdn-human-readable-lists",
        ),
        link_pattern=r"href=[\"']([^\"']*sdn\.csv[^\"']*)",
    ),
    "OFAC Non-SDN": Endpoint(
        key="OFAC Non-SDN",
        candidates=(
            "https://www.treasury.gov/ofac/downloads/consolidated/cons_prim.csv",
            "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_PRIM.CSV",
        ),
        allowed_hosts=frozenset({"treasury.gov", "ofac.treas.gov", "ofac.treasury.gov"}),
    ),
    "EU Sanctions": Endpoint(
        key="EU Sanctions",
        candidates=(
            "https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList/content?token=dG9rZW4tMjAxNw",
        ),
        allowed_hosts=frozenset({"europa.eu", "ec.europa.eu", "webgate.ec.europa.eu"}),
    ),
    "UK Sanctions": Endpoint(
        key="UK Sanctions",
        candidates=("https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv",),
        allowed_hosts=frozenset({"fcdo.gov.uk", "gov.uk", "publishing.service.gov.uk"}),
        discovery_pages=("https://www.gov.uk/government/publications/the-uk-sanctions-list",),
        link_pattern=r"href=[\"']([^\"']*[Ss]anctions[^\"']*\.csv[^\"']*)",
    ),
    "UN Sanctions": Endpoint(
        key="UN Sanctions",
        candidates=(
            "https://unsolprodfiles.blob.core.windows.net/publiclegacyxmlfiles/EN/consolidated.xml",
            # Reachable, but NOT byte-identical (2.18 MB against 2.54 MB). A
            # genuine fallback rather than a mirror, so it sits second and its
            # use is recorded.
            "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
        ),
        allowed_hosts=frozenset({"un.org", "scsanctions.un.org", "blob.core.windows.net"}),
    ),
}


def looks_like_a_list(payload: bytes) -> str:
    """Cheap shape check. Returns a reason to reject, or "" to continue."""
    if len(payload) < MIN_PAYLOAD_BYTES:
        return f"only {len(payload):,} bytes — a stub or an error page"
    head = payload[:200].lstrip()
    if head[:15].lower().startswith(_HTML_PREFIXES[:2]):
        return "an HTML page, not a data file"
    return ""


class LearnedEndpoints:
    """Remembers which URL last worked, so a recovery is not repeated daily.

    Stored beside the database rather than in it: an operator must be able to
    read and correct it without a database client, and it is cache, not record
    — the audit trail is where the change is actually evidenced.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        if path.exists():
            try:
                self._data = {
                    str(k): str(v) for k, v in json.loads(path.read_text("utf-8")).items()
                }
            except (json.JSONDecodeError, OSError):
                logger.warning("learned endpoints file unreadable; ignoring it")

    def get(self, key: str) -> str:
        return self._data.get(key, "")

    def remember(self, key: str, url: str) -> None:
        if self._data.get(key) == url:
            return
        self._data[key] = url
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True), "utf-8")
        except OSError:
            logger.warning("could not persist the learned endpoint for %s", key)


@dataclass
class Attempt:
    url: str
    ok: bool
    detail: str
    records: int = 0


@dataclass
class Resolution:
    """The outcome of trying to obtain one source."""

    payload: bytes
    url: str
    attempts: list[Attempt]
    records: int
    # The parsed result is carried back so the caller does not parse twice.
    # It is not a micro-optimisation: the OFAC adapters fetch two companion
    # files (addresses and aliases) on every call, so a second parse means two
    # more downloads per refresh.
    targets: list = field(default_factory=list)

    @property
    def recovered(self) -> bool:
        """True when the published URL failed and a fallback succeeded."""
        return len(self.attempts) > 1


def candidate_order(endpoint: Endpoint, learned: str, configured: str) -> list[str]:
    """Configured URL first, then whatever worked last, then the known list.

    The configured value wins so an operator can always override this by
    editing the environment — automation that cannot be overridden is a trap
    during an incident.
    """
    ordered: list[str] = []
    for url in (configured, learned, *endpoint.candidates):
        if url and url not in ordered:
            ordered.append(url)
    return ordered


def discover(
    endpoint: Endpoint, fetch: Callable[[str], bytes]
) -> list[str]:
    """Scrape the regulator's own page for a current download link.

    Restricted to the regulator's domains: a harvested link is untrusted input,
    and following one off-domain would let a compromised or mistyped page
    redirect the entire watchlist ingest somewhere else.
    """
    if not (endpoint.discovery_pages and endpoint.link_pattern):
        return []

    found: list[str] = []
    for page in endpoint.discovery_pages:
        try:
            html = fetch(page).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            logger.info("discovery page unavailable (%s): %s", page, exc)
            continue

        for href in re.findall(endpoint.link_pattern, html):
            url = href if href.startswith("http") else _absolute(page, href)
            if _host_allowed(url, endpoint.allowed_hosts) and url not in found:
                found.append(url)
    return found


def _absolute(page: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(page, href)


def _host_allowed(url: str, allowed: frozenset[str]) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host or not allowed:
        return False
    return any(host == a or host.endswith("." + a) for a in allowed)


def resolve(
    endpoint: Endpoint,
    *,
    configured_url: str,
    learned_url: str,
    fetch: Callable[[str], bytes],
    parse: Callable[[bytes], list],
    last_known_count: int = 0,
) -> Resolution:
    """Obtain a source's payload, trying alternatives until one validates.

    ``parse`` runs the source's real adapter. Whether it parses, and into how
    many records, is the only check that actually distinguishes the list from a
    plausible-looking file of the wrong kind.
    """
    attempts: list[Attempt] = []
    floor = int(last_known_count * MIN_RECORD_RATIO) if last_known_count else 1

    urls = candidate_order(endpoint, learned_url, configured_url)
    for url in urls:
        try:
            payload = fetch(url)
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            attempts.append(Attempt(url, False, f"{type(exc).__name__}: {exc}"[:200]))
            continue

        if reason := looks_like_a_list(payload):
            attempts.append(Attempt(url, False, reason))
            continue

        try:
            targets = parse(payload)
            count = len(targets)
        except Exception as exc:  # noqa: BLE001
            attempts.append(Attempt(url, False, f"did not parse: {type(exc).__name__}"[:200]))
            continue

        if count < floor:
            attempts.append(
                Attempt(
                    url,
                    False,
                    f"parsed only {count:,} records against {last_known_count:,} last time "
                    f"— refusing a source that would shrink the watchlist",
                    count,
                )
            )
            continue

        attempts.append(Attempt(url, True, "accepted", count))
        return Resolution(payload, url, attempts, count, targets)

    # Everything known has failed. Ask the regulator's own page what the link
    # is now, and put any answer through exactly the same validation.
    for url in discover(endpoint, fetch):
        if any(a.url == url for a in attempts):
            continue
        try:
            payload = fetch(url)
            if reason := looks_like_a_list(payload):
                attempts.append(Attempt(url, False, f"discovered, but {reason}"))
                continue
            targets = parse(payload)
            count = len(targets)
        except Exception as exc:  # noqa: BLE001
            attempts.append(Attempt(url, False, f"discovered, but failed: {type(exc).__name__}"))
            continue

        if count < floor:
            attempts.append(Attempt(url, False, f"discovered, but only {count:,} records", count))
            continue

        attempts.append(Attempt(url, True, "accepted after discovery", count))
        return Resolution(payload, url, attempts, count, targets)

    tried = "; ".join(f"{a.url} -> {a.detail}" for a in attempts) or "no candidates configured"
    raise RuntimeError(f"no working source for {endpoint.key}. Tried: {tried}")
