"""Editable interface wording.

The officer using this is not a developer and will not have an engineer on
hand. Wording they cannot change is wording that stays wrong.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app.routers import labels as module
from app.routers.labels import Label, load_labels


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_store", lambda: tmp_path / "ui-labels.json")
    return tmp_path / "ui-labels.json"


class _Session:
    """Enough of a session for record_event, which is patched out anyway."""


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr(module, "record_event", lambda *a, **k: None)


def _put(entries):
    return module.put_labels(
        [Label(**e) for e in entries], db=_Session(), user=type("U", (), {"email": "x@y"})()
    )


def test_an_override_round_trips(store) -> None:
    _put([{"path": "#a>h2", "original": "Search a single name", "text": "Name screening"}])
    saved = load_labels()
    assert saved == [
        {"path": "#a>h2", "original": "Search a single name", "text": "Name screening"}
    ]


def test_markup_is_refused(store) -> None:
    """The file is read back into every page.

    This is the one place a stored string could become script if it were ever
    interpolated as HTML rather than set as text.
    """
    with pytest.raises(HTTPException) as caught:
        _put([{"path": "#a", "original": "x", "text": "<script>alert(1)</script>"}])
    assert caught.value.status_code == 422

    with pytest.raises(HTTPException):
        _put([{"path": "#a", "original": "x", "text": "<b>bold</b>"}])


def test_text_identical_to_the_original_is_not_stored(store) -> None:
    """An override that changes nothing is clutter that can later go stale."""
    assert _put([{"path": "#a", "original": "Same", "text": "Same"}])["saved"] == 0
    assert load_labels() == []


def test_the_set_is_bounded(store) -> None:
    too_many = [
        {"path": f"#a{i}", "original": f"o{i}", "text": f"t{i}"}
        for i in range(module.MAX_OVERRIDES + 1)
    ]
    with pytest.raises(HTTPException) as caught:
        _put(too_many)
    assert caught.value.status_code == 422


def test_saving_an_empty_set_restores_the_original_wording(store) -> None:
    _put([{"path": "#a", "original": "Old", "text": "New"}])
    assert load_labels()
    _put([])
    assert load_labels() == []


def test_a_corrupt_file_falls_back_to_the_built_in_wording(store) -> None:
    """A broken overrides file must not take the interface down with it."""
    store.write_text("{ not json", encoding="utf-8")
    assert load_labels() == []

    store.write_text('{"not": "a list"}', encoding="utf-8")
    assert load_labels() == []


def test_entries_missing_their_anchor_are_dropped(store) -> None:
    """An override without its original text cannot be verified on apply.

    Applying it blind would let it land on an element that has since changed —
    a heading's replacement appearing under a different control.
    """
    store.write_text(
        json.dumps(
            [
                {"path": "#a", "original": "Old", "text": "New"},
                {"path": "#b", "text": "orphan"},
                "nonsense",
            ]
        ),
        encoding="utf-8",
    )
    assert load_labels() == [{"path": "#a", "original": "Old", "text": "New"}]


def test_long_text_is_rejected_by_the_model() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Label(path="#a", original="x", text="y" * (module.MAX_TEXT + 1))


def test_the_wording_route_is_registered() -> None:
    from app.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/labels" in paths
