"""Structured identity extraction.

Every regulator publishes date of birth, nationality, place of birth and
identity-document numbers. Until now this adapter layer threw almost all of it
away: nationality was folded into ``geography`` alongside street addresses, and
OFAC's identity data was left sitting unparsed inside the free-text ``remarks``
column, so an OFAC individual displayed no date of birth at all even though the
published record carried one.

That is not cosmetic. Date of birth, nationality and document number are the
three discriminators an officer uses to tell a true match from a namesake, and
"not provided" against a record that does provide it invites a wrong
adjudication in the confident direction.

OFAC's identity grammar
-----------------------
OFAC has no identity columns. It has one free-text ``remarks`` field holding
semicolon-delimited clauses, each opening with a label:

    DOB 15 Apr 1963; citizen Venezuela; Gender Male;
    Cedula No. 8370825 (Venezuela); Passport A0237802 (Venezuela).

The clause labels below were not guessed. They were counted across all 19,848
published OFAC rows and ordered by frequency, and ``audit_clauses`` re-runs that
census so an unrecognised label shows up as residue rather than vanishing.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# clause labels
# ---------------------------------------------------------------------------

# Matched longest-first: "National ID No." must win over "Identification
# Number", and "Business Registration Number" over "Registration Number".
_ID_LABELS = (
    "Aircraft Manufacturer's Serial Number",
    "Vessel Registration Identification",
    "Unified Social Credit Code",
    "Commercial Registry Number",
    "Business Registration Number",
    "Certificate of Incorporation Number",
    "Chamber of Commerce Number",
    "Government Gazette Number",
    "Economic Register Number",
    "Public Registration Number",
    "Digital Currency Address",
    "Folio Mercantil No.",
    "Registration Number",
    "Identification Number",
    "Legal Entity Number",
    "Driver's License No.",
    "National ID No.",
    "Trade License No.",
    "Registration ID",
    "Company Number",
    "Passport",
    "Tax ID No.",
    "Cedula No.",
    "Fiscal Code",
    "C.U.R.P.",
    "R.F.C.",
    "RFC",
    "SSN",
    "NIT",
    "MMSI",
    "IMO",
    "SWIFT/BIC",
    "SWIFT",
    "VAT",
    "LEI",
    "Email Address",
    "Website",
    "Phone Number",
    "Equity Ticker",
    "ISIN",
    "D.N.I.",
    "RUC #",
    "RUC",
    "BIK",
    "CBU",
    "Dubai Chamber of Commerce Membership No.",
    "US FEIN",
    "License",
    "Fax",
    "Telephone",
)

# Vessel and aircraft attributes. Identity for an asset, but not for a person,
# so they are kept in their own bucket rather than shown as personal identity.
_ASSET_LABELS = (
    "Former Vessel Flag",
    "Other Vessel Flag",
    "Vessel Year of Build",
    "Vessel Gross Registered Tonnage",
    "Vessel Registration Port",
    "Vessel Tonnage",
    "Vessel Length",
    "Vessel Owner",
    "Vessel Flag",
    "Vessel Type",
    "Aircraft Manufacture Date",
    "Aircraft Tail Number",
    "Aircraft Operator",
    "Aircraft Model",
    "Aircraft Mode S Transponder Code",
    "Previous Aircraft Tail Number",
    "Linked To Aircraft",
    "Nationality of Registration",
)

# Clauses describing the *measure* rather than the person. Kept, but as
# programme context — they are not identity and must never read as such.
_MEASURE_PREFIXES = (
    "Secondary sanctions risk",
    "Additional Sanctions Information",
    "Transactions Prohibited For",
    "Executive Order",
    "Organization Established Date",
    "Organization Type",
    "Target Type",
    "For more information",
    "Listing date",
    "Effective Date",
    "Subject to Secondary Sanctions",
)

# Contact/­web detail: real data, but neither identity nor rationale.
_CONTACT_LABELS = ("Email Address", "Website", "Phone Number", "Fax", "Telephone")

_DOB = re.compile(r"^(?:alt\.\s*)?DOB\s+(.+)$", re.I)
_POB = re.compile(r"^(?:alt\.\s*)?POB\s+(.+)$", re.I)
_NATIONALITY = re.compile(r"^(?:alt\.\s*)?nationality\s+(.+)$", re.I)
_CITIZEN = re.compile(r"^(?:alt\.\s*)?citizen\s+(.+)$", re.I)
_GENDER = re.compile(r"^Gender\s+(.+)$", re.I)
_LINKED = re.compile(r"^Linked To:\s*(.+)$", re.I)
_AKA = re.compile(r"^(?:alt\.\s*)?a\.k\.a\.\s*[:']?\s*(.+?)'?$", re.I)

# A label is followed by a separator or end-of-clause. NOT \b: a label ending
# in "." ("Cedula No.", "D.N.I.") has no word boundary before the following
# space, so every one of them failed to match and its number was misfiled as
# prose. Caught by running the parser over all 19,544 published OFAC remarks.
_SEP = r"(?=[\s:]|$)"


def _label_alternation(labels: tuple[str, ...]) -> str:
    return "|".join(re.escape(label) for label in labels)


_ID_CLAUSE = re.compile(
    r"^(?:alt\.\s*)?(" + _label_alternation(_ID_LABELS) + r")" + _SEP + r"[:\s]*(.*)$", re.I
)
_ASSET_CLAUSE = re.compile(
    r"^(?:alt\.\s*)?(" + _label_alternation(_ASSET_LABELS) + r")" + _SEP + r"[:\s]*(.*)$", re.I
)
# Fallback: an unlisted label that still self-describes as a number.
_ID_GENERIC = re.compile(
    r"^(?:alt\.\s*)?([A-Z][\w.'/ -]{2,40}?\s*(?:No\.|Number|Code|ID|#))" + _SEP + r"[:\s]*(.+)$"
)


@dataclass(slots=True)
class Identity:
    """Identity and rationale fields recovered from a published record."""

    dob: list[str] = field(default_factory=list)
    place_of_birth: list[str] = field(default_factory=list)
    nationality: list[str] = field(default_factory=list)
    gender: str = ""
    identifiers: list[str] = field(default_factory=list)
    linked_to: list[str] = field(default_factory=list)
    programme_notes: list[str] = field(default_factory=list)
    narrative: list[str] = field(default_factory=list)
    asset_details: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)

    LIST_FIELDS = (
        "dob",
        "place_of_birth",
        "nationality",
        "identifiers",
        "linked_to",
        "programme_notes",
        "narrative",
        "asset_details",
        "aliases",
    )

    def merge(self, other: "Identity") -> None:
        for name in (*self.LIST_FIELDS, "unclassified"):
            getattr(self, name).extend(getattr(other, name))
        self.gender = self.gender or other.gender


def _squash(value: str) -> str:
    """Collapse whitespace, keeping punctuation intact."""
    return re.sub(r"\s+", " ", (value or "").strip())


def _tidy(value: str) -> str:
    """Trim a clause. OFAC ends the final clause of a record with a period.

    Not applied to labels: it would turn "Cedula No." into "Cedula No".
    """
    return _squash(value).rstrip(".").strip()


def dedupe(values: list[str]) -> list[str]:
    """Order-preserving case-insensitive dedupe."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = _tidy(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def split_clauses(remarks: str) -> list[str]:
    return [c for c in (_tidy(part) for part in re.split(r";\s*", remarks or "")) if c]


def parse_ofac_remarks(remarks: str) -> Identity:
    """Split OFAC's free-text remarks into structured identity fields."""
    out = Identity()
    for clause in split_clauses(remarks):
        if match := _DOB.match(clause):
            out.dob.append(match.group(1))
        elif match := _POB.match(clause):
            out.place_of_birth.append(match.group(1))
        elif match := _NATIONALITY.match(clause):
            out.nationality.append(match.group(1))
        elif match := _CITIZEN.match(clause):
            # OFAC distinguishes the two; an officer comparing a passport needs
            # to know which one the list actually asserts.
            out.nationality.append(f"{match.group(1)} (citizen)")
        elif match := _GENDER.match(clause):
            out.gender = out.gender or _tidy(match.group(1))
        elif match := _LINKED.match(clause):
            out.linked_to.append(match.group(1))
        elif match := _AKA.match(clause):
            out.aliases.append(match.group(1))
        elif clause.startswith(_MEASURE_PREFIXES):
            out.programme_notes.append(clause)
        elif match := _ASSET_CLAUSE.match(clause):
            out.asset_details.append(clause)
        elif match := _ID_CLAUSE.match(clause):
            label, value = _squash(match.group(1)), _tidy(match.group(2))
            if label in _CONTACT_LABELS:
                out.programme_notes.append(clause)
            elif value:
                out.identifiers.append(f"{label} {value}")
            else:
                out.unclassified.append(clause)
        elif match := _ID_GENERIC.match(clause):
            out.identifiers.append(f"{_squash(match.group(1))} {_tidy(match.group(2))}")
        elif not any(ch.isdigit() for ch in clause):
            # Unlabelled prose with no number is a role, rank or reason —
            # "Member of the Constituent Assembly", "Brigadier General".
            out.narrative.append(clause)
        elif len(clause) > 25:
            out.narrative.append(clause)
        else:
            out.unclassified.append(clause)

    for name in out.LIST_FIELDS:
        setattr(out, name, dedupe(getattr(out, name)))
    return out


def label_identifier(kind: str, number: str, country: str = "", note: str = "") -> str:
    """Render one identity document the way OFAC renders them, for consistency.

    The other three regulators publish document type, number and issuing
    country in separate columns; formatting them identically means the UI has
    one shape to display and an officer reads one format everywhere.
    """
    kind = _tidy(kind) or "Identifier"
    number = _tidy(number)
    if not number:
        return ""
    out = f"{kind} {number}"
    if country := _tidy(country):
        out += f" ({country})"
    if note := _tidy(note):
        out += f" - {note}"
    return out


def audit_clauses(remark_values: list[str]) -> tuple[Counter, Counter]:
    """Census of clause labels, and of what the parser failed to classify.

    Used by the test suite to hold coverage above a floor: a regulator adding a
    new label should surface as a visible gap, not as silently dropped identity
    data.
    """
    classified: Counter = Counter()
    residue: Counter = Counter()
    for remarks in remark_values:
        parsed = parse_ofac_remarks(remarks)
        for name in parsed.LIST_FIELDS:
            classified[name] += len(getattr(parsed, name))
        if parsed.gender:
            classified["gender"] += 1
        for clause in parsed.unclassified:
            residue[re.match(r"[^0-9]{0,40}", clause).group(0).strip() or clause] += 1
    return classified, residue


# The country that issued an identity document, as every regulator writes it:
# "Passport A0237802 (Venezuela)", "Identification Number 179029472 (Colombia)".
_ISSUER = re.compile(r"\(([^)]{2,60})\)")


def issuing_countries(identifiers: str) -> list[str]:
    """Countries named on a party's identity documents.

    Deliberately NOT treated as nationality. A state issues documents to people
    who are not its nationals — a residence card, a tax number, a company
    registration — so folding this into the nationality field would assert
    something the regulator did not.

    OFAC SDN 44522 is the case in point: an identification number issued by
    Colombia, an address in Venezuela, and no nationality published at all.
    Reporting "Colombian" there would be an invention; reporting nothing throws
    away the only state-issued evidence on the record. It is its own signal.
    """
    if not identifiers:
        return []
    out: list[str] = []
    for candidate in _ISSUER.findall(identifiers):
        cleaned = _tidy(candidate)
        # "expires 08 Apr 2013" and similar notes also sit in brackets.
        if cleaned and not any(ch.isdigit() for ch in cleaned) and cleaned not in out:
            out.append(cleaned)
    return out
