"""OFAC (US Treasury) — SDN and Consolidated Non-SDN lists.

Improvement over the legacy Power Query model
---------------------------------------------
The spreadsheet ingested only ``sdn.csv``, which carries no country and no
aliases. That had two consequences: OFAC rows could never participate in a
geographic control, and every designated party was matchable only under its
single primary spelling.

This adapter additionally pulls the companion files OFAC publishes alongside
the primary list:
  * ``add.csv``  -> registered country, giving OFAC rows a geography
  * ``alt.csv``  -> AKA / FKA spellings, giving alias recall

Both are joined on ``ent_num``. If a companion file is unavailable the primary
list still ingests, degraded but functional.
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict

from .base import CanonicalTarget, clean, fetch, join_unique
from .identity import parse_ofac_remarks

logger = logging.getLogger(__name__)

SOURCE_SDN = "OFAC SDN"
SOURCE_CONS = "OFAC Non-SDN"

# Positional schemas — these files are published without a header row.
_PRIMARY_COLUMNS = 12
_ENT_NUM, _NAME, _TYPE, _PROGRAM, _TITLE = 0, 1, 2, 3, 4
_REMARKS = 11

_ADD_ENT_NUM, _ADD_COUNTRY = 0, 4
_ALT_ENT_NUM, _ALT_NAME = 0, 3


def _read_rows(payload: bytes) -> list[list[str]]:
    text = payload.decode("utf-8", errors="replace")
    return [row for row in csv.reader(io.StringIO(text)) if row]


def _companion_map(url: str, key_index: int, value_index: int) -> dict[str, list[str]]:
    """Fetch a companion CSV and group ``value_index`` by ``key_index``."""
    mapping: dict[str, list[str]] = defaultdict(list)
    try:
        result = fetch(url)
    except Exception as exc:  # noqa: BLE001 - degrade, never fail the whole refresh
        logger.warning("OFAC companion file unavailable (%s): %s", url, exc)
        return mapping

    for row in _read_rows(result.content):
        if len(row) <= max(key_index, value_index):
            continue
        key = clean(row[key_index])
        value = clean(row[value_index])
        if key and value:
            mapping[key].append(value)
    return mapping


def parse_primary(
    payload: bytes,
    source: str,
    *,
    countries: dict[str, list[str]] | None = None,
    aliases: dict[str, list[str]] | None = None,
) -> list[CanonicalTarget]:
    countries = countries or {}
    aliases = aliases or {}
    targets: list[CanonicalTarget] = []

    for row in _read_rows(payload):
        if len(row) < _PRIMARY_COLUMNS:
            row = row + [""] * (_PRIMARY_COLUMNS - len(row))

        name = clean(row[_NAME])
        if not name:
            continue

        ent_num = clean(row[_ENT_NUM])
        # OFAC encodes "this is an organisation" as the null sentinel "-0-".
        entity_type = clean(row[_TYPE]) or "entity"

        # OFAC publishes no identity columns at all: date of birth, nationality
        # and every document number live inside the free-text remarks field.
        # Left unparsed, an OFAC individual showed "no DOB" against a record
        # that plainly stated one.
        ident = parse_ofac_remarks(clean(row[_REMARKS]))

        # The title is the party's role ("Member of the Constituent Assembly").
        # Previously concatenated onto remarks with " | ", which corrupted the
        # first clause and produced labels like "Colonel | DOB".
        title = clean(row[_TITLE])

        targets.append(
            CanonicalTarget(
                source=source,
                watchlist_id=ent_num,
                target_name=name,
                entity_type=entity_type,
                sanctions_program=clean(row[_PROGRAM]),
                remarks=clean(row[_REMARKS]),
                geography=join_unique(countries.get(ent_num, [])),
                # alt.csv is the authoritative alias file; remarks carry a few
                # more as "a.k.a. '...'" clauses. Both feed recall.
                aliases=list(aliases.get(ent_num, [])) + ident.aliases,
                dob=join_unique(ident.dob),
                nationality=join_unique(ident.nationality),
                place_of_birth=join_unique(ident.place_of_birth),
                gender=ident.gender,
                position=title,
                identifiers=join_unique(ident.identifiers, separator=" | "),
                # OFAC states no prose reason. What it does state is the
                # programme, the role, and who the party is linked to — which
                # together are the closest thing to a published basis.
                listing_reason=join_unique(ident.narrative, separator=" | "),
                linked_to=join_unique(ident.linked_to, separator=" | "),
            )
        )
    return targets


def ingest_sdn(payload: bytes, *, add_url: str = "", alt_url: str = "") -> list[CanonicalTarget]:
    countries = _companion_map(add_url, _ADD_ENT_NUM, _ADD_COUNTRY) if add_url else {}
    aliases = _companion_map(alt_url, _ALT_ENT_NUM, _ALT_NAME) if alt_url else {}
    return parse_primary(payload, SOURCE_SDN, countries=countries, aliases=aliases)


def ingest_consolidated(
    payload: bytes, *, add_url: str = "", alt_url: str = ""
) -> list[CanonicalTarget]:
    countries = _companion_map(add_url, _ADD_ENT_NUM, _ADD_COUNTRY) if add_url else {}
    aliases = _companion_map(alt_url, _ALT_ENT_NUM, _ALT_NAME) if alt_url else {}
    return parse_primary(payload, SOURCE_CONS, countries=countries, aliases=aliases)
