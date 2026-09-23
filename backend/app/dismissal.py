"""Resolution vocabulary: status, risk level and reason.

Replaces a thirteen-code dismissal taxonomy. That taxonomy was built to make
the adjudication population analysable — "how many alerts did APAC clear on a
date-of-birth mismatch?" — but it asked an officer to choose among thirteen
overlapping codes for every alert, and the officers using it said it did not
make sense to them. A control nobody can apply consistently produces worse data
than a simple one applied correctly.

What replaced it mirrors a screening terminal: the reason restates the match
quality, and follows from the status rather than being chosen separately. The
rationale field is where the actual reasoning goes, in the officer's words.
"""

from __future__ import annotations

from dataclasses import dataclass

# Risk levels. A match that is false carries no risk to grade, so it is offered
# "Unknown" or "N/A" rather than a Low that would sit in a report beside
# genuine Low-risk positives and be counted with them.
GRADED_RISKS = ("High", "Medium", "Low")
UNGRADED_RISKS = ("Unknown", "N/A")
# An unresolved alert has not been assessed, so it has no grade yet. Offering
# High/Medium/Low here invited a risk to be recorded before any judgement was
# actually made, which is the opposite of what "Unspecified" means.
UNASSESSED_RISKS = ("Unspecified", "N/A")


@dataclass(frozen=True)
class Status:
    value: str          # stored on the alert; unchanged from earlier records
    label: str          # shown on screen
    reason_code: str
    reason_label: str
    risks: tuple[str, ...]
    default_risk: str


STATUSES: tuple[Status, ...] = (
    Status("Positive Match", "Positive", "FULL_MATCH", "Full match", GRADED_RISKS, "High"),
    Status("Potential Match", "Possible", "PARTIAL_MATCH", "Partial match", GRADED_RISKS, "Medium"),
    Status("False Match", "False", "NO_MATCH", "No match", UNGRADED_RISKS, "N/A"),
    Status("", "Unspecified", "UNKNOWN", "Unknown", UNASSESSED_RISKS, "Unspecified"),
)

BY_STATUS: dict[str, Status] = {s.value: s for s in STATUSES}
BY_CODE: dict[str, Status] = {s.reason_code: s for s in STATUSES}

VALID_RISKS = frozenset(GRADED_RISKS) | frozenset(UNGRADED_RISKS) | frozenset(UNASSESSED_RISKS)


def reason_for(decision: str) -> str:
    """The reason code implied by a status. Filled in automatically."""
    status = BY_STATUS.get(decision or "")
    return status.reason_code if status else ""


def default_risk_for(decision: str) -> str:
    status = BY_STATUS.get(decision or "")
    return status.default_risk if status else "Unknown"


def risks_for(decision: str) -> tuple[str, ...]:
    status = BY_STATUS.get(decision or "")
    return status.risks if status else UNGRADED_RISKS


def validate(decision: str, reason_code: str, rationale: str, risk_level: str = "") -> str | None:
    """Reject a resolution that could not be defended by the person who made it.

    Deliberately permissive about the reason, which now follows from the
    status, and strict about the two things a reviewer actually needs: a risk
    level that belongs to the status, and words for anything not self-evident.
    """
    status = BY_STATUS.get(decision or "")
    if status is None:
        return f"'{decision}' is not a resolution status."

    if risk_level:
        if risk_level not in VALID_RISKS:
            return f"'{risk_level}' is not a risk level."
        if risk_level not in status.risks:
            allowed = " or ".join(status.risks)
            return f"A '{status.label}' resolution takes {allowed}."

    if reason_code and reason_code != status.reason_code:
        return f"A '{status.label}' resolution is recorded as '{status.reason_label}'."

    # Confirming an alert commits the business to treating a counterparty as
    # designated; clearing one removes it from the record. Both need words.
    if decision in ("Positive Match", "False Match") and not rationale.strip():
        return "State what you checked and concluded in the rationale."

    return None
