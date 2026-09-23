"""Automatic cross-check of an evidence document against a designation.

Given the text of a document an officer attached to an alert, this looks for
the things that actually decide a sanctions adjudication:

* does the document name the **designated party**, or only the partner?
* do the **dates of birth** in it agree or conflict with the designation?
* do any **identifiers** in it appear on the designation?
* do the **countries** it mentions corroborate or contradict?

It produces findings and a *recommendation*. It does **not** decide.

Why it must not decide
----------------------
Every input here is unreliable in a way that matters. Text extraction from a
PDF can silently return a fragment. A document can be genuine and simply not
mention the designated party. A DOB can differ because the regulator recorded
it differently, not because it is a different person — which is precisely why
the identity fields elsewhere in this system never filter results either.

Clearing a sanctions alert is the one action in this application that removes a
party from scrutiny. An automated recommendation may order the queue and draft
the rationale; a person still has to accept it, and the audit trail records
which of the two did so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .geography import resolve_country
from .normalize import basic_normalize, is_generic_token, name_tokens

# Dates in the formats regulators and registries actually use.
_DATE_PATTERNS = (
    re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b"),          # 12/03/1975
    re.compile(r"\b(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})\b"),          # 1975-03-12
    re.compile(
        r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{4})\b",
        re.IGNORECASE,
    ),
)
_YEAR = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
# Registration / passport / tax identifiers: mixed alphanumerics of real length.
_IDENTIFIER = re.compile(r"\b(?=[A-Z0-9\-]{6,20}\b)(?=[^\s]*\d)[A-Z0-9][A-Z0-9\-]{4,18}\b")

VERDICT_CORROBORATES = "corroborates"
VERDICT_CONTRADICTS = "contradicts"
VERDICT_NEUTRAL = "neutral"
VERDICT_UNREADABLE = "unreadable"


@dataclass
class Finding:
    check: str
    verdict: str
    detail: str


@dataclass
class CrossCheck:
    readable: bool
    extraction_status: str
    extraction_detail: str
    findings: list[Finding] = field(default_factory=list)
    recommendation: str = ""
    suggested_reason_code: str = ""
    confidence: str = "none"      # none | low | moderate
    requires_human: bool = True   # always

    def as_dict(self) -> dict:
        return {
            "readable": self.readable,
            "extraction_status": self.extraction_status,
            "extraction_detail": self.extraction_detail,
            "findings": [vars(f) for f in self.findings],
            "recommendation": self.recommendation,
            "suggested_reason_code": self.suggested_reason_code,
            "confidence": self.confidence,
            "requires_human_confirmation": True,
        }


def _distinctive(tokens: tuple[str, ...]) -> list[str]:
    """Tokens that carry identity — the ones worth searching a document for."""
    return [t for t in tokens if len(t) > 2 and not is_generic_token(t)]


def _name_presence(name: str, haystack: str) -> tuple[bool, float, list[str]]:
    """Is ``name`` present in the document text, allowing spelling variance?"""
    tokens = _distinctive(name_tokens(name))
    if not tokens:
        return False, 0.0, []
    found, missing = [], []
    for token in tokens:
        if token in haystack:
            found.append(token)
            continue
        # Tolerate a transcription slip on a long token.
        if len(token) >= 6:
            window = max(len(token) - 1, 5)
            candidates = {
                haystack[i : i + len(token)]
                for i in range(0, max(0, len(haystack) - window), 1)
                if haystack[i : i + 1] == token[:1]
            }
            if any(fuzz.ratio(token, c) >= 88 for c in candidates):
                found.append(token)
                continue
        missing.append(token)
    coverage = len(found) / len(tokens)
    return coverage >= 0.75, coverage, missing


def _dates(text: str) -> set[str]:
    years: set[str] = set()
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groups()
            year = next((g for g in groups if len(str(g)) == 4 and str(g).isdigit()), None)
            if year:
                years.add(str(year))
    years |= set(_YEAR.findall(text))
    return years


def cross_check(
    *,
    text: str,
    extraction_status: str,
    extraction_detail: str,
    partner_name: str,
    target_name: str,
    target_dob: str,
    target_geography: str,
    target_watchlist_id: str,
    target_remarks: str,
) -> CrossCheck:
    """Compare a document against the designation it is offered against."""
    result = CrossCheck(
        readable=extraction_status == "ok" and bool(text.strip()),
        extraction_status=extraction_status,
        extraction_detail=extraction_detail,
    )

    if not result.readable:
        result.findings.append(
            Finding(
                "Document readability",
                VERDICT_UNREADABLE,
                extraction_detail
                or "No text could be extracted, so nothing in this document has "
                "been verified automatically.",
            )
        )
        result.recommendation = (
            "This document could not be read, so it provides NO automatic "
            "corroboration. Review it by eye before relying on it, and do not "
            "treat its presence as evidence in itself."
        )
        result.confidence = "none"
        return result

    haystack = basic_normalize(text)

    # --- 1. Does the document mention the designated party? ---------------
    target_present, target_cov, target_missing = _name_presence(target_name, haystack)
    if target_present:
        result.findings.append(
            Finding(
                "Designated party named in document",
                VERDICT_CONTRADICTS,
                f"The designation '{target_name}' appears in this document "
                f"({target_cov:.0%} of its distinctive terms). A document that "
                "names the designated party tends to support a match, not clear it.",
            )
        )
    else:
        result.findings.append(
            Finding(
                "Designated party named in document",
                VERDICT_CORROBORATES,
                f"No mention of '{target_name}' found"
                + (f" (missing: {', '.join(target_missing[:5])})" if target_missing else "")
                + ". Consistent with an unrelated party, though absence is weak "
                "evidence on its own.",
            )
        )

    # --- 2. Does it mention the partner? ----------------------------------
    partner_present, partner_cov, _ = _name_presence(partner_name, haystack)
    result.findings.append(
        Finding(
            "Partner named in document",
            VERDICT_CORROBORATES if partner_present else VERDICT_NEUTRAL,
            f"'{partner_name}' {'appears' if partner_present else 'does not appear'} "
            f"in this document ({partner_cov:.0%} of distinctive terms). "
            + (
                ""
                if partner_present
                else "A document that does not name the partner may not be about them."
            ),
        )
    )

    # --- 3. Dates of birth / incorporation --------------------------------
    doc_years = _dates(text)
    target_years = set(_YEAR.findall(target_dob or ""))
    if target_years and doc_years:
        overlap = target_years & doc_years
        result.findings.append(
            Finding(
                "Date comparison",
                VERDICT_CONTRADICTS if overlap else VERDICT_CORROBORATES,
                (
                    f"Designation year(s) {', '.join(sorted(target_years))} also appear "
                    "in the document — this points towards the same subject."
                    if overlap
                    else f"Designation year(s) {', '.join(sorted(target_years))} do not "
                    f"appear. Document years found: {', '.join(sorted(doc_years)[:8])}."
                ),
            )
        )
    elif doc_years and not target_years:
        result.findings.append(
            Finding(
                "Date comparison",
                VERDICT_NEUTRAL,
                f"Document contains year(s) {', '.join(sorted(doc_years)[:8])} but the "
                "designation records no date, so they cannot be compared.",
            )
        )

    # --- 4. Identifiers ---------------------------------------------------
    doc_ids = {m.group(0) for m in _IDENTIFIER.finditer(haystack)}
    designation_blob = basic_normalize(f"{target_watchlist_id} {target_remarks}")
    shared = {i for i in doc_ids if i and i in designation_blob}
    if shared:
        result.findings.append(
            Finding(
                "Identifier comparison",
                VERDICT_CONTRADICTS,
                f"Identifier(s) {', '.join(sorted(shared)[:4])} appear both in this "
                "document and on the designation. Verify carefully — this points "
                "towards the same subject.",
            )
        )
    elif doc_ids:
        result.findings.append(
            Finding(
                "Identifier comparison",
                VERDICT_NEUTRAL,
                f"{len(doc_ids)} identifier(s) found in the document "
                f"(e.g. {', '.join(sorted(doc_ids)[:3])}); none matches the "
                "designation record. Note the designation may simply not publish one.",
            )
        )

    # --- 5. Country -------------------------------------------------------
    target_countries = resolve_country(target_geography)
    doc_countries = resolve_country(text[:20000])
    if target_countries and doc_countries:
        overlap = target_countries & doc_countries
        result.findings.append(
            Finding(
                "Country comparison",
                VERDICT_CONTRADICTS if overlap else VERDICT_CORROBORATES,
                (
                    f"Both refer to {', '.join(sorted(overlap))}."
                    if overlap
                    else f"Designation country {', '.join(sorted(target_countries))} vs "
                    f"document {', '.join(sorted(doc_countries)[:5])}. Note a "
                    "designated party can operate through a third country, so this "
                    "is weak grounds alone."
                ),
            )
        )

    # --- recommendation ---------------------------------------------------
    supporting = sum(1 for f in result.findings if f.verdict == VERDICT_CORROBORATES)
    against = sum(1 for f in result.findings if f.verdict == VERDICT_CONTRADICTS)

    if against:
        result.recommendation = (
            f"{against} finding(s) point TOWARDS this being the designated party. "
            "Do not clear this alert on the strength of this document without "
            "resolving them."
        )
        result.suggested_reason_code = ""
        result.confidence = "moderate" if against > 1 else "low"
    elif supporting >= 2:
        result.recommendation = (
            f"{supporting} finding(s) are consistent with an unrelated party. "
            "This may support a dismissal — confirm the document is genuine and "
            "actually concerns this partner, then state the grounds yourself."
        )
        result.suggested_reason_code = (
            "DIFFERENT_ENTITY_SAME_NAME" if partner_present else ""
        )
        result.confidence = "low"
    else:
        result.recommendation = (
            "Nothing decisive found. This document neither corroborates nor "
            "contradicts the designation on its own."
        )
        result.confidence = "none"

    return result
