"""Candidate generation (blocking).

Screening 719k distinct partner names against ~40k watchlist targets is 29
billion pairs — infeasible to score directly, which is precisely why the
spreadsheet approach cannot scale. This module reduces each partner name to a
few hundred plausible candidates before any fuzzy scoring happens.

Three complementary indexes are maintained, so a name is recalled if it shares
*any* of:
  1. a discriminative token       -> catches word-order and extra-word variance
  2. a rare character trigram     -> catches typos, spacing and short names
  3. an acronym                   -> catches "UAC" vs "United Aircraft Corp"

Recall safety: token blocking alone would miss single-token names and heavy
misspellings, so the trigram index is always consulted as well. Only tokens
that are *common across the watchlist* are excluded from the token index, and
those names remain reachable through their trigrams.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .normalize import acronyms_of, is_acronym_candidate, name_tokens, squashed, trigrams


@dataclass
class BlockingIndex:
    """Inverted indexes over the watchlist, built once per screening run."""

    max_token_document_frequency: float = 0.02
    max_candidates: int = 400
    # A trigram on more than this share of indexed names carries no signal and
    # its posting list is large enough to dominate query cost. Dropped outright.
    max_gram_document_frequency: float = 0.004
    # Probe only this many of the rarest grams per query. Bounds worst-case work
    # per name to (probes x max posting length) instead of (all grams x all).
    gram_probes: int = 8

    _token_index: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    _trigram_index: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    _acronym_index: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    _size: int = 0
    _stop_tokens: frozenset[str] = frozenset()
    _built: bool = False

    def add(self, target_id: int, name: str) -> None:
        """Index one watchlist target under a single one of its names.

        Call once per primary name and once per alias so aliases are matchable.
        """
        tokens = name_tokens(name)
        if not tokens:
            return
        for token in set(tokens):
            self._token_index[token].add(target_id)
        for gram in trigrams(squashed(name)):
            self._trigram_index[gram].add(target_id)
        for acronym in acronyms_of(name):
            self._acronym_index[acronym].add(target_id)
        # A target that *is* an acronym should also be reachable by that token.
        if is_acronym_candidate(name):
            self._acronym_index[tokens[0]].add(target_id)
        self._size += 1

    def finalise(self, target_count: int) -> None:
        """Compute the stop-token set once all targets are indexed.

        Tokens such as "AL", "MOHAMMED" or "TRADING" appear on thousands of
        designations. Using them for recall would pull back most of the list on
        every query and destroy the performance gain, so they are demoted — the
        trigram index still provides recall for those names.
        """
        if target_count <= 0:
            self._built = True
            return
        cap = max(1, int(target_count * self.max_token_document_frequency))
        self._stop_tokens = frozenset(
            token for token, ids in self._token_index.items() if len(ids) > cap
        )

        # Physically drop over-common trigrams. Without this, a query probing
        # grams such as "MOH" or "ALS" walks posting lists tens of thousands of
        # entries long, and the index costs more than the scoring it avoids.
        gram_cap = max(64, int(self._size * self.max_gram_document_frequency))
        for gram in [g for g, ids in self._trigram_index.items() if len(ids) > gram_cap]:
            del self._trigram_index[gram]

        self._built = True

    @property
    def stop_tokens(self) -> frozenset[str]:
        return self._stop_tokens

    def candidates(self, name: str) -> set[int]:
        """Return watchlist target ids worth scoring against ``name``.

        Candidates are accumulated with an evidence weight so that, when more
        are found than ``max_candidates``, the *weakest* are dropped rather than
        an arbitrary slice. Truncating by id instead of by evidence silently
        discarded true matches — "Rosneft Oil Co" returned nothing because the
        common token "OIL" flooded the candidate set and the real Rosneft
        designations fell outside the cut.
        """
        if not self._built:
            raise RuntimeError("BlockingIndex.finalise() must be called before querying")

        tokens = name_tokens(name)
        if not tokens:
            return set()

        # Evidence weights: a shared discriminative token is worth more than a
        # shared trigram; an acronym hit is the strongest single signal.
        evidence: dict[int, int] = defaultdict(int)

        # 1. Discriminative tokens. Rarer tokens score higher.
        for token in set(tokens):
            if token in self._stop_tokens:
                continue
            postings = self._token_index.get(token)
            if not postings:
                continue
            weight = 6 if len(postings) <= 32 else 3
            for target_id in postings:
                evidence[target_id] += weight

        # 2. Acronym bridge, in both directions.
        for acronym in acronyms_of(name):
            for target_id in self._acronym_index.get(acronym, ()):
                evidence[target_id] += 8
        if is_acronym_candidate(name):
            for target_id in self._acronym_index.get(tokens[0], ()):
                evidence[target_id] += 8
            for target_id in self._token_index.get(tokens[0], ()):
                evidence[target_id] += 6

        found = set(evidence)

        # 3. Trigram recall — the safety net that keeps stop-token-only and
        #    misspelled names screenable.
        #
        #    Only the rarest few grams are probed. Rare grams are both cheaper
        #    (short posting lists) and far more discriminative than common ones,
        #    so this is a large speed win at negligible cost to recall.
        if len(found) < self.max_candidates:
            postings = [
                (len(ids), gram)
                for gram in trigrams(squashed(name))
                if (ids := self._trigram_index.get(gram)) is not None
            ]
            if postings:
                postings.sort()
                probes = [gram for _, gram in postings[: self.gram_probes]]

                counts: dict[int, int] = defaultdict(int)
                for gram in probes:
                    for target_id in self._trigram_index[gram]:
                        counts[target_id] += 1

                # Two independent rare grams in common is a strong signal; fall
                # back to one when the name is too short to offer two.
                required = 2 if len(probes) >= 2 else 1
                for target_id, hits in counts.items():
                    if hits >= required:
                        evidence[target_id] += hits
                        found.add(target_id)

        if len(found) > self.max_candidates:
            # Keep the strongest evidence. The id tiebreak keeps the result
            # deterministic so an audited run is reproducible.
            ranked = sorted(found, key=lambda tid: (-evidence[tid], tid))
            return set(ranked[: self.max_candidates])
        return found
