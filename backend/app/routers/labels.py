"""Editable interface wording.

Lets an officer correct the words on the screen without a developer. Compliance
language changes — a heading that reads well to the person who wrote it may not
read well to the team, or to an auditor — and waiting on an engineer for a
sentence is how a tool ends up saying something nobody agrees with.

How an override is anchored
---------------------------
Each override stores three things: where the text sits (a CSS path), what it
said originally, and what it should say now. It is applied only if the element
is still there *and* still says what it said when the override was written.

That second check is the point. If the interface is later restructured, a stale
override silently lands on the wrong element — a heading's replacement text
appearing under a different control. Requiring the original to still match means
a stale override simply does not apply, and edit mode lists it as stale so it
can be redone deliberately.

Plain text only
---------------
Overrides are rendered as text, never as markup, and anything containing "<" is
rejected here as well. This endpoint writes to a file that is read back into
every page: it is the one place where a stored string could become script if it
were ever interpolated as HTML.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..audit import record_event
from ..auth import current_user, require_role
from ..config import get_settings
from ..database import get_db
from ..models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labels", tags=["labels"])

MAX_OVERRIDES = 500
MAX_TEXT = 2000


class Label(BaseModel):
    """One piece of wording the officer has changed."""

    path: str = Field(min_length=1, max_length=500)
    original: str = Field(max_length=MAX_TEXT)
    text: str = Field(max_length=MAX_TEXT)


def _store() -> Path:
    return get_settings().data_dir / "ui-labels.json"


def load_labels() -> list[dict]:
    path = _store()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("ui-labels.json is unreadable; serving the built-in wording")
        return []
    if not isinstance(data, list):
        return []
    return [
        entry
        for entry in data
        if isinstance(entry, dict) and {"path", "original", "text"} <= set(entry)
    ]


@router.get("")
def get_labels(_: User = Depends(current_user)) -> list[dict]:
    """Every override, applied by the page as it loads."""
    return load_labels()


@router.put("")
def put_labels(
    labels: list[Label],
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
) -> dict[str, int]:
    """Replace the whole set. Recorded in the audit trail.

    Wording is part of what an auditor reads. If a screen said something
    different last month, the record should show who changed it and when.
    """
    if len(labels) > MAX_OVERRIDES:
        raise HTTPException(422, f"Too many overrides (limit {MAX_OVERRIDES}).")

    cleaned: list[dict] = []
    for label in labels:
        if "<" in label.text:
            raise HTTPException(
                422, "Wording is plain text — the '<' character is not allowed."
            )
        # An override identical to the original is not an override.
        if label.text.strip() == label.original.strip():
            continue
        cleaned.append(
            {"path": label.path, "original": label.original, "text": label.text}
        )

    previous = load_labels()
    path = _store()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(cleaned, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        raise HTTPException(500, f"Could not save the wording: {exc}") from exc

    record_event(
        db,
        "UI_WORDING_CHANGED",
        actor=user.email,
        entity_ref="ui-labels",
        payload={
            "overrides_before": len(previous),
            "overrides_after": len(cleaned),
            # The strings themselves, so the chain shows what the screen said.
            "changed": [
                {"was": entry["original"][:200], "now": entry["text"][:200]}
                for entry in cleaned
                if entry not in previous
            ][:40],
        },
    )
    return {"saved": len(cleaned)}
