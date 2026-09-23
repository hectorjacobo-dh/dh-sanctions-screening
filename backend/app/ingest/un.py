"""UN Security Council consolidated list (XML).

Gap closed relative to the legacy process
-----------------------------------------
The previous Power Query expanded ``INDIVIDUALS/INDIVIDUAL`` only. The UN
consolidated file also contains an ``ENTITIES/ENTITY`` block — UN-designated
companies, banks and organisations. Those were never loaded, so no partner was
ever screened against them. This adapter parses both blocks.

Aliases (``INDIVIDUAL_ALIAS`` / ``ENTITY_ALIAS``) are retained rather than
collapsed, because UN designations frequently list the transliteration a
partner would actually register under.
"""

from __future__ import annotations

from lxml import etree

from .base import CanonicalTarget, clean, join_unique
from .identity import label_identifier

SOURCE = "UN Sanctions"

_INDIVIDUAL_NAME_FIELDS = ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")


def _text(node, path: str) -> str:
    found = node.find(path)
    return clean(found.text) if found is not None else ""


def _all_text(node, path: str) -> list[str]:
    return [clean(el.text) for el in node.findall(path) if clean(el.text)]


def _parse_individual(node) -> CanonicalTarget | None:
    parts = [_text(node, field) for field in _INDIVIDUAL_NAME_FIELDS]
    primary = " ".join(part for part in parts if part).strip()
    if not primary:
        primary = _text(node, "NAME_ORIGINAL_SCRIPT")
    if not primary:
        return None

    nationalities = _all_text(node, "NATIONALITY/VALUE")
    countries = _all_text(node, "INDIVIDUAL_ADDRESS/COUNTRY")
    births = _all_text(node, "INDIVIDUAL_DATE_OF_BIRTH/DATE")
    births += _all_text(node, "INDIVIDUAL_DATE_OF_BIRTH/YEAR")

    aliases = _all_text(node, "INDIVIDUAL_ALIAS/ALIAS_NAME")
    original = _text(node, "NAME_ORIGINAL_SCRIPT")
    if original and original != primary:
        aliases.append(original)

    # Documents are published structurally: type, number and issuing country in
    # separate elements. Rendered into the same one-line shape the other three
    # sources produce, so the UI has a single format to display.
    documents = [
        label_identifier(
            _text(doc, "TYPE_OF_DOCUMENT") or "Document",
            _text(doc, "NUMBER"),
            _text(doc, "ISSUING_COUNTRY") or _text(doc, "COUNTRY_OF_ISSUE"),
            _text(doc, "NOTE"),
        )
        for doc in node.findall("INDIVIDUAL_DOCUMENT")
    ]

    birth_place = " ".join(
        part
        for part in (
            _text(node, "INDIVIDUAL_PLACE_OF_BIRTH/CITY"),
            _text(node, "INDIVIDUAL_PLACE_OF_BIRTH/STATE_PROVINCE"),
            _text(node, "INDIVIDUAL_PLACE_OF_BIRTH/COUNTRY"),
        )
        if part
    )

    return CanonicalTarget(
        source=SOURCE,
        watchlist_id=_text(node, "DATAID"),
        target_name=primary,
        entity_type="individual",
        sanctions_program=_text(node, "UN_LIST_TYPE"),
        remarks="",
        # Address country only; nationality is a separate assertion.
        geography=join_unique(countries),
        dob=join_unique(births),
        aliases=aliases,
        nationality=join_unique(nationalities),
        place_of_birth=birth_place,
        gender=_text(node, "GENDER"),
        position=join_unique(_all_text(node, "TITLE/VALUE")),
        identifiers=join_unique(documents, separator=" | "),
        # COMMENTS1 is the UN's narrative summary of reasons — the published
        # justification, in the Committee's own words.
        listing_reason=_text(node, "COMMENTS1"),
        linked_to=join_unique(_all_text(node, "DESIGNATION/VALUE")),
        listed_on=_text(node, "LISTED_ON"),
    )


def _parse_entity(node) -> CanonicalTarget | None:
    primary = _text(node, "FIRST_NAME") or _text(node, "NAME_ORIGINAL_SCRIPT")
    if not primary:
        return None

    aliases = _all_text(node, "ENTITY_ALIAS/ALIAS_NAME")
    original = _text(node, "NAME_ORIGINAL_SCRIPT")
    if original and original != primary:
        aliases.append(original)

    countries = _all_text(node, "ENTITY_ADDRESS/COUNTRY")

    return CanonicalTarget(
        source=SOURCE,
        watchlist_id=_text(node, "DATAID"),
        target_name=primary,
        entity_type="entity",
        sanctions_program=_text(node, "UN_LIST_TYPE"),
        remarks="",
        geography=join_unique(countries),
        aliases=aliases,
        listing_reason=_text(node, "COMMENTS1"),
        listed_on=_text(node, "LISTED_ON"),
    )


def ingest(payload: bytes) -> list[CanonicalTarget]:
    # resolve_entities=False blocks XXE / billion-laughs against a remote feed.
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=True, recover=True
    )
    root = etree.fromstring(payload, parser=parser)

    targets: list[CanonicalTarget] = []

    for node in root.findall(".//INDIVIDUALS/INDIVIDUAL"):
        target = _parse_individual(node)
        if target:
            targets.append(target)

    # Previously missing entirely — UN-designated organisations.
    for node in root.findall(".//ENTITIES/ENTITY"):
        target = _parse_entity(node)
        if target:
            targets.append(target)

    return targets
