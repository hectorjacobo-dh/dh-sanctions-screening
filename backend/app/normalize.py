"""Name normalisation — the pre-processing control.

This module implements the deterministic transformation applied identically to
both watchlist targets and partner names before any similarity scoring. Because
adjudication defensibility depends on reproducibility, every function here is
pure: same input always yields the same output, with no dependence on locale,
clock or database state.

Pipeline:
    raw -> unicode fold -> transliterate -> upper -> punctuation strip
        -> legal-form strip -> whitespace collapse

The legal-form strip is what fixes the "UAC" vs "United Aircraft Corporation"
class of miss: it removes the corporate wrapper so the *core* trading name is
compared, and the acronym helpers below bridge the remaining gap.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from unidecode import unidecode

# ---------------------------------------------------------------------------
# Legal forms
# ---------------------------------------------------------------------------
# Suffixes stripped from the END of a name. Multi-word entries are matched as
# contiguous token runs. Sourced from the jurisdictions Delivery Hero brands
# operate in plus the major sanctions geographies.
LEGAL_SUFFIXES: frozenset[tuple[str, ...]] = frozenset(
    tuple(s.split())
    for s in [
        # Anglophone
        "LTD", "LIMITED", "LTD LIABILITY COMPANY", "LLC", "L L C", "LC",
        "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
        "PLC", "LLP", "LP", "PARTNERSHIP", "TRUST", "HOLDINGS", "HOLDING",
        "GROUP", "ENTERPRISES", "ENTERPRISE", "INTERNATIONAL", "INTL",
        "PRIVATE LIMITED", "PVT LTD", "PVT", "PTY LTD", "PTY", "PTE LTD", "PTE",
        # German / Austrian / Swiss
        "GMBH", "MBH", "GMBH CO KG", "AG", "KG", "KGAA", "OHG", "EG", "GBR",
        "UG", "SE",
        # French / Belgian / Luxembourg
        "SARL", "S A R L", "SAS", "SASU", "SA", "S A", "SNC", "SCS", "SCA", "EURL",
        # Spanish / LATAM
        "SOCIEDAD DE RESPONSABILIDAD LIMITADA", "SOCIEDAD ANONIMA",
        "SOCIEDAD ANONIMA DEPORTIVA", "SOCIEDAD LIMITADA", "SOCIEDAD",
        "RESPONSABILIDAD LIMITADA", "LIMITADA",
        "SL", "S L", "SLU", "SRL", "S R L", "SA DE CV", "S A DE C V",
        "SAPI DE CV", "S DE RL", "S DE R L", "S DE RL DE CV", "SAC", "SAS DE CV",
        "EIRL", "LTDA", "CIA LTDA", "CIA", "Y CIA",
        # Italian / Portuguese
        "SPA", "S P A", "SRLS", "SNC", "SAPA", "LDA", "UNIPESSOAL LDA",
        # Nordic / Dutch / Baltic
        "BV", "B V", "NV", "N V", "VOF", "CV", "AB", "AS", "A S", "ASA", "OY",
        "OYJ", "APS", "IVS", "HF", "EHF", "OU", "SIA", "UAB", "AS OU",
        # Central & Eastern Europe
        "KFT", "ZRT", "NYRT", "BT", "SP Z OO", "SPOLKA Z OO", "SA SP Z OO",
        "SRO", "S R O", "AS SRO", "DOO", "D O O", "AD", "EOOD", "OOD",
        # CIS / Russia / Central Asia
        "OOO", "O O O", "ZAO", "OAO", "PAO", "AO", "TOO", "IP", "JSC",
        "OJSC", "CJSC", "PJSC", "LLC OOO",
        # Middle East / Gulf
        "WLL", "W L L", "LLC SPC", "SPC", "FZE", "FZC", "FZ LLC", "FZ",
        "FZCO", "DMCC", "EST", "ESTABLISHMENT", "TRADING EST", "SAOG", "SAOC",
        "QSC", "QPSC", "KSC", "KSCC", "BSC", "BSC C",
        # Asia-Pacific
        "SDN BHD", "SDN", "BHD", "BERHAD", "TBK", "PERSERO", "KK", "YK",
        "CO LTD", "CO LTD JAPAN", "HK", "LIMITADA",
        # Generic trade descriptors that behave as wrappers
        "TRADING", "TRADING LLC", "GENERAL TRADING", "RESTAURANT",
        "RESTAURANTS", "RESTAURANT LLC", "CATERING", "FOODSTUFF",
        "FOODSTUFF TRADING", "CAFE", "CAFETERIA", "KITCHEN", "FOODS", "FOOD",
    ]
)

# Prefixes stripped from the START of a name (Indonesia, Russia, Turkey, ...).
LEGAL_PREFIXES: frozenset[tuple[str, ...]] = frozenset(
    tuple(s.split())
    for s in [
        "PT", "PT PERSERO", "CV", "UD", "OOO", "ZAO", "OAO", "PAO", "AO",
        "JSC", "OJSC", "CJSC", "PJSC", "TOO", "SP Z OO", "MS", "MESSRS",
        "THE", "AL", "EL",
    ]
)

_MAX_SUFFIX_TOKENS = max(len(s) for s in LEGAL_SUFFIXES)
_MAX_PREFIX_TOKENS = max(len(p) for p in LEGAL_PREFIXES)

# Characters that carry no matching signal. Ampersand and slash become spaces
# rather than being deleted so "A&B" -> "A B" rather than "AB".
_SEPARATORS = re.compile(r"[^0-9A-Z]+")
_WHITESPACE = re.compile(r"\s+")

# Latin noise that transliteration leaves behind for Arabic/Cyrillic scripts.
_ARABIC_ARTICLE = re.compile(r"\bAL\s+(?=[A-Z])")


def fold_unicode(value: str) -> str:
    """Strip diacritics and transliterate non-Latin scripts to ASCII.

    Sanctions lists mix scripts freely; UN and EU entries in particular carry
    Cyrillic and Arabic original-script names. Transliterating both sides gives
    the scorer a common alphabet.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unidecode(without_marks)


def basic_normalize(value: str | None) -> str:
    """Case-fold, transliterate and remove punctuation. No legal-form removal."""
    if not value:
        return ""
    folded = fold_unicode(str(value)).upper()
    spaced = _SEPARATORS.sub(" ", folded)
    return _WHITESPACE.sub(" ", spaced).strip()


def _collapse_initial_runs(tokens: list[str]) -> list[str]:
    """Merge runs of two or more single-character tokens into one token.

    Punctuation removal turns "L.L.C." into "L L C" and spaced-out branding
    ("U N N I E STORE") into a string of one-letter tokens. Left alone, those
    letters match single letters anywhere in a designation — vessel names like
    "SILVIA I", initials like "HAN, Kwon U" — and generate large volumes of
    meaningless alerts. Merging restores the intended word ("LLC", "UNNIE").

    A lone single-character token is preserved: it is a genuine initial.
    """
    collapsed: list[str] = []
    run: list[str] = []
    for token in tokens:
        if len(token) == 1:
            run.append(token)
            continue
        if run:
            collapsed.append("".join(run) if len(run) > 1 else run[0])
            run = []
        collapsed.append(token)
    if run:
        collapsed.append("".join(run) if len(run) > 1 else run[0])
    return collapsed


def _strip_prefixes(tokens: list[str]) -> list[str]:
    changed = True
    while changed and len(tokens) > 1:
        changed = False
        for size in range(min(_MAX_PREFIX_TOKENS, len(tokens) - 1), 0, -1):
            if tuple(tokens[:size]) in LEGAL_PREFIXES:
                tokens = tokens[size:]
                changed = True
                break
    return tokens


def _strip_suffixes(tokens: list[str]) -> list[str]:
    changed = True
    while changed and len(tokens) > 1:
        changed = False
        for size in range(min(_MAX_SUFFIX_TOKENS, len(tokens) - 1), 0, -1):
            if tuple(tokens[-size:]) in LEGAL_SUFFIXES:
                tokens = tokens[:-size]
                changed = True
                break
    return tokens


@lru_cache(maxsize=1_000_000)
def normalize_name(value: str | None) -> str:
    """Full pre-processing control: the canonical form used for scoring.

    Guarantees a non-empty result whenever the input had any alphanumeric
    content — if stripping legal forms would empty the name (e.g. a partner
    literally called "Trading LLC"), the pre-strip form is kept so the row is
    still screened rather than silently dropped.
    """
    base = basic_normalize(value)
    if not base:
        return ""
    base = _ARABIC_ARTICLE.sub("AL", base)
    tokens = _collapse_initial_runs(base.split())
    core = _strip_suffixes(_strip_prefixes(list(tokens)))
    return " ".join(core) if core else " ".join(tokens)


@lru_cache(maxsize=1_000_000)
def name_tokens(value: str | None) -> tuple[str, ...]:
    """Normalised token tuple, used for blocking and token-set scoring."""
    normalized = normalize_name(value)
    return tuple(normalized.split()) if normalized else ()


def _initials(tokens: tuple[str, ...] | list[str]) -> str:
    useful = [t for t in tokens if len(t) > 1]
    if len(useful) < 2:
        return ""
    return "".join(t[0] for t in useful)


@lru_cache(maxsize=1_000_000)
def acronym_of(value: str | None) -> str:
    """Conventional acronym, built from the name *including* its legal form.

    "United Aircraft Corporation" -> "UAC". The legal form must be kept here:
    real-world acronyms almost always incorporate it (UAC, IBM, BASF), so
    building initials from the suffix-stripped core would yield "UA" and miss
    the match entirely.
    """
    return _initials(basic_normalize(value).split())


@lru_cache(maxsize=1_000_000)
def acronyms_of(value: str | None) -> frozenset[str]:
    """All plausible acronyms for a name.

    Four readings are produced, because usage is inconsistent and the legal
    form may sit on either end:

        "PJSC United Aircraft Corporation"
            all tokens              -> PUAC
            prefix stripped         -> UAC   <- the one people actually use
            suffix stripped         -> PUA
            prefix+suffix stripped  -> UA

    Indexing and scoring accept a hit on any of them.
    """
    base = basic_normalize(value)
    if not base:
        return frozenset()

    tokens = _collapse_initial_runs(base.split())
    views = {
        tuple(tokens),
        tuple(_strip_prefixes(list(tokens))),
        tuple(_strip_suffixes(list(tokens))),
        tuple(_strip_suffixes(_strip_prefixes(list(tokens)))),
    }
    return frozenset(a for a in (_initials(v) for v in views) if a)


def is_acronym_candidate(value: str | None) -> bool:
    """True when a name looks like it *is* an acronym (single short token)."""
    tokens = name_tokens(value)
    if len(tokens) != 1:
        return False
    token = tokens[0]
    return 2 <= len(token) <= 6 and token.isalpha()


_ALL_LEGAL_TOKENS: frozenset[str] = frozenset(
    token for phrase in (LEGAL_SUFFIXES | LEGAL_PREFIXES) for token in phrase
)

# Corporate boilerplate: words that appear in a registered name without
# carrying identity. Used when pricing how much of a designation a match left
# unexplained — "PUBLIC JOINT STOCK COMPANY GAZPROM NEFT" is essentially
# "GAZPROM NEFT" wearing four words of Russian company-law dressing, and a
# search for "gazprom" should not be penalised for them.
#
# Deliberately excludes identity-bearing words that merely look generic:
# BANK, PETROLEUM, AIRLINES, SHIPPING, INSURANCE and the like distinguish one
# designation from another and must keep costing.
GENERIC_TOKENS: frozenset[str] = frozenset(
    """
    PUBLIC PRIVATE OPEN CLOSED JOINT STOCK SHARE SHARES COMPANY COMPANIES
    CORPORATION CORP INCORPORATED LIMITED LIABILITY UNLIMITED PARTNERSHIP
    ENTERPRISE ENTERPRISES FIRM CONCERN COMBINE ASSOCIATION ORGANISATION
    ORGANIZATION SOCIETY UNION FEDERAL STATE NATIONAL REGIONAL MUNICIPAL
    GENERAL CENTRAL UNITARY SUBSIDIARY AFFILIATE BRANCH DIVISION GROUP
    HOLDING HOLDINGS INTERNATIONAL GLOBAL WORLDWIDE TRADING COMMERCIAL
    INDUSTRIAL INDUSTRIES INDUSTRY MANUFACTURING PRODUCTION SERVICES SERVICE
    SOLUTIONS SYSTEMS SYSTEM TECHNOLOGIES TECHNOLOGY SCIENTIFIC RESEARCH
    DEVELOPMENT MANAGEMENT INVESTMENT INVESTMENTS CAPITAL FINANCE FINANCIAL
    THE AND OF FOR
    """.split()
)


def is_generic_token(token: str) -> bool:
    """True when a token is corporate boilerplate rather than an identifier."""
    return token in GENERIC_TOKENS or token in _ALL_LEGAL_TOKENS


def is_generic_legal_form(value: str | None) -> bool:
    """True when a name carries no identity beyond its corporate form.

    Regulator feeds contain occasional degenerate rows whose name field holds
    only a legal form (the EU list publishes entries reading merely "sociedad
    de responsabilidad limitada"). Screening against those matches every
    company registered under that form in the entire partner base, so they are
    excluded at ingestion rather than adjudicated thousands of times.
    """
    tokens = basic_normalize(value).split()
    if not tokens:
        return True
    return all(token in _ALL_LEGAL_TOKENS or token.isdigit() for token in tokens)


def squashed(value: str | None) -> str:
    """Normalised name with all spaces removed — used for trigram blocking."""
    return normalize_name(value).replace(" ", "")


def trigrams(value: str) -> set[str]:
    """Character trigrams of a squashed name."""
    if len(value) < 3:
        return {value} if value else set()
    return {value[i : i + 3] for i in range(len(value) - 2)}
