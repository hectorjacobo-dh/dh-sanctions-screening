"""Country resolution and the Delivery Hero regional mapping.

Two jobs:
  * Resolve free-text country strings (from four regulators, each with its own
    spelling conventions) onto ISO 3166-1 alpha-2 codes so the geographic
    control compares like with like.
  * Map an account country onto the reporting region used in the existing
    adjudication workbook (APAC / Europe / Glovo / LATAM / MENA - HS /
    MENA - talabat).

The region mapping is operational, not geographic — "Glovo" is a brand that
spans several continents. It is therefore loaded from ``data/region_map.json``
when present so Compliance can maintain it without a code change, and the table
below is only the fallback default.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .normalize import basic_normalize

# ---------------------------------------------------------------------------
# Country aliases -> ISO alpha-2
# ---------------------------------------------------------------------------
_COUNTRY_ALIASES: dict[str, str] = {
    # Delivery Hero operating markets
    "BAHRAIN": "BH", "KUWAIT": "KW", "QATAR": "QA", "OMAN": "OM",
    "UNITED ARAB EMIRATES": "AE", "UAE": "AE", "U A E": "AE", "EMIRATES": "AE",
    "SAUDI ARABIA": "SA", "KINGDOM OF SAUDI ARABIA": "SA", "KSA": "SA",
    "JORDAN": "JO", "IRAQ": "IQ", "EGYPT": "EG", "LEBANON": "LB",
    "TURKEY": "TR", "TURKIYE": "TR", "PAKISTAN": "PK", "BANGLADESH": "BD",
    "SINGAPORE": "SG", "MALAYSIA": "MY", "THAILAND": "TH", "TH": "TH",
    "PHILIPPINES": "PH", "TAIWAN": "TW", "HONG KONG": "HK", "JAPAN": "JP",
    "SOUTH KOREA": "KR", "KOREA REPUBLIC OF": "KR", "REPUBLIC OF KOREA": "KR",
    "CAMBODIA": "KH", "LAOS": "LA", "LAO PEOPLES DEMOCRATIC REPUBLIC": "LA",
    "MYANMAR": "MM", "BURMA": "MM", "VIETNAM": "VN", "VIET NAM": "VN",
    "GERMANY": "DE", "AUSTRIA": "AT", "SWITZERLAND": "CH", "SWEDEN": "SE",
    "NORWAY": "NO", "DENMARK": "DK", "FINLAND": "FI", "SPAIN": "ES",
    "PORTUGAL": "PT", "ITALY": "IT", "FRANCE": "FR", "POLAND": "PL",
    "CZECH REPUBLIC": "CZ", "CZECHIA": "CZ", "SLOVAKIA": "SK", "HUNGARY": "HU",
    "ROMANIA": "RO", "BULGARIA": "BG", "CROATIA": "HR", "SLOVENIA": "SI",
    "SERBIA": "RS", "GREECE": "GR", "NETHERLANDS": "NL", "BELGIUM": "BE",
    "LUXEMBOURG": "LU", "IRELAND": "IE", "UNITED KINGDOM": "GB", "UK": "GB",
    "GREAT BRITAIN": "GB", "ENGLAND": "GB", "SCOTLAND": "GB", "WALES": "GB",
    "UKRAINE": "UA", "MOLDOVA": "MD", "GEORGIA": "GE", "ARMENIA": "AM",
    "AZERBAIJAN": "AZ", "KAZAKHSTAN": "KZ", "UZBEKISTAN": "UZ",
    "KYRGYZSTAN": "KG", "TAJIKISTAN": "TJ", "TURKMENISTAN": "TM",
    "ARGENTINA": "AR", "BOLIVIA": "BO", "CHILE": "CL", "COLOMBIA": "CO",
    "COSTA RICA": "CR", "DOMINICAN REPUBLIC": "DO", "ECUADOR": "EC",
    "EL SALVADOR": "SV", "GUATEMALA": "GT", "HONDURAS": "HN", "MEXICO": "MX",
    "NICARAGUA": "NI", "PANAMA": "PA", "PARAGUAY": "PY", "PERU": "PE",
    "URUGUAY": "UY", "VENEZUELA": "VE",
    "KENYA": "KE", "NIGERIA": "NG", "GHANA": "GH", "MOROCCO": "MA",
    "TUNISIA": "TN", "ALGERIA": "DZ", "LIBYA": "LY", "SUDAN": "SD",
    "SOUTH AFRICA": "ZA", "IVORY COAST": "CI", "COTE D IVOIRE": "CI",
    # Frequently designated jurisdictions
    "RUSSIA": "RU", "RUSSIAN FEDERATION": "RU", "IRAN": "IR",
    "IRAN ISLAMIC REPUBLIC OF": "IR", "NORTH KOREA": "KP",
    "KOREA DEMOCRATIC PEOPLES REPUBLIC OF": "KP", "DPRK": "KP",
    "SYRIA": "SY", "SYRIAN ARAB REPUBLIC": "SY", "CUBA": "CU",
    "BELARUS": "BY", "AFGHANISTAN": "AF", "YEMEN": "YE", "SOMALIA": "SO",
    "ZIMBABWE": "ZW", "CENTRAL AFRICAN REPUBLIC": "CF", "MALI": "ML",
    "SOUTH SUDAN": "SS", "ERITREA": "ER", "BURUNDI": "BI",
    "CONGO DEMOCRATIC REPUBLIC OF THE": "CD", "DR CONGO": "CD",
    "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "USA": "US",
    "CHINA": "CN", "INDIA": "IN", "INDONESIA": "ID", "AUSTRALIA": "AU",
    "NEW ZEALAND": "NZ", "CANADA": "CA", "BRAZIL": "BR", "ISRAEL": "IL",
    "PALESTINE": "PS", "PALESTINIAN": "PS", "CYPRUS": "CY", "MALTA": "MT",
    "SEYCHELLES": "SC", "PANAMA CITY": "PA", "BRITISH VIRGIN ISLANDS": "VG",
    "CAYMAN ISLANDS": "KY",
    # Spellings the regulators publish that ISO 3166-1 does not carry. Taken
    # from the values actually present in the four lists, not invented: each
    # one was failing to resolve and is annotated with how often it appeared.
    "KOREA NORTH": "KP",                      # 365 occurrences
    "KOREA SOUTH": "KR",                      # 6
    "DEMOCRATIC REPUBLIC OF THE CONGO": "CD",  # 59
    "MACAU": "MO",
    "MAN ISLE OF": "IM",
}

# Demonyms appearing in nationality fields (UN and UK publish nationality,
# not registration country).
_DEMONYMS: dict[str, str] = {
    "RUSSIAN": "RU", "IRANIAN": "IR", "SYRIAN": "SY", "CUBAN": "CU",
    "BELARUSIAN": "BY", "AFGHAN": "AF", "YEMENI": "YE", "SOMALI": "SO",
    "IRAQI": "IQ", "LIBYAN": "LY", "SUDANESE": "SD", "TURKISH": "TR",
    "EGYPTIAN": "EG", "LEBANESE": "LB", "JORDANIAN": "JO", "SAUDI": "SA",
    "KUWAITI": "KW", "QATARI": "QA", "BAHRAINI": "BH", "OMANI": "OM",
    "EMIRATI": "AE", "PAKISTANI": "PK", "BANGLADESHI": "BD", "INDIAN": "IN",
    "CHINESE": "CN", "JAPANESE": "JP", "KOREAN": "KR", "THAI": "TH",
    "MALAYSIAN": "MY", "SINGAPOREAN": "SG", "INDONESIAN": "ID",
    "FILIPINO": "PH", "VIETNAMESE": "VN", "CAMBODIAN": "KH", "BURMESE": "MM",
    "GERMAN": "DE", "AUSTRIAN": "AT", "SWISS": "CH", "SWEDISH": "SE",
    "NORWEGIAN": "NO", "DANISH": "DK", "FINNISH": "FI", "SPANISH": "ES",
    "PORTUGUESE": "PT", "ITALIAN": "IT", "FRENCH": "FR", "POLISH": "PL",
    "GREEK": "GR", "DUTCH": "NL", "BELGIAN": "BE", "IRISH": "IE",
    "BRITISH": "GB", "UKRAINIAN": "UA", "AMERICAN": "US", "MEXICAN": "MX",
    "BRAZILIAN": "BR", "ARGENTINE": "AR", "ARGENTINIAN": "AR",
    "COLOMBIAN": "CO", "PERUVIAN": "PE", "CHILEAN": "CL", "VENEZUELAN": "VE",
}

_ISO_CODES = frozenset(_COUNTRY_ALIASES.values()) | frozenset(_DEMONYMS.values())

# Region fallback. Overridden by data/region_map.json when that file exists.
_DEFAULT_REGION_BY_ISO: dict[str, str] = {
    "BH": "MENA - talabat", "KW": "MENA - talabat", "QA": "MENA - talabat",
    "OM": "MENA - talabat", "AE": "MENA - talabat", "IQ": "MENA - talabat",
    "JO": "MENA - talabat", "EG": "MENA - HS", "TR": "MENA - HS",
    "PK": "MENA - HS", "BD": "MENA - HS",
    "SG": "APAC", "MY": "APAC", "TH": "APAC", "PH": "APAC", "TW": "APAC",
    "HK": "APAC", "JP": "APAC", "KR": "APAC", "KH": "APAC", "LA": "APAC",
    "MM": "APAC", "VN": "APAC", "BN": "APAC",
    "DE": "Europe", "AT": "Europe", "CH": "Europe", "SE": "Europe",
    "NO": "Europe", "DK": "Europe", "FI": "Europe", "CZ": "Europe",
    "HU": "Europe", "BG": "Europe", "RO": "Europe", "GR": "Europe",
    "ES": "Glovo", "PT": "Glovo", "IT": "Glovo", "PL": "Glovo",
    "HR": "Glovo", "RS": "Glovo", "SI": "Glovo", "SK": "Glovo",
    "UA": "Glovo", "GE": "Glovo", "AM": "Glovo", "MD": "Glovo",
    "KZ": "Glovo", "KG": "Glovo", "MA": "Glovo", "TN": "Glovo",
    "CI": "Glovo", "KE": "Glovo", "GH": "Glovo", "NG": "Glovo",
    "AR": "LATAM", "BO": "LATAM", "CL": "LATAM", "CO": "LATAM",
    "CR": "LATAM", "DO": "LATAM", "EC": "LATAM", "SV": "LATAM",
    "GT": "LATAM", "HN": "LATAM", "MX": "LATAM", "NI": "LATAM",
    "PA": "LATAM", "PY": "LATAM", "PE": "LATAM", "UY": "LATAM",
    "VE": "LATAM",
}

_SPLITTERS = ("/", ";", ",", "|")


# ---------------------------------------------------------------------------
# ISO 3166-1 index
# ---------------------------------------------------------------------------
# The hand-maintained tables above cover this team's markets and the demonyms
# regulators publish. They did not cover the rest of the world, and three gaps
# mattered:
#
#   * alpha-3 codes. The EU publishes "RUS", "IRN", "PRK", "SYR", "BLR"; the
#     resolver understood only alpha-2, so 1,945 Russian designations and 553
#     Iranian ones could not take part in the geographic control at all.
#   * comma-inverted names: "Korea, North", "Congo, Democratic Republic of the".
#   * countries simply absent — Rwanda, Uganda, Haiti, Liberia, Maldives,
#     Bosnia and Herzegovina, Marshall Islands.
#
# 18.1% of published country values resolved to nothing, and silently: the
# control reports "country unknown" and applies no adjustment, which is
# indistinguishable from a country that genuinely was not stated.
#
# pycountry is the ISO 3166-1 register, so this stays correct without being
# hand-maintained. The tables above still win, because they carry local usage
# ("UAE", "KSA", "Burma") that ISO does not.

# Bare forms naming more than one state. Resolving them would be a guess
# presented as a fact.
_AMBIGUOUS_HEADS = frozenset({"KOREA", "CONGO", "VIRGIN ISLANDS", "SUDAN", "SAMOA"})

# Designated parties are recorded here, but it is not in ISO 3166-1.
_NON_ISO: dict[str, str] = {
    "KOSOVO": "XK",
    "REPUBLIC OF KOSOVO": "XK",
    # Territories the regulators name directly. Screened as Palestine, which
    # is the ISO entry a designation against them is compared with.
    "REGION GAZA": "PS",
    "GAZA": "PS",
    "GAZA STRIP": "PS",
    "REGION WEST BANK": "PS",
    "WEST BANK": "PS",
}

# Qualifiers appended to a country value. "Iran (citizen)" is produced by this
# application's own OFAC parser, which marks citizenship distinctly from
# nationality; unstripped, the value matched nothing.
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")


@lru_cache(maxsize=1)
def _iso_index() -> dict[str, str]:
    """Every ISO name, alpha-2 and alpha-3 spelling, mapped to alpha-2."""
    try:
        import pycountry
    except ImportError:  # pragma: no cover - degrade to the hand tables
        return {}

    index: dict[str, str] = {}
    for country in pycountry.countries:
        code = country.alpha_2
        for spelling in (
            country.name,
            country.alpha_2,
            country.alpha_3,
            getattr(country, "common_name", None),
            getattr(country, "official_name", None),
        ):
            if not spelling:
                continue
            key = basic_normalize(spelling)
            if key:
                index.setdefault(key, code)
            if "," in str(spelling):
                head, _, tail = str(spelling).partition(",")
                inverted = basic_normalize(f"{tail.strip()} {head.strip()}")
                if inverted:
                    index.setdefault(inverted, code)
                head_key = basic_normalize(head)
                if head_key and head_key not in _AMBIGUOUS_HEADS:
                    index.setdefault(head_key, code)
    return index


@lru_cache(maxsize=1)
def country_catalogue() -> list[dict[str, str]]:
    """The country list offered in the interface, as full names.

    Full names only: a picker offering "RUS" asks an officer to know alpha-3,
    and the point of the control is that they should not have to. The matcher
    still accepts codes, because the published data contains them.
    """
    try:
        import pycountry
    except ImportError:  # pragma: no cover
        return []

    catalogue = [
        {"name": getattr(c, "common_name", None) or c.name, "code": c.alpha_2}
        for c in pycountry.countries
    ]
    catalogue.append({"name": "Kosovo", "code": "XK"})
    return sorted(catalogue, key=lambda entry: entry["name"])


@lru_cache(maxsize=100_000)
def resolve_country(value: str | None) -> frozenset[str]:
    """Resolve a free-text country/nationality field to ISO alpha-2 codes.

    Regulators publish multi-valued geography (the existing Power Query joins
    address country and nationality with " / "), so this returns a *set*. An
    empty set means "unknown" — which the geographic control treats as "no
    evidence", never as "no match".
    """
    if not value:
        return frozenset()

    found: set[str] = set()
    # Split only on separators that genuinely divide two countries. The comma
    # is NOT one of them: ISO publishes "Korea, North" and "Congo, Democratic
    # Republic of the", and splitting those first threw away the half that says
    # which Korea and which Congo. It did not merely fail — "Congo, Democratic
    # Republic of the" resolved to CG, the *other* Congo, so the geographic
    # control compared a DR Congo designation against the wrong state and
    # reported a contradiction that does not exist.
    for segment in re.split(r"[/;|\n]", str(value)):
        segment = segment.strip()
        if not segment:
            continue

        # Exactly as published first, commas and parentheses and all: several
        # ISO names contain brackets of their own ("Cocos (Keeling) Islands",
        # "Falkland Islands (Malvinas)"), and stripping them first turned a
        # valid country into an unmatchable fragment.
        code = _match_country(basic_normalize(segment))
        if not code:
            # Then without a trailing qualifier, for values like
            # "Iran (citizen)" that this application's own parser produces.
            segment = _PARENTHETICAL.sub("", segment).strip()
            code = _match_country(basic_normalize(segment))
        if code:
            found.add(code)
            continue

        # Only now treat the comma as a separator, for values that really do
        # list several ("Tehran, Iran").
        for part in segment.split(","):
            key = basic_normalize(part)
            if not key:
                continue
            code = _match_country(key)
            if code:
                found.add(code)
                continue
            # Substring sweep for embedded names ("BORN IN TEHRAN, IRAN").
            for alias, iso in _COUNTRY_ALIASES.items():
                if len(alias) > 4 and alias in key:
                    found.add(iso)
    return frozenset(found)


def _match_country(key: str) -> str | None:
    """One normalised string to one alpha-2 code, or None."""
    if not key:
        return None
    if key in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[key]
    if key in _DEMONYMS:
        return _DEMONYMS[key]
    if key in _NON_ISO:
        return _NON_ISO[key]
    if len(key) == 2 and key in _ISO_CODES:
        return key
    if key in _iso_index():
        return _iso_index()[key]
    # Regulators prefix a definite article ("The Gambia") and abbreviate saints
    # ("St Kitts and Nevis"); ISO spells neither that way.
    if key.startswith("THE "):
        return _match_country(key[4:])
    if key.startswith("ST "):
        return _match_country("SAINT " + key[3:])
    return None


@lru_cache(maxsize=1)
def _region_overrides() -> dict[str, str]:
    from .config import get_settings

    path: Path = get_settings().data_dir / "region_map.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(k).strip().upper(): str(v) for k, v in raw.items() if v}


def resolve_region(country: str | None) -> str:
    """Map an account country to its Delivery Hero reporting region."""
    if not country:
        return "Unmapped"
    overrides = _region_overrides()
    key = basic_normalize(country)
    if key in overrides:
        return overrides[key]
    codes = resolve_country(country)
    for code in sorted(codes):
        if code in overrides:
            return overrides[code]
        if code in _DEFAULT_REGION_BY_ISO:
            return _DEFAULT_REGION_BY_ISO[code]
    return "Unmapped"


def countries_contradict(
    partner_country: str | None, target_geography: str | None
) -> bool:
    """True only when both sides are known *and* share no country.

    Deliberately conservative. If either side is unknown — which is the case
    for every OFAC row, since OFAC publishes no structured country — this
    returns False and no penalty is applied. Absence of evidence must never be
    read as evidence of absence in a sanctions control.
    """
    partner_codes = resolve_country(partner_country)
    target_codes = resolve_country(target_geography)
    if not partner_codes or not target_codes:
        return False
    return partner_codes.isdisjoint(target_codes)
