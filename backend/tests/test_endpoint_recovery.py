"""Recovering when a regulator's download URL breaks.

A dead feed is the most likely maintenance event this system will face — two
of the four URLs were already dead once during the build, and the EU's carries
a rotatable token. It fails quietly: the list stops refreshing while every
other part of the application carries on working normally.
"""

from __future__ import annotations

import pytest

from app.ingest.endpoints import (
    Endpoint,
    LearnedEndpoints,
    candidate_order,
    looks_like_a_list,
    resolve,
)

GOOD = b"x" * 50_000
SMALL = b"x" * 500
HTML = b"<!DOCTYPE html>\n<html><body>Not found</body></html>" + b" " * 50_000

EP = Endpoint(
    key="Test",
    candidates=("https://primary.example/list.csv", "https://mirror.example/list.csv"),
    allowed_hosts=frozenset({"example"}),
)

LINK_PATTERN = r"href='([^']*\.csv)'"


def _fetcher(responses: dict[str, bytes]):
    def fetch(url: str) -> bytes:
        value = responses.get(url)
        if value is None:
            raise ConnectionError(f"404 {url}")
        return value

    return fetch


def _parses_to(count: int):
    return lambda payload: list(range(count))


def test_the_published_url_is_used_when_it_works() -> None:
    result = resolve(
        EP,
        configured_url="https://primary.example/list.csv",
        learned_url="",
        fetch=_fetcher({"https://primary.example/list.csv": GOOD}),
        parse=_parses_to(1000),
        last_known_count=1000,
    )
    assert result.url == "https://primary.example/list.csv"
    assert result.recovered is False


def test_a_dead_primary_falls_through_to_the_mirror() -> None:
    result = resolve(
        EP,
        configured_url="https://primary.example/list.csv",
        learned_url="",
        fetch=_fetcher({"https://mirror.example/list.csv": GOOD}),
        parse=_parses_to(1000),
        last_known_count=1000,
    )
    assert result.url == "https://mirror.example/list.csv"
    assert result.recovered is True
    assert result.attempts[0].ok is False


def test_a_reachable_url_serving_the_wrong_file_is_refused() -> None:
    """The defect this whole module exists to prevent.

    ``ofac.treasury.gov/media/5926/download`` answers 200 with a non-HTML body
    and is not the SDN list: 199 KB against the real file's 5.7 MB. Accepting
    it would have replaced the watchlist with something 96% smaller, and every
    screening afterwards would have come back clean for the wrong reason.
    """
    with pytest.raises(RuntimeError, match="no working source"):
        resolve(
            EP,
            configured_url="https://primary.example/list.csv",
            learned_url="",
            fetch=_fetcher({"https://primary.example/list.csv": GOOD}),
            parse=_parses_to(50),
            last_known_count=20_000,
        )


def test_a_modest_shrink_is_still_accepted() -> None:
    """Designations are removed all the time; only a collapse is suspicious."""
    result = resolve(
        EP,
        configured_url="https://primary.example/list.csv",
        learned_url="",
        fetch=_fetcher({"https://primary.example/list.csv": GOOD}),
        parse=_parses_to(9_500),
        last_known_count=10_000,
    )
    assert result.records == 9_500


def test_an_error_page_is_not_a_sanctions_list() -> None:
    assert looks_like_a_list(HTML)
    assert looks_like_a_list(SMALL)
    assert looks_like_a_list(GOOD) == ""


def test_a_file_that_does_not_parse_is_refused() -> None:
    def explode(payload: bytes) -> list:
        raise ValueError("unexpected columns")

    with pytest.raises(RuntimeError):
        resolve(
            EP,
            configured_url="https://primary.example/list.csv",
            learned_url="",
            fetch=_fetcher({"https://primary.example/list.csv": GOOD}),
            parse=explode,
        )


def test_discovery_will_not_follow_a_link_off_the_regulators_domain() -> None:
    """A harvested link is untrusted input.

    Following one off-domain would let a compromised or simply mistyped page
    redirect the entire watchlist ingest somewhere else.
    """
    endpoint = Endpoint(
        key="Test",
        candidates=("https://primary.example/list.csv",),
        allowed_hosts=frozenset({"regulator.example"}),
        discovery_pages=("https://regulator.example/downloads",),
        link_pattern=LINK_PATTERN,
    )
    page = b"<a href='https://attacker.test/list.csv'>list</a>"
    with pytest.raises(RuntimeError):
        resolve(
            endpoint,
            configured_url="https://primary.example/list.csv",
            learned_url="",
            fetch=_fetcher(
                {
                    "https://regulator.example/downloads": page,
                    "https://attacker.test/list.csv": GOOD,
                }
            ),
            parse=_parses_to(1000),
        )


def test_discovery_accepts_a_new_link_on_the_regulators_own_domain() -> None:
    endpoint = Endpoint(
        key="Test",
        candidates=("https://regulator.example/old.csv",),
        allowed_hosts=frozenset({"regulator.example"}),
        discovery_pages=("https://regulator.example/downloads",),
        link_pattern=LINK_PATTERN,
    )
    page = b"<a href='/new-location/list.csv'>current list</a>"
    result = resolve(
        endpoint,
        configured_url="https://regulator.example/old.csv",
        learned_url="",
        fetch=_fetcher(
            {
                "https://regulator.example/downloads": page,
                "https://regulator.example/new-location/list.csv": GOOD,
            }
        ),
        parse=_parses_to(1000),
        last_known_count=1000,
    )
    assert result.url == "https://regulator.example/new-location/list.csv"
    assert "discovery" in result.attempts[-1].detail


def test_the_configured_url_always_wins() -> None:
    """Automation that cannot be overridden is a trap during an incident."""
    order = candidate_order(
        EP,
        learned="https://mirror.example/list.csv",
        configured="https://operator.example/override.csv",
    )
    assert order[0] == "https://operator.example/override.csv"


def test_a_working_url_is_remembered_and_tried_first(tmp_path) -> None:
    store = LearnedEndpoints(tmp_path / "learned.json")
    store.remember("OFAC SDN", "https://mirror.example/list.csv")

    reopened = LearnedEndpoints(tmp_path / "learned.json")
    assert reopened.get("OFAC SDN") == "https://mirror.example/list.csv"

    order = candidate_order(EP, learned="https://mirror.example/list.csv", configured="")
    assert order[0] == "https://mirror.example/list.csv"


def test_a_corrupt_memory_file_is_ignored_not_fatal(tmp_path) -> None:
    path = tmp_path / "learned.json"
    path.write_text("{not json", encoding="utf-8")
    assert LearnedEndpoints(path).get("OFAC SDN") == ""


def test_every_configured_source_has_recovery(tmp_path) -> None:
    """A source with no entry silently loses this protection entirely."""
    from app.ingest.endpoints import ENDPOINTS
    from app.ingest.runner import SOURCES

    missing = [spec.key for spec in SOURCES if spec.key not in ENDPOINTS]
    assert not missing, f"no recovery configured for: {missing}"


def test_no_candidate_points_off_its_own_regulators_domain() -> None:
    from urllib.parse import urlparse

    from app.ingest.endpoints import ENDPOINTS

    for endpoint in ENDPOINTS.values():
        for url in endpoint.candidates:
            host = (urlparse(url).hostname or "").lower()
            assert any(
                host == allowed or host.endswith("." + allowed)
                for allowed in endpoint.allowed_hosts
            ), f"{endpoint.key}: candidate {url} is outside its allowed hosts"
