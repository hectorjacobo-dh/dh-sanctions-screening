"""Screening run endpoints: upload, monitor, inspect, adjudicate, export."""

from __future__ import annotations

import csv
import io

import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi import Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..audit import record_event
from ..auth import current_user, require_role
from ..config import get_settings
from ..database import SessionLocal, get_db
from ..dismissal import STATUSES, default_risk_for, reason_for
from ..taxonomy import BRANDS, COUNTERPARTY_GROUPS, normalise_brand, normalise_group
from ..doc_crosscheck import cross_check
from ..doc_extract import extract as extract_text
from ..dismissal import validate as validate_reason
from ..evidence import read as read_evidence
from ..evidence import store as store_evidence
from ..evidence import validate_upload as validate_evidence
from ..evidence import verify as verify_evidence
from ..ingest.runner import active_target_count
from ..models import EvidenceDocument, MatchResult, ScreeningRun, User, to_local, utcnow
from ..pdf_report import generate_pdf
from ..report import collect_dossier, generate_csv, generate_xlsx, generate_dossier
from ..schemas import (
    AdjudicationIn,
    EvidenceOut,
    MatchOut,
    MatchPage,
    StatusOut,
    RunOut,
)
from ..screening import STORAGE_FLOOR, MAX_UPLOAD_BYTES, parse_upload, run_screening, sha256_of

router = APIRouter(prefix="/api/runs", tags=["screening"])


def _screen_task(run_id: int, payload: bytes, actor: str) -> None:
    db = SessionLocal()
    try:
        run_screening(db, run_id, payload, actor=actor)
    finally:
        db.close()


# The column names the parser recognises without any guessing. Offered as a
# download so a counterparty list can be prepared in the shape the engine reads
# best, rather than relying on content-based detection — which works, but tells
# the officer afterwards rather than before.
TEMPLATE_COLUMNS = ["Name", "Country", "Tax ID", "Internal reference", "Type"]
TEMPLATE_ROWS = [
    ["Acme Trading GmbH", "Germany", "DE123456789", "VENDOR-001", "Entity"],
    ["Ivanov Sergey", "Russia", "", "UBO-002", "Individual"],
]


@router.get("/taxonomy")
def counterparty_taxonomy(_: User = Depends(current_user)) -> dict[str, list[str]]:
    """Counterparty groups and operating brands offered by the forms.

    Served from the module the writers validate against, so a form can never
    offer a value the API would silently drop.
    """
    return {"groups": list(COUNTERPARTY_GROUPS), "brands": list(BRANDS)}


@router.get("/template.csv")
def batch_template_csv(_: User = Depends(current_user)) -> Response:
    """A ready-to-fill CSV for batch screening."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(TEMPLATE_COLUMNS)
    writer.writerows(TEMPLATE_ROWS)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="batch-by-name-template.csv"'},
    )


@router.get("/template.xlsx")
def batch_template_xlsx(_: User = Depends(current_user)) -> Response:
    """The same template as a workbook, for people who live in Excel."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Batch by name"
    sheet.append(TEMPLATE_COLUMNS)
    for row in TEMPLATE_ROWS:
        sheet.append(row)
    for index, column in enumerate(TEMPLATE_COLUMNS, start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = (
            max(18, len(column) + 4)
        )
    stream = io.BytesIO()
    workbook.save(stream)
    return Response(
        content=stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="batch-by-name-template.xlsx"'},
    )


@router.post("", response_model=RunOut, status_code=202)
async def create_run(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    threshold: int = Form(default=-1),
    geo_penalty: int = Form(default=-1),
    subject_types: str = Form(default=""),
    counterparty_group: str = Form(default=""),
    brand: str = Form(default=""),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
) -> ScreeningRun:
    """Upload a partner CSV and start a screening run."""
    settings = get_settings()

    if active_target_count(db) == 0:
        raise HTTPException(
            409,
            "No active watchlist snapshot. An administrator must refresh the "
            "watchlists before screening can run.",
        )

    payload = await file.read()
    if not payload:
        raise HTTPException(400, "Uploaded file is empty.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
        )

    # Fail fast on an unparseable upload rather than in the background task.
    # Bounded to a prefix so a 1M-row export does not hold the request open.
    #
    # Only the header is validated. Deliberately NOT rejected: a prefix with no
    # populated names. Real exports carry long runs of blank Legal Name rows, so
    # judging screenability from a sample produces false rejections — the
    # background task determines the true count.
    try:
        preview = parse_upload(payload, filename=file.filename or "", max_rows=1000)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if preview.total_rows == 0:
        raise HTTPException(400, "The uploaded file contains a header but no data rows.")

    run = ScreeningRun(
        run_ref=str(uuid.uuid4()),
        uploaded_filename=file.filename or "upload.csv",
        uploaded_by=user.email,
        input_sha256=sha256_of(payload),
        threshold=threshold if threshold >= 0 else settings.match_threshold,
        score_floor=min(threshold if threshold >= 0 else settings.match_threshold, STORAGE_FLOOR),
        strong_threshold=settings.strong_match_threshold,
        geo_penalty=geo_penalty if geo_penalty >= 0 else settings.geo_contradiction_penalty,
        subject_types=subject_types.strip() or settings.screen_subject_types,
        counterparty_group=normalise_group(counterparty_group),
        brand=normalise_brand(brand),
        input_profile={
            "format": preview.delimiter,
            "header_row": preview.header_row,
            "columns": preview.detected_columns,
            "name_column_detection": preview.detection_note or "matched by header",
        },
        status="queued",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    record_event(
        db,
        "SCREENING_RUN_SUBMITTED",
        actor=user.email,
        entity_ref=run.run_ref,
        payload={
            "filename": run.uploaded_filename,
            "input_sha256": run.input_sha256,
            "threshold": run.threshold,
            "geo_penalty": run.geo_penalty,
            "detected_columns": preview.detected_columns,
            "detected_format": preview.delimiter,
            "header_row": preview.header_row,
            "name_column_detection": preview.detection_note or "matched by header",
        },
    )

    background.add_task(_screen_task, run.id, payload, user.email)
    return run


@router.get("", response_model=list[RunOut])
def list_runs(
    limit: int = Query(default=50, le=200),
    counterparty_group: str = Query(default=""),
    brand: str = Query(default=""),
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[ScreeningRun]:
    """Cases, newest first, optionally narrowed to one group or brand."""
    stmt = select(ScreeningRun)
    if group := normalise_group(counterparty_group):
        stmt = stmt.where(ScreeningRun.counterparty_group == group)
    if wanted_brand := normalise_brand(brand):
        stmt = stmt.where(ScreeningRun.brand == wanted_brand)
    return list(db.execute(stmt.order_by(ScreeningRun.id.desc()).limit(limit)).scalars())


def _get_run(db: Session, run_ref: str) -> ScreeningRun:
    run = db.execute(
        select(ScreeningRun).where(ScreeningRun.run_ref == run_ref)
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


@router.get("/{run_ref}", response_model=RunOut)
def get_run(
    run_ref: str, db: Session = Depends(get_db), _: User = Depends(current_user)
) -> ScreeningRun:
    return _get_run(db, run_ref)


@router.get("/{run_ref}/matches", response_model=MatchPage)
def list_matches(
    run_ref: str,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
    region: str | None = None,
    source: str | None = None,
    risk_band: str | None = None,
    decision: str | None = None,
    min_score: float | None = Query(default=None, ge=0, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> MatchPage:
    run = _get_run(db, run_ref)

    filters = [MatchResult.run_id == run.id]
    if region:
        filters.append(MatchResult.region == region)
    if source:
        filters.append(MatchResult.source == source)
    if risk_band:
        filters.append(MatchResult.risk_band == risk_band)
    if decision is not None and decision != "":
        filters.append(MatchResult.decision == decision)
    # The name score, defaulting to the threshold the run was screened at. The bar can
    # be lowered as far as the stored floor, which is how the officer sees the
    # 85-94 band without screening again.
    filters.append(
        MatchResult.base_score >= (run.threshold if min_score is None else min_score)
    )

    total = db.execute(
        select(func.count(MatchResult.id)).where(*filters)
    ).scalar_one()

    items = list(
        db.execute(
            select(MatchResult)
            .where(*filters)
            .order_by(MatchResult.score.desc(), MatchResult.id.asc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )

    # Evidence counts for just this page, so the UI can show an attachment
    # indicator without a round-trip per row.
    counts: dict[int, int] = {}
    if items:
        rows = db.execute(
            select(EvidenceDocument.match_id, func.count(EvidenceDocument.id))
            .where(EvidenceDocument.match_id.in_([m.id for m in items]))
            .group_by(EvidenceDocument.match_id)
        ).all()
        counts = dict(rows)

    out: list[MatchOut] = []
    for m in items:
        model = MatchOut.model_validate(m)
        model.evidence_count = counts.get(m.id, 0)
        out.append(model)

    return MatchPage(items=out, total=total, limit=limit, offset=offset)


@router.get("/{run_ref}/summary")
def run_summary(
    run_ref: str, db: Session = Depends(get_db), _: User = Depends(current_user)
) -> dict:
    """Region x source and region x decision pivots, mirroring the workbook."""
    run = _get_run(db, run_ref)

    by_source = db.execute(
        select(
            MatchResult.region,
            MatchResult.source,
            func.count(func.distinct(MatchResult.grid)),
        )
        .where(MatchResult.run_id == run.id)
        .group_by(MatchResult.region, MatchResult.source)
    ).all()

    by_decision = db.execute(
        select(
            MatchResult.region,
            MatchResult.decision,
            func.count(func.distinct(MatchResult.grid)),
        )
        .where(MatchResult.run_id == run.id)
        .group_by(MatchResult.region, MatchResult.decision)
    ).all()

    by_band = db.execute(
        select(MatchResult.risk_band, func.count(MatchResult.id))
        .where(MatchResult.run_id == run.id)
        .group_by(MatchResult.risk_band)
    ).all()

    return {
        "run_ref": run.run_ref,
        "by_region_source": [
            {"region": r, "source": s, "unique_partners": c} for r, s, c in by_source
        ],
        "by_region_decision": [
            {"region": r, "decision": d or "Unreviewed", "unique_partners": c}
            for r, d, c in by_decision
        ],
        "by_risk_band": [{"risk_band": b, "alerts": c} for b, c in by_band],
        "watchlist_snapshot": run.watchlist_snapshot,
    }


@router.get("/reasons/catalogue", response_model=list[StatusOut])
def status_catalogue(_: User = Depends(current_user)) -> list[StatusOut]:
    """The resolution matrix: which risk levels each status may carry."""
    return [
        StatusOut(
            value=st.value,
            label=st.label,
            reason_code=st.reason_code,
            reason_label=st.reason_label,
            risks=list(st.risks),
            default_risk=st.default_risk,
        )
        for st in STATUSES
    ]


@router.delete("/{run_ref}", status_code=200)
def delete_run(
    run_ref: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
) -> dict[str, object]:
    """Remove a case and its alerts.

    The case goes; the audit chain does not. Deleting is itself recorded, with
    a count of how many alerts had already been resolved, so a screening cannot
    be made to disappear from the record — only from the worklist. That is the
    difference between tidying up and destroying evidence, and a compliance
    tool has to be on the right side of it.
    """
    run = _get_run(db, run_ref)

    total = db.execute(
        select(func.count()).select_from(MatchResult).where(MatchResult.run_id == run.id)
    ).scalar_one()
    resolved = db.execute(
        select(func.count())
        .select_from(MatchResult)
        .where(MatchResult.run_id == run.id, MatchResult.decided_at.is_not(None))
    ).scalar_one()

    record_event(
        db,
        "SCREENING_RUN_DELETED",
        actor=user.email,
        entity_ref=run.run_ref,
        payload={
            "case": run.uploaded_filename,
            "origin": run.origin,
            "started_at_utc": str(run.started_at),
            "alerts": total,
            "already_resolved": resolved,
            "input_sha256": run.input_sha256,
        },
    )

    db.execute(delete(MatchResult).where(MatchResult.run_id == run.id))
    db.delete(run)
    db.commit()
    return {"deleted": run_ref, "alerts_removed": total, "were_resolved": resolved}


@router.patch("/{run_ref}/matches/{match_id}", response_model=MatchOut)
def adjudicate(
    run_ref: str,
    match_id: int,
    body: AdjudicationIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
) -> MatchResult:
    """Record an analyst decision. Every change is written to the audit chain."""
    run = _get_run(db, run_ref)
    match = db.get(MatchResult, match_id)
    if match is None or match.run_id != run.id:
        raise HTTPException(404, "Match not found in this run")

    # Enforced here, not only in the form: clearing or confirming an alert
    # without stated grounds is the failure this control exists to prevent.
    # The reason restates the match quality, so it follows from the status
    # rather than being a second thing to get wrong. A client may still send
    # one; it is validated against the status and otherwise derived.
    reason_code = body.reason_code or reason_for(body.decision)
    risk_level = body.risk_level or default_risk_for(body.decision)

    problem = validate_reason(body.decision, reason_code, body.comment, risk_level)
    if problem:
        raise HTTPException(422, problem)

    before = {
        "decision": match.decision,
        "reason_code": match.reason_code,
        "risk_level": match.risk_level,
        "comment": match.comment,
    }

    match.decision = body.decision
    match.reason_code = reason_code
    match.risk_level = risk_level
    match.comment = body.comment
    match.decided_by = user.email
    match.decided_at = utcnow()
    db.commit()

    record_event(
        db,
        "ADJUDICATION_RECORDED",
        actor=user.email,
        entity_ref=f"{run.run_ref}:{match.id}",
        payload={
            "before": before,
            "after": {
                "decision": match.decision,
                "reason_code": match.reason_code,
                "risk_level": match.risk_level,
                "comment": match.comment,
            },
            "grid": match.grid,
            "partner_legal_name": match.partner_legal_name,
            "target_name": match.target_name,
            "source": match.source,
            "watchlist_id": match.watchlist_id,
            "score": match.score,
        },
    )
    db.refresh(match)
    return match


# ---------------------------------------------------------------------------
# Evidence documents
# ---------------------------------------------------------------------------


def _get_match(db: Session, run: ScreeningRun, match_id: int) -> MatchResult:
    match = db.get(MatchResult, match_id)
    if match is None or match.run_id != run.id:
        raise HTTPException(404, "Match not found in this run")
    return match


@router.post(
    "/{run_ref}/matches/{match_id}/evidence", response_model=EvidenceOut, status_code=201
)
async def upload_evidence(
    run_ref: str,
    match_id: int,
    file: UploadFile = File(...),
    note: str = Form(default=""),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("admin", "analyst")),
) -> EvidenceDocument:
    """Attach a due-diligence document to an alert.

    Stored under its own SHA-256 and the digest written to the audit chain, so
    the record proves *this exact document* existed when the alert was cleared.
    """
    run = _get_run(db, run_ref)
    match = _get_match(db, run, match_id)

    payload = await file.read()
    problem = validate_evidence(file.filename or "", payload)
    if problem:
        raise HTTPException(400, problem)

    stored = store_evidence(payload, file.filename or "document")

    # Read the document and cross-check it against this designation. Runs on
    # attach so the officer sees the findings while deciding, not afterwards.
    extraction = extract_text(payload, file.filename or "")
    analysis = cross_check(
        text=extraction.text,
        extraction_status=extraction.status,
        extraction_detail=extraction.detail,
        partner_name=match.partner_legal_name or match.ubo_name,
        target_name=match.target_name,
        target_dob=match.dob,
        target_geography=match.geography,
        target_watchlist_id=match.watchlist_id,
        target_remarks=match.remarks,
    )

    document = EvidenceDocument(
        match_id=match.id,
        run_ref=run.run_ref,
        original_filename=(file.filename or "document")[:400],
        stored_filename=stored.stored_filename,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        content_type=(file.content_type or "")[:120],
        note=note[:2000],
        uploaded_by=user.email,
        extraction_status=extraction.status,
        extracted_chars=extraction.char_count,
        analysis=analysis.as_dict(),
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    record_event(
        db,
        "EVIDENCE_ATTACHED",
        actor=user.email,
        entity_ref=f"{run.run_ref}:{match.id}",
        payload={
            "evidence_id": document.id,
            "filename": document.original_filename,
            "sha256": document.sha256,
            "size_bytes": document.size_bytes,
            "note": document.note,
            "deduplicated": stored.deduplicated,
            "extraction_status": extraction.status,
            "extracted_chars": extraction.char_count,
            # The findings go in the chain too: what the engine advised, and
            # therefore what the officer was looking at when they decided.
            "cross_check": analysis.as_dict(),
        },
    )
    return document


@router.get("/{run_ref}/matches/{match_id}/evidence", response_model=list[EvidenceOut])
def list_evidence(
    run_ref: str,
    match_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[EvidenceDocument]:
    run = _get_run(db, run_ref)
    _get_match(db, run, match_id)
    return list(
        db.execute(
            select(EvidenceDocument)
            .where(EvidenceDocument.match_id == match_id)
            .order_by(EvidenceDocument.id.asc())
        ).scalars()
    )


@router.get("/{run_ref}/evidence/{evidence_id}/download")
def download_evidence(
    run_ref: str,
    evidence_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> FileResponse:
    run = _get_run(db, run_ref)
    document = db.get(EvidenceDocument, evidence_id)
    if document is None or document.run_ref != run.run_ref:
        raise HTTPException(404, "Evidence not found in this run")

    payload = read_evidence(document.stored_filename)
    if payload is None:
        raise HTTPException(
            410, "The stored document is missing from the evidence store."
        )
    if not verify_evidence(document.stored_filename, document.sha256):
        # Integrity is the whole point of hashing it; refuse rather than serve.
        raise HTTPException(
            409,
            "The stored document no longer matches its recorded SHA-256 and may "
            "have been altered. It has not been served.",
        )

    record_event(
        db,
        "EVIDENCE_DOWNLOADED",
        actor=user.email,
        entity_ref=f"{run.run_ref}:{document.match_id}",
        payload={"evidence_id": document.id, "sha256": document.sha256},
    )
    return Response(
        content=payload,
        media_type=document.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{document.original_filename}"'
        },
    )


@router.get("/{run_ref}/due-diligence-record.pdf")
def download_dossier_pdf(
    run_ref: str,
    min_score: float | None = Query(default=None, ge=0, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    """The due-diligence record as a PDF — the version you hand to someone."""
    run = _get_run(db, run_ref)
    data = collect_dossier(db, run, min_score)
    payload = generate_pdf(data)

    record_event(
        db,
        "DUE_DILIGENCE_RECORD_EXPORTED",
        actor=user.email,
        entity_ref=run.run_ref,
        payload={
            "format": "pdf",
            "bytes": len(payload),
            "export_sha256": sha256_of(payload),
            "alerts_reviewed": len(data.reviewed),
            "alerts_outstanding": len(data.outstanding),
        },
    )

    local = to_local(run.completed_at)
    stamp = local.strftime("%Y-%m-%d_%H%M_Berlin") if local else "pending"
    filename = f"DH_Due_Diligence_Record_{stamp}_{run.run_ref[:8]}.pdf"
    return Response(
        content=payload,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{run_ref}/report.csv")
def download_report(
    run_ref: str,
    min_score: float | None = Query(default=None, ge=0, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> StreamingResponse:
    """Download the immutable adjudication report."""
    run = _get_run(db, run_ref)
    if run.status != "completed":
        raise HTTPException(409, f"Run is '{run.status}'; report available once completed.")

    csv_text = generate_csv(db, run, min_score)
    record_event(
        db,
        "REPORT_EXPORTED",
        actor=user.email,
        entity_ref=run.run_ref,
        payload={
            "rows": run.total_matches,
            "export_sha256": sha256_of(csv_text.encode("utf-8")),
        },
    )

    # Berlin time in the filename: this is read by people, and a Berlin-based
    # team should not have to mentally convert. The rows carry both zones.
    local = to_local(run.completed_at)
    stamp = local.strftime("%Y-%m-%d_%H%M_Berlin") if local else "pending"
    filename = f"DH_Sanctions_Adjudication_{stamp}_{run.run_ref[:8]}.csv"

    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{run_ref}/report.xlsx")
def download_report_xlsx(
    run_ref: str,
    min_score: float | None = Query(default=None, ge=0, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    """The adjudication report as an Excel workbook, with the full published record."""
    run = _get_run(db, run_ref)
    if run.status != "completed":
        raise HTTPException(409, f"Run is '{run.status}'; report available once completed.")

    payload = generate_xlsx(db, run, min_score)
    record_event(
        db,
        "REPORT_EXPORTED",
        actor=user.email,
        entity_ref=run.run_ref,
        payload={
            "format": "xlsx",
            "shown_at_or_above": run.threshold if min_score is None else min_score,
            "export_sha256": sha256_of(payload),
        },
    )
    local = to_local(run.completed_at)
    stamp = local.strftime("%Y-%m-%d_%H%M_Berlin") if local else "pending"
    filename = f"DH_Sanctions_Adjudication_{stamp}_{run.run_ref[:8]}.xlsx"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
