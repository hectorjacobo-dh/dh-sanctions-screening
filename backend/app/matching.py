"""The scoring engine.

Design constraints that come from this being a regulated control rather than a
search feature:

* **Explainable.** Every score decomposes into named components that are
  persisted, so an auditor can reconstruct why a row scored what it scored.
* **Deterministic.** No randomness, no clock dependence, stable ordering.
* **Recall-biased.** A false positive costs an analyst a minute. A false
  negative costs a sanctions breach. Where the two trade off, we keep the row.

The geographic control lowers a *priority* score and is recorded as a reason
code — it never removes a row from the report and never drops it below the
alerting threshold on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from rapidfuzz import fuzz

from .geography import countries_contradict, resolve_country
from .normalize import (
    basic_normalize,
    strict_tokens,
    acronyms_of,
    is_acronym_candidate,
    is_generic_token,
    name_tokens,
    normalize_name,
)


class RiskBand(StrEnum):
    """System-proposed banding. The analyst still owns the final Decision."""

    STRONG = "Strong Match"
    POTENTIAL = "Potential Match"
    WEAK = "Weak Match"


class ReasonCode(StrEnum):
    EXACT_NORMALISED = "EXACT_NORMALISED_NAME"
    ACRONYM_BRIDGE = "ACRONYM_EXPANSION"
    TOKEN_SUBSET = "ALL_TOKENS_PRESENT"
    CONTAINED_NAME = "DISCRIMINATIVE_NAME_CONTAINED"
    NAME_WITHIN_DESIGNATION = "SEARCHED_NAME_WITHIN_DESIGNATION"
    ALIAS_HIT = "MATCHED_ON_ALIAS"
    GEO_CONTRADICTION = "GEO_CONTRADICTION_DEPRIORITISED"
    GEO_CONFIRMED = "GEO_CONFIRMED"
    GEO_UNKNOWN = "GEO_UNKNOWN_NO_ADJUSTMENT"
    SHORT_NAME = "SHORT_NAME_LOW_CONFIDENCE"
    COMMON_WORD = "COMMON_WORD_NAME"


@dataclass(frozen=True)
class ScoreBreakdown:
    """Component scores, persisted verbatim for audit reconstruction."""

    token_set: float
    token_sort: float
    partial: float
    weighted: float
    acronym: float
    base_score: float
    geo_adjustment: float
    final_score: float
    reasons: tuple[str, ...] = field(default=())
    length_ratio: float = 0.0
    alignment: float = 0.0
    token_coverage: float = 0.0
    unmatched_target_tokens: float = 0.0

    def as_dict(self) -> dict[str, float | list[str]]:
        return {
            "token_set_ratio": round(self.token_set, 2),
            "token_sort_ratio": round(self.token_sort, 2),
            "partial_ratio": round(self.partial, 2),
            "weighted_ratio": round(self.weighted, 2),
            "acronym_ratio": round(self.acronym, 2),
            "length_ratio": round(self.length_ratio, 3),
            "token_alignment": round(self.alignment, 2),
            "token_coverage": round(self.token_coverage, 3),
            "unmatched_target_weight": round(self.unmatched_target_tokens, 2),
            "base_score": round(self.base_score, 2),
            "geo_adjustment": round(self.geo_adjustment, 2),
            "final_score": round(self.final_score, 2),
            "reasons": list(self.reasons),
        }


def token_alignment(
    query_tokens: tuple[str, ...], target_tokens: tuple[str, ...]
) -> tuple[float, int]:
    """Align each query token to its best unused target token.

    Returns ``(coverage, unmatched_target_tokens)`` where coverage is 0..1.

    This is the control for the pattern that dominates manual screening: an
    officer types a short trading name and the designation carries a longer
    legal one. "GAZPROM" sits inside 109 designations, but character-level
    ratios score it ~78 against "Gazprom PMC" because two thirds of the target
    string is unaccounted for. Token-level alignment scores what actually
    matters — is the searched name present as a name — and reports the leftover
    separately so the caller can price it.

    Prefix matching is deliberate and one-directional per pair: "GAZPROM"
    aligns to "GAZPROMBANK" and "GAZPROMNEFT", which is how a compliance
    officer reads them.
    """
    if not query_tokens or not target_tokens:
        return 0.0, 0

    used: set[int] = set()
    total = 0.0
    weight = 0.0

    for q_token in query_tokens:
        best_score, best_index = 0.0, -1
        for index, t_token in enumerate(target_tokens):
            if index in used:
                continue
            if q_token == t_token:
                score = 1.0
            elif len(q_token) >= 5 and t_token.startswith(q_token):
                score = 0.94
            elif len(t_token) >= 5 and q_token.startswith(t_token):
                score = 0.94
            else:
                # Only near-identical tokens count; anything looser is noise.
                char_ratio = fuzz.ratio(q_token, t_token)
                score = char_ratio / 100.0 if char_ratio >= 85 else 0.0
            if score > best_score:
                best_score, best_index = score, index
        if best_index >= 0 and best_score > 0:
            used.add(best_index)
        total += best_score

    # Leftover target tokens are weighted by whether they carry identity.
    # Corporate boilerplate barely counts; a distinctive word costs fully.
    for index, t_token in enumerate(target_tokens):
        if index in used:
            continue
        weight += 0.15 if is_generic_token(t_token) else 1.0

    return total / len(query_tokens), weight


def _shared_name_score(
    partner_tokens: tuple[str, ...],
    target_tokens: tuple[str, ...],
    *,
    individual: bool,
) -> float:
    """How much of BOTH names is the same name, 0-100. Used in batch.

    A weighted Dice coefficient over words: ``2 x shared / (partner + target)``.
    It replaces two measures that both rewarded a name for being *contained*:

    * ``token_set_ratio`` returns 100 whenever one name's set of words sits
      inside the other's, so "ALEXIS RODRIGUEZ" scored 100 against "ROMERO
      RODRIGUEZ, Alexis" and "ALI MOHAMMADI" scored 100 against "Hodjatoleslam
      Ali KHAN MOHAMMADI". Two further unexplained words make a different
      person, not the same one.
    * The alignment score charged 3 points for a whole unexplained surname.

    Counting both sides means an extra word on EITHER name costs. Three of four
    words shared is 86; two of three against a four-word name is 57.

    Matching words:

    * Exact equality always counts.
    * A near-identical spelling counts only for long words (6+ letters, ratio of
      at least 90): MOHAMMED/MOHAMED is a transliteration, JUNG/UNG is a
      different name. A one-letter difference in a short word is not variation.
    * A prefix counts only for an ORGANISATION ("GAZPROM" -> "GAZPROMBANK").
      For a person it is wrong: BELLO is not BELLOSO.

    Words that identify nothing (company-law boilerplate) weigh 0.15, so a
    shared "COMERCIALIZADORA" is worth almost nothing and a shared surname is
    worth a full point.
    """
    if not partner_tokens or not target_tokens:
        return 0.0

    def weight(token: str) -> float:
        return 0.15 if is_generic_token(token) else 1.0

    used: set[int] = set()
    shared = 0.0
    for q_token in partner_tokens:
        best_score, best_index = 0.0, -1
        for index, t_token in enumerate(target_tokens):
            if index in used:
                continue
            if q_token == t_token:
                score = 1.0
            else:
                score = 0.0
                if len(q_token) >= 6 and len(t_token) >= 6:
                    ratio = fuzz.ratio(q_token, t_token)
                    if ratio >= 90:
                        score = ratio / 100.0
                if score == 0.0 and not individual:
                    short, long_ = sorted((q_token, t_token), key=len)
                    # A prefix is a compound only when it is long and is most of
                    # the word: GAZPROM (7 of 11) is GAZPROMBANK. SOUTH (5 of 10)
                    # is not SOUTHFRONT — it is a common word that starts a
                    # different one, and "South Food S.A." scored 90 against it.
                    if (
                        len(short) >= 6
                        and len(short) / len(long_) >= 0.55
                        and long_.startswith(short)
                    ):
                        score = 0.9
            if score > best_score:
                best_score, best_index = score, index
        if best_index >= 0:
            used.add(best_index)
            shared += best_score * weight(q_token)

    partner_weight = sum(weight(t) for t in partner_tokens)
    target_weight = sum(weight(t) for t in target_tokens)
    total = partner_weight + target_weight
    return 100.0 * 2.0 * shared / total if total else 0.0


def _alignment_score(coverage: float, unmatched_weight: float) -> float:
    """Convert an alignment into a 0-100 score.

    Every query token accounted for is the strong signal. Unexplained target
    tokens shave points in proportion to how much identity they carry, so
    "Gazprom PMC" outranks "Gazprom Neft Shelf Yamal" while
    "PUBLIC JOINT STOCK COMPANY GAZPROM NEFT" is not punished for four words
    of company-law boilerplate.
    """
    if coverage <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * coverage - 3.0 * max(0.0, unmatched_weight)))


# Shortest acronym worth bridging. Three letters in both modes: "UAC" against
# "United Aircraft Corporation" is the first requirement this system was built
# to meet. The false alerts that prompted tightening this rule did not come from
# the length — they came from bridging to INDIVIDUALS and from the reverse
# direction, both now removed. Measured on 83,642 Latin American partner names,
# raising the batch minimum to four changed nothing (0 acronym-dependent alerts
# survive either way), so there was no case for giving up the requirement.
# Kept as separate constants so it can be raised without a code change to the
# rule if a future file proves otherwise.
ACRONYM_MIN_LEN_SEARCH = 3
ACRONYM_MIN_LEN_BATCH = 3

# In batch, an acronym needs the country to corroborate it — at ANY length.
# This started at three letters (432 of 461 acronym-only alerts in an 83,642-name
# file were three-letter names) and was then raised, because four letters turned
# out to be no better: with every other fix in place, TEN OF THE TWENTY alerts
# that survived a 95% screen were four-letter coincidences — SAKI SAS against
# SHAHID AHMAD KAZEMI INDUSTRIES GROUP, MFCL RESTAURANT against MISTRAL FLEET CO,
# DILL against DIGITAL INVEST LIMITED LIABILITY. The initials of a four-word name
# spelling a short partner name is chance. The founding requirement ("UAC" ->
# United Aircraft Corporation) is kept where it is evidence: a UAC registered in
# Russia against a Russian designation. Acronym candidates are at most six
# letters, so a value above six means "always require corroboration".
ACRONYM_UNCORROBORATED_MIN_LEN_BATCH = 7


def _acronym_score(
    partner_raw: str,
    target_raw: str,
    *,
    target_is_individual: bool = False,
    recall_mode: bool = False,
) -> tuple[float, bool]:
    """Bridge an acronym against the name it abbreviates.

    This is the control for the "UAC" vs "United Aircraft Corporation" failure
    mode called out in the process review.

    Takes the *raw* names deliberately. Normalisation strips the legal form, and
    the legal form is usually part of the acronym — expanding the normalised
    "United Aircraft" yields "UA" and the match is lost.

    Three restrictions, each added after a specific batch of false alerts:

    * **Never against a person.** An organisation has an acronym; a person does
      not. "AMF SAS" scored 100 against four unrelated individuals because
      their initials happened to spell AMF (Azzam Mansour Fadlallah, Abdelkebir
      Mohamad Fakhiri, ...). Initials of a human name are not an abbreviation of
      it, so the bridge is switched off for individual designations entirely.
    * **Only the partner's own name may be the acronym in batch.** The reverse —
      a designation's alias is the acronym and the partner's long name merely
      shares its initials — paired "AGN Alimentos y Bebidas" with the Abdallah
      Azzam Brigades because both start A-A-B. An officer typing a full name
      reaches the designation through its words anyway, so the reverse adds
      only coincidences. Manual search keeps it for entities.
    * A minimum length (see ``ACRONYM_MIN_LEN_BATCH``).
    """
    if target_is_individual:
        return 0.0, False

    partner_norm = normalize_name(partner_raw)
    target_norm = normalize_name(target_raw)
    if not partner_norm or not target_norm:
        return 0.0, False

    partner_is_acronym = is_acronym_candidate(partner_norm)
    target_is_acronym = is_acronym_candidate(target_norm)

    # Only meaningful when exactly one side is an acronym.
    if partner_is_acronym == target_is_acronym:
        return 0.0, False

    if partner_is_acronym:
        short, long_name = partner_norm.strip(), target_raw
    else:
        if not recall_mode:
            return 0.0, False
        short, long_name = target_norm.strip(), partner_raw

    # Two-letter acronyms are not discriminative: "BM" expands to any two-word
    # name beginning B_ M_, which matched "Bank Melli" to "BM Holding AG" at
    # full score.
    minimum = ACRONYM_MIN_LEN_SEARCH if recall_mode else ACRONYM_MIN_LEN_BATCH
    expansions = acronyms_of(long_name)
    if not expansions or len(short) < minimum:
        return 0.0, False

    if short in expansions:
        return 100.0, True
    # Tolerate one dropped/extra initial (e.g. "UAC" vs "UACO").
    best = max(fuzz.ratio(short, expansion) for expansion in expansions)
    return (float(best), True) if best >= 85 else (0.0, False)


def score_pair(
    partner_name: str,
    target_name: str,
    partner_country: str | None = None,
    target_geography: str | None = None,
    *,
    geo_penalty: int = 15,
    matched_on_alias: bool = False,
    recall_mode: bool = False,
    target_subject_type: str = "",
) -> ScoreBreakdown:
    """Score one partner/target pair and explain the result.

    ``recall_mode`` selects the calibration, and the two are genuinely
    different controls rather than a tuning preference:

    * **True — manual search.** An officer typed one name on purpose and will
      read every row. Token alignment always counts, so a short distinctive
      name reaches the long designations that contain it. This is what a
      commercial screening terminal does, and what "90% should find Gazprom"
      means in practice.
    * **False — batch screening.** The same rule applied across 719,651 partner
      names is unusable: "Pizza" is contained in "Pizza House Trading Co"
      exactly as "Gazprom" is contained in "Gazprom PMC", and nothing in the
      names themselves separates them. Alignment therefore only counts when the
      contained name is discriminative on its own (3+ tokens, or 12+
      characters), which is what keeps batch volume tractable.

    The asymmetry is a deliberate, documented policy choice about where the
    recall/volume trade-off should sit, not an inconsistency.
    """
    partner_norm = normalize_name(partner_name)
    target_norm = normalize_name(target_name)

    if not partner_norm or not target_norm:
        return ScoreBreakdown(0, 0, 0, 0, 0, 0, 0, 0, ())

    reasons: list[str] = []

    token_set = fuzz.token_set_ratio(partner_norm, target_norm)
    token_sort = fuzz.token_sort_ratio(partner_norm, target_norm)
    partial = fuzz.partial_ratio(partner_norm, target_norm)
    weighted = fuzz.WRatio(partner_norm, target_norm)
    acronym, acronym_used = _acronym_score(
        partner_name,
        target_name,
        target_is_individual=(target_subject_type == "Individual"),
        recall_mode=recall_mode,
    )

    # Geography is resolved here, ahead of its use below, because a short
    # acronym in batch is only evidence when the country supports it.
    partner_codes = resolve_country(partner_country)
    target_codes = resolve_country(target_geography)
    if (
        not recall_mode
        and acronym_used
        and len(partner_norm) < ACRONYM_UNCORROBORATED_MIN_LEN_BATCH
        and not (partner_codes and target_codes and partner_codes & target_codes)
    ):
        acronym, acronym_used = 0.0, False

    # Length agreement between the two normalised names.
    length_ratio = min(len(partner_norm), len(target_norm)) / max(
        len(partner_norm), len(target_norm)
    )

    # token_sort_ratio is the primary: order-insensitive but still sensitive to
    # length, so it cannot be satisfied by a fragment.
    #
    # token_set_ratio is GATED on length agreement. It returns ~100 whenever one
    # name's tokens are a subset of the other's, so short partner names
    # ("Pizza", "Al Noor") otherwise match hundreds of long designations at full
    # score. Measured against the live watchlist, ungated scoring put 34% of all
    # scored pairs over an 80% threshold — an unusable alert volume.
    #
    # partial_ratio and WRatio are recorded for the audit trail but NEVER
    # contribute to the score. Both are substring tests — WRatio falls back to
    # partial_ratio * 0.9 for uneven lengths — and together they were the single
    # largest source of false positives, concentrating ~53k spurious alerts into
    # one 85-90 band on a 3k-name sample.
    # A short name fully contained in a longer one is the hard case: it is the
    # false-positive engine ("Pizza" in "Pizza House Trading Co") AND a genuine
    # true positive ("Banco de Venezuela" in "BANCO DE VENEZUELA SA BANCO
    # UNIVERSAL"). Length ratio alone cannot separate them — both sit near 0.5.
    #
    # What separates them is whether the contained name is *discriminative on
    # its own*: three or more tokens, or twelve or more characters. "Pizza"
    # (1 token, 5 chars) is not; "Banco de Venezuela" (3 tokens, 18 chars) is.
    partner_tokens_set = set(name_tokens(partner_name))
    target_tokens_set = set(name_tokens(target_name))
    shorter, longer = (
        (partner_norm, target_tokens_set)
        if len(partner_norm) <= len(target_norm)
        else (target_norm, partner_tokens_set)
    )
    shorter_tokens = shorter.split()
    contained = bool(shorter_tokens) and all(t in longer for t in shorter_tokens)
    # Counted on DISTINCT words. A repeated word inflated both measures:
    # "ISMAEL ISMAEL" is two tokens and thirteen characters, so it passed as
    # "discriminative" and scored 100 against every four-word name containing
    # ISMAEL — which is one given name, not a distinctive designation.
    distinct_shorter = list(dict.fromkeys(shorter_tokens))
    # ...and it must contain at least one word that identifies anything. Length
    # alone was the test, so "INTERNATIONAL" — a single generic word that happens
    # to be 13 letters — counted as distinctive and paired a partner with every
    # designation containing it (Al-Aqsa International Foundation, Persia
    # International Bank, Korea International Chemical...). "FEDERAL SERVICES"
    # did the same. A name made only of corporate boilerplate says nothing.
    carries_identity = any(not is_generic_token(t) for t in distinct_shorter)
    discriminative = carries_identity and (
        len(distinct_shorter) >= 3 or len(" ".join(distinct_shorter)) >= 12
    )

    # Token-level alignment: does the searched name appear *as a name* in the
    # designation, allowing prefix forms (GAZPROM -> GAZPROMBANK)?
    partner_tokens = name_tokens(partner_name)
    target_tokens = name_tokens(target_name)
    coverage, unmatched = token_alignment(partner_tokens, target_tokens)
    alignment = _alignment_score(coverage, unmatched)

    # How much of each name is actually shared, counted on distinct words.
    # token_set_ratio returns 100 whenever one name's SET of words sits inside
    # the other's, and the length check above only compares raw string lengths,
    # so two names of similar length sharing one word walked through it:
    # a two-word partner scored 100 against "ISMAEL ISMAEL", and a one-word core
    # (CAPITAL) scored 100 against three unrelated "... CAPITAL ..." companies,
    # because 7 characters against 10 happens to clear 0.70. Similar length says
    # nothing about whether the names are the same name; the words do.
    partner_distinct = set(partner_norm.split())
    target_distinct = set(target_norm.split())
    word_overlap = len(partner_distinct & target_distinct) / max(
        len(partner_distinct), len(target_distinct), 1
    )

    # What counts as the SAME name in batch is judged on the strict form, where
    # only genuine legal forms are removed. The aggressive form used for finding
    # candidates also drops GROUP, HOLDING, TRADING and similar business words,
    # which is how "CORAL HOLDINGS GROUP" came to equal "CORAL TRADING EST".
    partner_strict = strict_tokens(partner_name)
    target_strict = strict_tokens(target_name)
    strict_equal = bool(partner_strict) and sorted(partner_strict) == sorted(target_strict)

    # A core of four letters or fewer is too thin for "same name" to mean much
    # once the legal forms are set aside: BIO SRL and BIO CORPORATION both reduce
    # to BIO. For such a name only a literally identical name is exact — ISIS and
    # ISIS — while a core of any real length (ANTARES, ALBATROS, CAMORRA) is
    # exact whatever legal form is attached.
    literal_equal = basic_normalize(partner_name) == basic_normalize(target_name)
    short_core = min(len(partner_norm), len(target_norm)) <= 4
    exact_in_batch = strict_equal and (literal_equal or not short_core)

    shared_score = _shared_name_score(
        partner_strict, target_strict, individual=(target_subject_type == "Individual")
    )

    if recall_mode:
        # Manual search: an officer typed one name on purpose and reads every row,
        # so a short name contained in a longer designation SHOULD score high —
        # that is what makes "gazprom" return the 100+ designations containing
        # it. Unchanged from the calibration already validated.
        base = float(token_sort)
        if (length_ratio >= 0.70 and word_overlap >= 0.75) or (contained and discriminative):
            base = max(base, float(token_set))
        base = max(base, alignment)
        if alignment >= base and coverage >= 0.999 and unmatched > 0:
            reasons.append(ReasonCode.NAME_WITHIN_DESIGNATION)
    else:
        # Batch: the score is how much of the two names is the same name, and 100
        # means they are the same name. Nobody reads 28,000 alerts, and a score of
        # 100 on names that visibly differ cannot be defended to an officer.
        # Word-level only. The character-level ratio is deliberately NOT part of
        # the batch score: on a short name it rescues a different surname, since
        # "BELLO MIGUEL" and "BELLOSO MIGUEL" are 92% alike letter for letter and
        # are two different people. Spelling variation is still handled, but where
        # it is real: long words (6+ letters, ratio 90+) in _shared_name_score.
        base = shared_score

        # A name of three or more distinct words that sits wholly inside the
        # other is a genuine partial match: it is how "Banco de Venezuela" reaches
        # OFAC's "BANCO DE VENEZUELA SA BANCO UNIVERSAL", a live designation this
        # system exists to catch. Counted on both sides the extra "BANCO
        # UNIVERSAL" would cost it 22 points, so it is given a stated floor of 85
        # — high enough to be seen, never 100, because the names are not the same.
        # Two words is not enough: ALEXIS RODRIGUEZ inside ROMERO RODRIGUEZ ALEXIS
        # is a different person.
        if contained and carries_identity and len(distinct_shorter) >= 3:
            base = max(base, 85.0)

        # Only identical wording, in any order, may score 100. Every other
        # pair is held below it, so 100 keeps its meaning.
        if not exact_in_batch:
            base = min(base, 99.0)

    # In batch, a very short name that is not an exact match to the spelling it
    # was scored against is held below the alerting range. Ratios on three or
    # four letters are unstable, and across a large book they pair unrelated
    # names constantly. Flagged-not-suppressed was the earlier policy, and it
    # put a long run of such pairs in front of an officer at full score.
    # Exact matches are untouched, so a partner genuinely called "ISIS" still
    # alerts on "ISIS". It is applied BEFORE the acronym bridge is folded in:
    # initials of an organisation are evidence of a different kind, and holding
    # "UAC" to 89 would have broken the system's founding requirement.
    if not recall_mode and not literal_equal and short_core:
        base = min(base, 89.0)

    # An acronym is strong evidence, but it is not an identical name: in batch it
    # is capped at 95 so it sits at the alerting line and never at 100.
    base = max(base, acronym if recall_mode else min(acronym, 95.0))

    # Manual search keeps the aggressive comparison (an officer reads every row);
    # batch requires identical wording once only genuine legal forms are removed.
    if exact_in_batch if not recall_mode else partner_norm == target_norm:
        base = 100.0
        reasons.append(ReasonCode.EXACT_NORMALISED)
        if not recall_mode and not literal_equal:
            core = [w for w in set(partner_strict) if not is_generic_token(w)]
            if len(core) == 1 and TOKEN_FREQUENCY.get(core[0], 0) >= COMMON_WORD_DESIGNATIONS:
                base = COMMON_WORD_SCORE
                reasons.append(ReasonCode.COMMON_WORD)
    if acronym_used and acronym >= 85:
        reasons.append(ReasonCode.ACRONYM_BRIDGE)
    if matched_on_alias:
        reasons.append(ReasonCode.ALIAS_HIT)

    if len(target_tokens_set) >= 2 and target_tokens_set.issubset(partner_tokens_set):
        reasons.append(ReasonCode.TOKEN_SUBSET)
    if contained and discriminative and length_ratio < 0.70:
        reasons.append(ReasonCode.CONTAINED_NAME)

    # Very short cores produce unstable ratios; flag rather than suppress.
    if min(len(partner_norm), len(target_norm)) <= 4:
        reasons.append(ReasonCode.SHORT_NAME)

    # --- Geographic control -------------------------------------------
    geo_adjustment = 0.0

    if not partner_codes or not target_codes:
        reasons.append(ReasonCode.GEO_UNKNOWN)
    elif partner_codes & target_codes:
        reasons.append(ReasonCode.GEO_CONFIRMED)
    elif countries_contradict(partner_country, target_geography):
        geo_adjustment = -float(geo_penalty)
        reasons.append(ReasonCode.GEO_CONTRADICTION)

    final = max(0.0, min(100.0, base + geo_adjustment))

    return ScoreBreakdown(
        token_set=float(token_set),
        token_sort=float(token_sort),
        partial=float(partial),
        weighted=float(weighted),
        acronym=float(acronym),
        base_score=float(base),
        geo_adjustment=geo_adjustment,
        final_score=final,
        reasons=tuple(reasons),
        length_ratio=length_ratio,
        alignment=alignment,
        token_coverage=coverage,
        unmatched_target_tokens=unmatched,
    )


def band_for(breakdown: ScoreBreakdown, threshold: int, strong_threshold: int) -> RiskBand:
    """Band on the *pre-geography* score.

    The geographic signal reorders the analyst's queue but must not be able to
    push a genuine name match out of the alerting population on its own — that
    would convert a soft signal into a silent suppression.
    """
    if breakdown.base_score >= strong_threshold:
        return RiskBand.STRONG
    if breakdown.base_score >= threshold:
        return RiskBand.POTENTIAL
    return RiskBand.WEAK


# Aliases already drive candidate recall — the blocking index registers every
# published spelling — but scoring used only the primary name, so the spelling
# that caused the recall was then thrown away.
#
# The cost is concrete. OFAC lists "PUBLIC JOINT STOCK COMPANY UNITED AIRCRAFT
# CORPORATION" with the aliases "UNITED AIRCRAFT CORPORATION" and "PJSC UAC".
# A search for "UAC" reaches the designation through the alias index, then
# scores against the primary, whose acronym is PJSCUAC, and returns nothing.
# "UAC" versus "United Aircraft Corporation" is the first requirement this
# system was built to meet.
ALIAS_SCORING_LIMIT = 25

# Batch screening does NOT score aliases. A published alias ("a.k.a.") is often a
# single common word, a nickname or a short form, and a partner file holds tens
# of thousands of names that collapse to exactly such a word once the legal form
# and generic words are stripped: "JULIA HOLDING E.I.R.L." becomes JULIA, which
# equals a person's alias and scored 100 against an unrelated individual; "LIGA
# GROUP", "MARTIN LTDA", "MLM S.R.L." and "LA RIVIERA LTDA" did the same. Across
# one 83,642-name file this was the largest remaining source of noise, and a
# score of 100 on names that visibly differ cannot be defended to an officer.
#
# Manual search keeps aliases: an officer types one name on purpose and reads
# every row, so a nickname hit is information there and not noise.
#
# The cost is real and belongs on the record: a partner whose legal name equals
# a designation's genuine alias, and nothing else about it, is no longer raised.
# The designation's primary name still is, and so is an exact primary match.
BATCH_ALIAS_LIMIT = 0

# A single word that names MANY different designations is a weak identification:
# "INTEGRAL" appears in 13 designations, "ANTARES" in 4, "ALBATROS" in 2,
# "CAMORRA" in 1. An exact match on a one-word core therefore means less the more
# designations share that word, so it is held just below 100 once ten or more do
# (and the names are not literally identical). Counted from the watchlist itself,
# not from a dictionary, so it describes THESE designations; registered whenever
# the index is built. Empty by default, which leaves the rule inactive.
# Articles and particles carry no identity of their own: "LA RIVIERA" is one
# distinctive word, not two. Used when deciding whether an alias is a FULL name.
FUNCTION_WORDS = frozenset(
    {
        "A", "AL", "AND", "DA", "DAS", "DE", "DEL", "DELLA", "DI", "DO", "DOS", "E",
        "EL", "LA", "LAS", "LE", "LES", "LO", "LOS", "O", "OF", "THE", "Y",
    }
)

TOKEN_FREQUENCY: dict[str, int] = {}
COMMON_WORD_DESIGNATIONS = 10
COMMON_WORD_SCORE = 97.0


def register_token_frequencies(names) -> None:
    """Count, per word, how many designation names contain it."""
    counter: dict[str, int] = {}
    for name in names:
        for token in set(strict_tokens(name)):
            counter[token] = counter.get(token, 0) + 1
    TOKEN_FREQUENCY.clear()
    TOKEN_FREQUENCY.update(counter)


def score_against_all_names(
    query: str,
    primary_name: str,
    aliases: str | list[str] | None = None,
    partner_country: str | None = None,
    target_geography: str | None = None,
    *,
    geo_penalty: int = 15,
    recall_mode: bool = False,
    alias_limit: int = ALIAS_SCORING_LIMIT,
    target_subject_type: str = "",
) -> tuple[ScoreBreakdown, str]:
    """Score a query against every published spelling; return the best.

    Returns ``(breakdown, matched_name)`` so the record can state which
    spelling matched — "matched on alias 'PJSC UAC'" is defensible in a way
    that a bare score against a different string is not.

    ``alias_limit`` bounds the worst case: 99% of designations carry 17 or
    fewer aliases, but one carries 144, and a manual search must stay
    interactive. When the cap truncates, the primary name and the first
    ``alias_limit`` spellings are still scored.
    """
    best = score_pair(
        query,
        primary_name,
        partner_country,
        target_geography,
        geo_penalty=geo_penalty,
        recall_mode=recall_mode,
        target_subject_type=target_subject_type,
    )
    matched_name = primary_name

    if isinstance(aliases, str):
        alias_list = [a.strip() for a in aliases.split(" / ") if a.strip()]
    else:
        alias_list = [a.strip() for a in (aliases or []) if a and a.strip()]

    for alias in alias_list[:alias_limit]:
        if alias.casefold() == primary_name.casefold():
            continue
        candidate = score_pair(
            query,
            alias,
            partner_country,
            target_geography,
            geo_penalty=geo_penalty,
            recall_mode=recall_mode,
            matched_on_alias=True,
            target_subject_type=target_subject_type,
        )
        if candidate.base_score > best.base_score:
            best, matched_name = candidate, alias

    # Batch drops aliases because a nickname or one-word alias is the largest source
    # of noise (JULIA, LIGA, MARTIN, MLM). But a partner whose name is IDENTICAL to a
    # full-name alias of a sanctioned party — two or more distinctive words, the
    # same wording — is not noise, and it is exactly the common-name case that can
    # genuinely be a sanctioned person. So that one narrow case is kept.
    if not recall_mode and alias_limit == 0:
        wanted = tuple(sorted(strict_tokens(query)))
        distinctive = [
            w for w in set(wanted) if not is_generic_token(w) and w not in FUNCTION_WORDS
        ]
        if wanted and len(distinctive) >= 2:
            for alias in alias_list:
                if alias.casefold() == primary_name.casefold():
                    continue
                if tuple(sorted(strict_tokens(alias))) != wanted:
                    continue
                candidate = score_pair(
                    query, alias, partner_country, target_geography,
                    geo_penalty=geo_penalty, recall_mode=recall_mode,
                    matched_on_alias=True, target_subject_type=target_subject_type,
                )
                if candidate.base_score > best.base_score:
                    best, matched_name = candidate, alias

    return best, matched_name
