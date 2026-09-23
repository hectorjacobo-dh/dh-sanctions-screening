"""Adjudication report generation.

Column order deliberately reproduces the existing workbook so the output is a
drop-in replacement for the current manual process, with the audit fields the
review asked for appended rather than substituted.

Every row carries the screening timestamp, the regulator tag, the SHA-256 of
the exact published list version that produced it, and the run reference — so a
single exported row is self-contained evidence of when and against what it was
screened.
"""

from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from collections import defaultdict
from dataclasses import dataclass

from .audit import verify_chain
from .dismissal import BY_CODE, BY_STATUS
from .models import EvidenceDocument, MatchResult, ScreeningRun, as_utc, to_local


def _utc(value) -> str:  # noqa: ANN001
    stamped = as_utc(value)
    return stamped.isoformat(timespec="seconds") if stamped else ""


def _local(value) -> str:  # noqa: ANN001
    """Berlin-time rendering, with the offset kept so the value is unambiguous."""
    stamped = to_local(value)
    return stamped.isoformat(timespec="seconds") if stamped else ""

REPORT_COLUMNS = [
    # --- Restaurant / partner information (existing workbook order) ---
    "GRID",
    "Counterparty_Group",
    "Brand",
    "Region",
    "Account Country",
    "Account Status",
    "Created Date",
    "Partner Legal Name",
    "UBO Name",
    "Tax ID",
    "Screened Field",
    # --- Sanctions list information ---
    "Target_Name",
    "Matched_Spelling",
    "Subject_Type",
    "Entity_Type",
    "Sanctions_Program",
    "Remarks",
    "Source_List",
    "Geography",
    "DOB",
    # The identity the officer weighed, and the grounds the regulator gave.
    # Absent from the report, a reviewer cannot tell whether a dismissal was
    # reasonable: "different person" is only defensible against the date of
    # birth, nationality and document number that were actually on the record.
    "Nationality",
    "Place_Of_Birth",
    "Gender",
    "Position",
    "Identity_Documents",
    "Listing_Reason",
    "Linked_To",
    "Listed_On",
    "Watchlist_ID",
    # --- Engine output ---
    "Match_Score",
    "Base_Score",
    "Geo_Adjustment",
    "Risk_Band",
    "Reason_Codes",
    # --- Adjudication (analyst-owned) ---
    "Decision",
    "Risk_Level",
    "Reason_Code",
    "Reason_Label",
    "Evidence_Documents",
    "Evidence_SHA256",
    "Comment / Notes",
    "Decided_By",
    "Decided_At_Berlin",
    "Decided_At_UTC",
    # --- Immutable audit stamp ---
    # Berlin time is for the reader; UTC is the authoritative value and the one
    # the audit chain commits to.
    "Screened_At_Berlin",
    "Screened_At_UTC",
    "Run_Reference",
    "Input_File_SHA256",
    "Source_List_SHA256",
    "Engine_Threshold",
    "Screened_Subject_Types",
]


# The record must speak the same language as the screen it was produced from.
# Stored values are unchanged so historic records stay readable; only the
# presentation follows the resolution vocabulary, and the risk level is derived
# from the status rather than stored separately, so the two can never disagree.
def status_of(decision: str, risk_level: str = "") -> tuple[str, str]:
    """Status label and risk level as they should be printed.

    The risk level is what the officer graded, not a value derived from the
    status. Deriving it would silently overwrite a deliberate judgement — an
    alert graded Medium would print as High simply because it was Positive.
    The default is used only where nothing was recorded.
    """
    status = BY_STATUS.get(decision or "")
    if status is None:
        return (str(decision).upper(), risk_level or "Unknown")
    return (status.label.upper(), risk_level or status.default_risk)


def _snapshot_digest(run: ScreeningRun, source: str) -> str:
    snapshot = run.watchlist_snapshot or {}
    entry = snapshot.get(source) or {}
    return entry.get("payload_sha256", "")


def _evidence_by_match(db: Session, run: ScreeningRun) -> dict[int, list[EvidenceDocument]]:
    grouped: dict[int, list[EvidenceDocument]] = defaultdict(list)
    rows = db.execute(
        select(EvidenceDocument)
        .where(EvidenceDocument.run_ref == run.run_ref)
        .order_by(EvidenceDocument.id.asc())
    ).scalars()
    for document in rows:
        grouped[document.match_id].append(document)
    return grouped


def generate_csv(db: Session, run: ScreeningRun) -> str:
    """Render the full adjudication report for a completed run."""
    evidence = _evidence_by_match(db, run)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(REPORT_COLUMNS)

    stmt = (
        select(MatchResult)
        .where(MatchResult.run_id == run.id)
        .order_by(
            MatchResult.region.asc(),
            MatchResult.score.desc(),
            MatchResult.grid.asc(),
            MatchResult.id.asc(),
        )
        .execution_options(yield_per=1000)
    )

    for match in db.execute(stmt).scalars():
        writer.writerow(
            [
                match.grid,
                match.counterparty_group,
                match.brand,
                match.region,
                match.account_country,
                match.account_status,
                match.created_date,
                match.partner_legal_name,
                match.ubo_name,
                match.tax_id,
                match.screened_field,
                match.target_name,
                match.matched_alias or match.target_name,
                match.subject_type,
                match.entity_type,
                match.sanctions_program,
                match.remarks,
                match.source,
                match.geography,
                match.dob,
                match.nationality,
                match.place_of_birth,
                match.gender,
                match.position,
                match.identifiers,
                match.listing_reason,
                match.linked_to,
                match.listed_on,
                match.watchlist_id,
                f"{match.score:.2f}",
                f"{match.base_score:.2f}",
                f"{match.geo_adjustment:.2f}",
                match.risk_band,
                match.reason_codes,
                match.decision,
                match.risk_level,
                match.reason_code,
                BY_CODE[match.reason_code].reason_label if match.reason_code in BY_CODE else "",
                " | ".join(d.original_filename for d in evidence.get(match.id, [])),
                " | ".join(d.sha256 for d in evidence.get(match.id, [])),
                match.comment,
                match.decided_by,
                _local(match.decided_at),
                _utc(match.decided_at),
                _local(match.screened_at_utc),
                _utc(match.screened_at_utc),
                run.run_ref,
                run.input_sha256,
                _snapshot_digest(run, match.source),
                str(run.threshold),
                run.subject_types,
            ]
        )

    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Due Diligence Record (dossier)
# ---------------------------------------------------------------------------

DOSSIER_WIDTH = 78


def _rule(char: str = "-") -> str:
    return char * DOSSIER_WIDTH


def _wrap(label: str, value: str, indent: int = 16) -> str:
    """Render a labelled field, wrapping long rationale text under the label."""
    import textwrap

    value = (value or "—").strip() or "—"
    body = textwrap.wrap(value, width=DOSSIER_WIDTH - indent) or ["—"]
    pad = " " * indent
    # A label at or past the indent would otherwise butt straight against its
    # value ("Designation data:Cambodia").
    first = (
        f"{label:<{indent}}{body[0]}"
        if len(label) < indent
        else f"{label} {body[0]}"
    )
    rest = "".join(f"\n{pad}{line}" for line in body[1:])
    return first + rest


@dataclass
class DossierData:
    """Everything both dossier renderers need, gathered once.

    The text and PDF records must never diverge — two versions of the same
    compliance document disagreeing is a defect in its own right — so both
    consume this.
    """

    run: ScreeningRun
    is_search: bool
    profile: dict
    query: dict
    identity: str
    evidence: dict
    matches: list
    reviewed: list
    outstanding: list
    ungrounded: list
    by_decision: dict
    by_reason: dict
    chain: dict


def collect_dossier(db: Session, run: ScreeningRun) -> DossierData:
    evidence = _evidence_by_match(db, run)
    matches = list(
        db.execute(
            select(MatchResult)
            .where(MatchResult.run_id == run.id)
            .order_by(
                MatchResult.decision.asc(),
                MatchResult.score.desc(),
                MatchResult.id.asc(),
            )
        ).scalars()
    )
    reviewed = [m for m in matches if m.decision]
    outstanding = [m for m in matches if not m.decision]
    by_decision: dict[str, int] = defaultdict(int)
    by_reason: dict[str, int] = defaultdict(int)
    for m in reviewed:
        by_decision[m.decision] += 1
        if m.reason_code:
            by_reason[m.reason_code] += 1

    profile = run.input_profile or {}
    return DossierData(
        run=run,
        is_search=(run.origin or "batch") == "search",
        profile=profile,
        query=profile.get("query") or {},
        identity=profile.get("identity_provided") or "none",
        evidence=evidence,
        matches=matches,
        reviewed=reviewed,
        outstanding=outstanding,
        ungrounded=[m for m in reviewed if not m.reason_code],
        by_decision=dict(by_decision),
        by_reason=dict(by_reason),
        chain=verify_chain(db),
    )


def generate_dossier(db: Session, run: ScreeningRun) -> str:
    """Plain-text due-diligence record. See collect_dossier for the data."""
    data = collect_dossier(db, run)
    evidence = data.evidence
    matches, reviewed, outstanding = data.matches, data.reviewed, data.outstanding
    by_decision, by_reason, chain = data.by_decision, data.by_reason, data.chain

    out: list[str] = []
    add = out.append

    is_search, profile, query = data.is_search, data.profile, data.query

    add(_rule("="))
    add("DUE DILIGENCE RECORD".center(DOSSIER_WIDTH))
    add("Delivery Hero Group — Sanctions Screening".center(DOSSIER_WIDTH))
    add(
        ("Manual name screening" if is_search else "Batch partner screening").center(
            DOSSIER_WIDTH
        )
    )
    add(_rule("="))
    add("")
    if is_search:
        add(_wrap("Party screened:", query.get("query") or run.uploaded_filename))
        add(_wrap("Name as matched:", query.get("normalized") or "—"))
        if query.get("subject_reference"):
            add(_wrap("Internal ref:", query["subject_reference"]))
        add(_wrap("Identity given:", profile.get("identity_provided") or "none"))
        if query.get("note"):
            add(_wrap("Reason for check:", query["note"]))
        add(_wrap("Screened by:", run.uploaded_by))
    else:
        add(_wrap("Source file:", run.uploaded_filename))
    add(_wrap("Run reference:", run.run_ref))
    add(_wrap("Input SHA-256:", run.input_sha256))
    add(_wrap("Screened:", f"{_local(run.completed_at)}  ({_utc(run.completed_at)})"))
    add(_wrap("Threshold:", f"{run.threshold}% similarity"))
    add(_wrap("Scope:", run.subject_types))
    if not is_search:
        add(
            _wrap(
                "Rows / names:",
                f"{run.total_rows:,} rows, {run.distinct_names:,} distinct names",
            )
        )
    add(_wrap("Alerts raised:", f"{run.total_matches:,}"))
    add("")
    add("Watchlist snapshot screened against:")
    for source, entry in sorted((run.watchlist_snapshot or {}).items()):
        digest = (entry.get("payload_sha256") or "")[:16]
        count = entry.get("record_count", 0)
        add(f"    {source:<16} {count:>8,} designations   sha256 {digest}…")
    add("")
    add(_rule())
    add("REVIEW SUMMARY")
    add(_rule())
    add(_wrap("Reviewed:", f"{len(reviewed):,} of {len(matches):,} alerts"))
    for decision, count in sorted(by_decision.items()):
        add(f"    {decision:<24} {count:>6,}")
    if outstanding:
        add(f"    {'UNREVIEWED':<24} {len(outstanding):>6,}   <-- outstanding")
    if by_reason:
        add("")
        add("Grounds relied upon:")
        for code, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            label = BY_CODE[code].reason_label if code in BY_CODE else code
            add(f"    {count:>5,}  {code}")
            add(f"           {label}")

    # Decisions predating the reason-code control, or recorded through an older
    # client, carry no grounds. Surfaced here because an undocumented
    # disposition is an audit finding, not a cosmetic gap.
    ungrounded = data.ungrounded
    if ungrounded:
        add("")
        add(f"!!  {len(ungrounded):,} decision(s) carry NO stated grounds.")
        add("    These are not defensible as recorded and should be revisited:")
        for m in ungrounded[:40]:
            add(f"      {m.decision:<16} {(m.partner_legal_name or m.ubo_name)[:44]}")
        if len(ungrounded) > 40:
            add(f"      … and {len(ungrounded) - 40:,} more")
    add("")
    add(_wrap("Audit chain:", f"{'VERIFIED INTACT' if chain['valid'] else 'BROKEN — ' + chain['detail']}"
                              f" ({chain['events_checked']:,} events)"))
    add("")

    # A screening that returned nothing is a positive result, not an empty
    # page. Stating it plainly — with the threshold and the exact list versions
    # it was tested against — is what makes "we checked and they were clear"
    # defensible rather than an assertion.
    if not matches:
        add(_rule("="))
        add("RESULT: NO DESIGNATION MATCHED")
        add(_rule("="))
        subject = query.get("query") or run.uploaded_filename
        add(
            _wrap(
                "Finding:",
                f"{subject} was screened against the watchlist snapshot recorded "
                f"above and produced no match at or above the {run.threshold}% "
                f"similarity threshold, within the scope '{run.subject_types}'.",
            )
        )
        add("")
        add("Limits of this finding — it is valid only for:")
        add(f"    · the threshold applied ({run.threshold}%); a lower one may match")
        add(f"    · the subject types screened ({run.subject_types})")
        add("    · the list versions and date stated above; designations change")
        add("    · the name and identifiers as supplied, exactly as given")
        add("")

    if reviewed:
        add(_rule("="))
        add("ADJUDICATED ALERTS")
        add(_rule("="))
        for index, m in enumerate(reviewed, start=1):
            docs = evidence.get(m.id, [])
            add("")
            label, risk = status_of(m.decision, m.risk_level)
            add(f"[{index}] {label}  ·  {risk} risk")
            add(_rule())
            add(_wrap("Partner:", f"{m.partner_legal_name or m.ubo_name}"))
            add(_wrap("GRID / country:", f"{m.grid or '—'} · {m.account_country or 'unknown'} · {m.region}"))
            add(_wrap("Designation:", m.target_name))
            add(_wrap("List / ref:", f"{m.source} · {m.watchlist_id or '—'} · {m.subject_type}"))
            if m.sanctions_program:
                add(_wrap("Programme:", m.sanctions_program))

            # The identity as published, field by field. This used to collapse
            # to one line reading "no country · no DOB", which was printed into
            # the permanent record even where the regulator did state both —
            # OFAC publishes date of birth, nationality and document numbers
            # inside its remarks text, which went unparsed. A reviewer cannot
            # test "different person" against a blank.
            for label, value in (
                ("Date of birth:", m.dob),
                ("Nationality:", m.nationality),
                ("Place of birth:", m.place_of_birth),
                ("Gender:", m.gender),
                ("Address country:", m.geography),
                ("Identity documents:", m.identifiers),
                ("Role / position:", m.position),
            ):
                if value:
                    add(_wrap(label, value))
            if not any((m.dob, m.nationality, m.place_of_birth, m.geography, m.identifiers)):
                add(
                    _wrap(
                        "Identity data:",
                        "this designation carries no identity detail beyond the "
                        "name, so identity could not be used to separate this "
                        "party from a namesake",
                    )
                )

            # Why the party is listed. Quoted, never paraphrased: a dismissal
            # has to be defensible against the grounds the regulator actually
            # gave, and only the UK and UN publish a narrative one.
            if m.listing_reason:
                add(_wrap("Grounds for listing:", m.listing_reason))
                add(_wrap("", f"(as published by {m.source})"))
            elif m.position:
                add(
                    _wrap(
                        "Grounds for listing:",
                        f"{m.source} publishes no narrative reason; the role "
                        f"stated on the record is: {m.position}",
                    )
                )
            else:
                add(
                    _wrap(
                        "Grounds for listing:",
                        f"not published by {m.source} with this record; the "
                        f"grounds are in the designating instrument"
                        + (f" ({m.sanctions_program})" if m.sanctions_program else ""),
                    )
                )
            if m.listed_on:
                add(_wrap("Listed on:", m.listed_on))
            if m.linked_to:
                add(_wrap("Linked to:", m.linked_to))
            add(_wrap("Match score:", f"{m.score:.0f}% ({m.risk_band}); matched on {m.screened_field}"))
            if m.reason_codes:
                add(_wrap("Engine reasons:", m.reason_codes.replace(",", ", ")))
            add("")
            reason = BY_CODE.get(m.reason_code)
            add(_wrap("Reason:", f"{m.reason_code or 'NOT STATED'}"))
            if reason:
                add(_wrap("", reason.reason_label))
            add(_wrap("Rationale:", m.comment))
            if docs:
                add(_wrap("Evidence:", f"{len(docs)} document(s)"))
                for d in docs:
                    flag = " [SUPERSEDED]" if d.superseded else ""
                    add(f"                - {d.original_filename}{flag}")
                    add(f"                  sha256 {d.sha256}")
                    add(f"                  {d.size_bytes:,} bytes, uploaded {_local(d.uploaded_at)}")
                    if d.note:
                        add(_wrap("", f"note: {d.note}", indent=18))
                    analysis = d.analysis or {}
                    if analysis:
                        if not analysis.get("readable"):
                            add(_wrap("", "AUTOMATIC CROSS-CHECK: NOT READABLE — "
                                          + (analysis.get("extraction_detail") or ""),
                                      indent=18))
                        else:
                            add(_wrap("", f"automatic cross-check ({d.extracted_chars:,} "
                                          f"chars read, confidence "
                                          f"{analysis.get('confidence','none')}):",
                                      indent=18))
                            for finding in analysis.get("findings", []):
                                add(_wrap("", f"[{finding['verdict']}] {finding['check']} — "
                                              f"{finding['detail']}", indent=20))
                        add(_wrap("", f"engine advised: {analysis.get('recommendation','')}",
                                  indent=18))
            else:
                add(_wrap("Evidence:", "none attached"))
            add(_wrap("Decided by:", m.decided_by or "—"))
            add(_wrap("Decided at:", f"{_local(m.decided_at)}  ({_utc(m.decided_at)})"))

    if outstanding:
        add("")
        add(_rule("="))
        add(f"OUTSTANDING — {len(outstanding):,} ALERT(S) NOT YET REVIEWED")
        add(_rule("="))
        add("These alerts carry no decision. The review is incomplete and this")
        add("record should not be represented as a completed screening.")
        add("")
        for m in outstanding[:200]:
            add(f"  {m.score:>5.0f}%  {(m.partner_legal_name or m.ubo_name)[:34]:<34} "
                f"-> {m.target_name[:30]:<30} [{m.source}]")
        if len(outstanding) > 200:
            add(f"  … and {len(outstanding) - 200:,} more")

    add("")
    add(_rule("="))
    add("Generated by the DH Group Sanctions Screening Engine.")
    add("Timestamps are Europe/Berlin with the UTC equivalent in brackets; UTC")
    add("is the authoritative value and the one the audit chain commits to.")
    add("Evidence SHA-256 digests can be re-verified against the stored files.")
    add("Automatic document cross-checks are advisory: every decision recorded")
    add("here was made and accepted by the named person, not by the engine.")
    add(_rule("="))
    return "\n".join(out) + "\n"
