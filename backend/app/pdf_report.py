"""PDF rendering of the due-diligence record.

Consumes the same ``DossierData`` as the plain-text version, so the two can
never disagree about what was decided.

Two decisions worth knowing about:

**Fonts.** The PDF core fonts are Latin-1 only, which would mangle the Cyrillic,
Greek and accented names that fill a sanctions list. A system TrueType font is
registered instead when one is available. Where a name still contains characters
the font cannot draw, the ASCII transliteration is printed beneath it rather
than silently dropping glyphs — for a compliance record, showing both the
original script and its romanisation is better than showing neither.

**Pagination.** Alert blocks are kept together where they fit on one page. A
dismissal rationale split across a page break, away from the alert it belongs
to, is how a reader mis-attributes a decision.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fpdf import FPDF
from unidecode import unidecode

from .dismissal import BY_CODE
from .models import as_utc, to_local
from .report import DossierData, status_of

logger = logging.getLogger(__name__)

# Preference order: broad Unicode coverage first, then anything serviceable.
_FONT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("arial.ttf", "arialbd.ttf"),
    ("segoeui.ttf", "segoeuib.ttf"),
    ("tahoma.ttf", "tahomabd.ttf"),
    ("verdana.ttf", "verdanab.ttf"),
)

_FONT_DIRS: tuple[Path, ...] = (
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype"),
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
)

INK = (16, 24, 34)
DIM = (90, 102, 122)
RULE = (200, 208, 218)
ACCENT = (150, 88, 16)
CRITICAL = (150, 42, 26)
OK = (24, 92, 68)


def _find_font_pair() -> tuple[Path, Path] | None:
    for regular_name, bold_name in _FONT_CANDIDATES:
        for directory in _FONT_DIRS:
            regular = directory / regular_name
            bold = directory / bold_name
            if regular.is_file():
                return regular, (bold if bold.is_file() else regular)
    return None


class DossierPDF(FPDF):
    """A4 record with a running header, footer and page numbering."""

    def __init__(self, title: str, subtitle: str, run_ref: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.doc_title = title
        self.doc_subtitle = subtitle
        self.run_ref = run_ref
        self.unicode_ok = False

        pair = _find_font_pair()
        if pair:
            try:
                self.add_font("body", "", str(pair[0]))
                self.add_font("body", "B", str(pair[1]))
                self.base = "body"
                self.unicode_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not register %s: %s", pair[0], exc)
                self.base = "helvetica"
        else:
            logger.warning("no TrueType font found; falling back to Latin-1 core font")
            self.base = "helvetica"

        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(18, 16, 18)
        self.set_title(title)

    # -- text safety -------------------------------------------------------
    def safe(self, text: object) -> str:
        """Render text the font can actually draw."""
        value = "" if text is None else str(text)
        if self.unicode_ok:
            return value
        # Core fonts are Latin-1: transliterate rather than raise mid-document.
        return unidecode(value)

    def needs_translit(self, text: str) -> bool:
        """True when a romanisation should be shown alongside the original."""
        if not text:
            return False
        try:
            text.encode("ascii")
            return False
        except UnicodeEncodeError:
            return unidecode(text) != text or True

    # -- chrome ------------------------------------------------------------
    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font(self.base, "", 7.5)
        self.set_text_color(*DIM)
        self.cell(0, 4, self.safe(f"{self.doc_title} — {self.doc_subtitle}"), align="L")
        self.cell(0, 4, self.safe(self.run_ref[:8]), align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*RULE)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y() + 1, self.w - self.r_margin, self.get_y() + 1)
        self.ln(4)
        self.set_text_color(*INK)

    def footer(self) -> None:
        self.set_y(-16)
        self.set_font(self.base, "", 7)
        self.set_text_color(*DIM)
        self.multi_cell(
            0,
            3.2,
            self.safe(
                f"Page {self.page_no()} of {{nb}}   ·   Run {self.run_ref}   ·   "
                "Timestamps are Europe/Berlin with UTC in brackets; UTC is authoritative."
            ),
            align="C",
        )
        self.set_text_color(*INK)

    # -- building blocks ---------------------------------------------------
    def rule(self, gap_before: float = 1.5, gap_after: float = 2.5) -> None:
        self.ln(gap_before)
        self.set_draw_color(*RULE)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(gap_after)

    def section(self, heading: str) -> None:
        self.ln(3)
        self.set_font(self.base, "B", 10.5)
        self.set_text_color(*INK)
        self.cell(0, 5.5, self.safe(heading.upper()), new_x="LMARGIN", new_y="NEXT")
        self.rule(0.8, 2.2)

    def field(self, label: str, value: str, *, bold: bool = False, colour=None) -> None:
        """A label in the gutter with its value wrapped alongside."""
        label_width = 40
        safe_label = self.safe(label)
        self.set_font(self.base, "", 8.5)
        self.set_text_color(*DIM)

        # A label wider than the gutter would otherwise collide with its value
        # ("CONFIRMED_DESIGNATED_PARTYConfirmed: partner is..."). Give it its
        # own line and indent the value beneath.
        overflows = self.get_string_width(safe_label) > label_width - 2
        if overflows:
            self.multi_cell(0, 4.6, safe_label, new_x="LMARGIN", new_y="NEXT")
        else:
            self.cell(label_width, 4.6, safe_label, new_x="RIGHT", new_y="TOP")

        self.set_font(self.base, "B" if bold else "", 9)
        self.set_text_color(*(colour or INK))
        self.set_x(self.l_margin + label_width)
        self.multi_cell(
            self.w - self.r_margin - self.l_margin - label_width,
            4.6,
            self.safe(value or "—"),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        self.set_text_color(*INK)

    def mono(self, label: str, value: str) -> None:
        """Digests and references: fixed width, small, unambiguous."""
        label_width = 40
        self.set_font(self.base, "", 8.5)
        self.set_text_color(*DIM)
        self.cell(label_width, 4.4, self.safe(label), new_x="RIGHT", new_y="TOP")
        self.set_font("courier", "", 7.6)
        self.set_text_color(*INK)
        self.set_x(self.l_margin + label_width)
        self.multi_cell(
            self.w - self.r_margin - self.l_margin - label_width,
            4.4,
            value or "—",
            new_x="LMARGIN",
            new_y="NEXT",
        )

    def para(self, text: str, size: float = 8.5, colour=None) -> None:
        self.set_font(self.base, "", size)
        self.set_text_color(*(colour or DIM))
        self.multi_cell(0, 4.2, self.safe(text), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*INK)


def _stamp(value) -> str:  # noqa: ANN001
    local, utc = to_local(value), as_utc(value)
    if not local:
        return "—"
    return (
        f"{local.strftime('%d %b %Y, %H:%M')} {local.tzname()}"
        f"  ({utc.strftime('%Y-%m-%dT%H:%M:%SZ')})"
    )


def generate_pdf(data: DossierData) -> bytes:
    run = data.run
    subject = data.query.get("query") or run.uploaded_filename
    pdf = DossierPDF(
        "Due Diligence Record",
        "Manual name screening" if data.is_search else "Batch partner screening",
        run.run_ref,
    )
    pdf.alias_nb_pages()
    pdf.add_page()

    # ---- title block ----------------------------------------------------
    pdf.set_font(pdf.base, "B", 17)
    pdf.set_text_color(*INK)
    pdf.multi_cell(0, 7.5, pdf.safe("Due Diligence Record"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(pdf.base, "", 10)
    pdf.set_text_color(*ACCENT)
    pdf.cell(
        0,
        5,
        pdf.safe(
            "Delivery Hero Group — Sanctions Screening   ·   "
            + ("Manual name screening" if data.is_search else "Batch partner screening")
        ),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.set_text_color(*INK)
    pdf.rule(2, 3)

    # ---- what was screened ----------------------------------------------
    if data.is_search:
        pdf.field("Party screened", subject, bold=True)
        if pdf.needs_translit(subject):
            pdf.field("Romanised", unidecode(subject))
        pdf.field("Name as matched", data.query.get("normalized") or "—")
        if data.query.get("subject_reference"):
            pdf.field("Internal reference", data.query["subject_reference"])
        pdf.field("Identity supplied", data.identity)
        if data.query.get("note"):
            pdf.field("Reason for check", data.query["note"])
        pdf.field("Screened by", run.uploaded_by)
    else:
        pdf.field("Source file", run.uploaded_filename, bold=True)
        pdf.field(
            "Population",
            f"{run.total_rows:,} rows · {run.distinct_names:,} distinct names",
        )
        pdf.field("Uploaded by", run.uploaded_by)

    pdf.field("Screened", _stamp(run.completed_at))
    pdf.field("Threshold", f"{run.threshold}% similarity")
    pdf.field("Scope", run.subject_types)
    pdf.field("Alerts raised", f"{run.total_matches:,}")
    pdf.mono("Run reference", run.run_ref)
    pdf.mono("Input SHA-256", run.input_sha256)

    # ---- lists screened against ------------------------------------------
    pdf.section("Watchlist versions screened against")
    snapshot = run.watchlist_snapshot or {}
    if snapshot:
        pdf.set_font(pdf.base, "", 8)
        for source, entry in sorted(snapshot.items()):
            pdf.set_text_color(*INK)
            pdf.cell(46, 4.4, pdf.safe(source), new_x="RIGHT", new_y="TOP")
            pdf.cell(28, 4.4, f"{entry.get('record_count', 0):,}", align="R",
                     new_x="RIGHT", new_y="TOP")
            pdf.set_font("courier", "", 7.4)
            pdf.set_text_color(*DIM)
            pdf.cell(0, 4.4, (entry.get("payload_sha256") or "")[:32],
                     new_x="LMARGIN", new_y="NEXT")
            pdf.set_font(pdf.base, "", 8)
        pdf.set_text_color(*INK)
    else:
        pdf.para("No snapshot recorded.")

    # ---- review summary --------------------------------------------------
    pdf.section("Review summary")
    pdf.field("Reviewed", f"{len(data.reviewed):,} of {len(data.matches):,} alerts")
    for decision, count in sorted(data.by_decision.items()):
        colour = CRITICAL if decision == "Positive Match" else None
        pdf.field(f"    {decision}", f"{count:,}", colour=colour)
    if data.outstanding:
        pdf.field("    Unreviewed", f"{len(data.outstanding):,}  — outstanding",
                  bold=True, colour=CRITICAL)

    if data.by_reason:
        pdf.ln(1.5)
        pdf.para("Grounds relied upon:")
        for code, count in sorted(data.by_reason.items(), key=lambda kv: -kv[1]):
            label = BY_CODE[code].reason_label if code in BY_CODE else code
            pdf.field(f"    {count:,}", f"{code} — {label}")

    if data.ungrounded:
        pdf.ln(2)
        pdf.set_font(pdf.base, "B", 9)
        pdf.set_text_color(*CRITICAL)
        pdf.multi_cell(
            0,
            4.4,
            pdf.safe(
                f"{len(data.ungrounded):,} decision(s) carry no stated grounds and are "
                "not defensible as recorded."
            ),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.set_text_color(*INK)
        for m in data.ungrounded[:25]:
            pdf.field(f"    {m.decision}", m.partner_legal_name or m.ubo_name)
        if len(data.ungrounded) > 25:
            pdf.para(f"    … and {len(data.ungrounded) - 25:,} more")

    chain = data.chain
    pdf.ln(1)
    pdf.field(
        "Audit chain",
        (
            f"Verified intact — {chain['events_checked']:,} events"
            if chain["valid"]
            else f"BROKEN at sequence {chain['broken_at']} — {chain['detail']}"
        ),
        bold=True,
        colour=OK if chain["valid"] else CRITICAL,
    )

    # ---- clean result ----------------------------------------------------
    if not data.matches:
        pdf.section("Result: no designation matched")
        pdf.set_font(pdf.base, "B", 9.5)
        pdf.set_text_color(*OK)
        pdf.multi_cell(
            0,
            4.6,
            pdf.safe(
                f"{subject} was screened against the watchlist versions recorded above "
                f"and produced no match at or above the {run.threshold}% similarity "
                f"threshold, within the scope '{run.subject_types}'."
            ),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.set_text_color(*INK)
        pdf.ln(2)
        pdf.para("This finding is valid only for:")
        for line in (
            f"the threshold applied ({run.threshold}%); a lower one may match",
            f"the subject types screened ({run.subject_types})",
            "the list versions and date stated above; designations change",
            "the name and identifiers exactly as supplied",
        ):
            pdf.para("   ·  " + line)

    # ---- adjudicated alerts ----------------------------------------------
    if data.reviewed:
        pdf.section(f"Adjudicated alerts ({len(data.reviewed):,})")
        for index, m in enumerate(data.reviewed, start=1):
            docs = data.evidence.get(m.id, [])
            # Keep an alert block together where it plausibly fits.
            if pdf.get_y() > pdf.h - 90:
                pdf.add_page()

            pdf.set_font(pdf.base, "B", 10)
            pdf.set_text_color(*(CRITICAL if m.decision == "Positive Match" else ACCENT))
            pdf.cell(
                0,
                5.2,
                pdf.safe(
                    f"[{index}]  {status_of(m.decision, m.risk_level)[0]}   ·   "
                    f"{status_of(m.decision, m.risk_level)[1]} risk   ·   {m.score:.0f}% match"
                ),
                new_x="LMARGIN",
                new_y="NEXT",
            )
            pdf.set_text_color(*INK)
            pdf.rule(0.6, 1.8)

            pdf.field("Partner", m.partner_legal_name or m.ubo_name, bold=True)
            pdf.field(
                "Reference / country",
                f"{m.grid or '—'}  ·  {m.account_country or 'unknown'}  ·  {m.region}",
            )
            if m.counterparty_group or m.brand:
                pdf.field(
                    "Group / brand",
                    f"{m.counterparty_group or 'not stated'}  ·  {m.brand or 'not stated'}",
                )
            pdf.field("Designation", m.target_name, bold=True)
            if pdf.needs_translit(m.target_name):
                pdf.field("Romanised", unidecode(m.target_name))
            pdf.field(
                "List / reference",
                f"{m.source}  ·  {m.watchlist_id or '—'}  ·  {m.subject_type}",
            )
            if m.sanctions_program:
                pdf.field("Programme", m.sanctions_program)
            # The identity as published, field by field. This was one line
            # reading "no country · no DOB", printed into the permanent record
            # even where the regulator did state both — OFAC carries date of
            # birth, nationality and document numbers inside its remarks text,
            # which went unparsed. A reviewer cannot test "different person"
            # against a blank.
            for label, value in (
                ("Date of birth", m.dob),
                ("Nationality", m.nationality),
                ("Place of birth", m.place_of_birth),
                ("Address country", m.geography),
                ("Identity documents", m.identifiers),
                ("Role / position", m.position),
            ):
                if value:
                    pdf.field(label, value)
            if not any((m.dob, m.nationality, m.place_of_birth, m.geography, m.identifiers)):
                pdf.field(
                    "Identity data",
                    "none published beyond the name, so identity could not be "
                    "used to separate this party from a namesake",
                )
            if m.matched_alias:
                pdf.field("Matched spelling", m.matched_alias)
            if m.aliases:
                pdf.field("Also known as", m.aliases[:700])
            if m.reason_codes:
                pdf.field("Engine reasons", m.reason_codes.replace(",", ", "))

            # Quoted, never paraphrased: a dismissal has to be defensible
            # against the grounds the regulator actually gave.
            if m.listing_reason:
                pdf.field("Grounds for listing", m.listing_reason)
                pdf.field("", f"(as published by {m.source})")
            elif m.position:
                pdf.field(
                    "Grounds for listing",
                    f"{m.source} publishes no narrative reason; the role on "
                    f"the record is: {m.position}",
                )
            if m.listed_on:
                pdf.field("Listed on", m.listed_on)

            reason = BY_CODE.get(m.reason_code)
            pdf.ln(1)
            pdf.field(
                "Reason",
                m.reason_code or "NOT STATED",
                bold=True,
                colour=None if m.reason_code else CRITICAL,
            )
            if reason:
                pdf.field("", reason.reason_label)
            pdf.field("Rationale", m.comment or "—")

            if docs:
                pdf.field("Evidence", f"{len(docs)} document(s)")
                for d in docs:
                    flag = "  [SUPERSEDED]" if d.superseded else ""
                    pdf.field("", f"{d.original_filename}{flag}")
                    pdf.mono("", d.sha256)
                    pdf.field(
                        "",
                        f"{d.size_bytes:,} bytes · uploaded {_stamp(d.uploaded_at)}"
                        + (f" · {d.note}" if d.note else ""),
                    )
                    analysis = d.analysis or {}
                    if analysis:
                        readable = analysis.get("readable")
                        pdf.field(
                            "",
                            (
                                f"Automatic cross-check: {d.extracted_chars:,} characters "
                                f"read, confidence {analysis.get('confidence', 'none')}"
                                if readable
                                else "Automatic cross-check: DOCUMENT NOT READABLE — "
                                + (analysis.get("extraction_detail") or "")
                            ),
                            bold=not readable,
                            colour=None if readable else CRITICAL,
                        )
                        for finding in analysis.get("findings", []):
                            colour = (
                                CRITICAL
                                if finding["verdict"] == "contradicts"
                                else OK
                                if finding["verdict"] == "corroborates"
                                else None
                            )
                            pdf.field(
                                f"      {finding['verdict']}",
                                f"{finding['check']} — {finding['detail']}",
                                colour=colour,
                            )
                        pdf.field("      engine advised", analysis.get("recommendation", ""))
            else:
                pdf.field("Evidence", "none attached")

            pdf.field("Decided by", m.decided_by or "—")
            pdf.field("Decided at", _stamp(m.decided_at))
            pdf.ln(3)

    # ---- outstanding -----------------------------------------------------
    if data.outstanding:
        pdf.add_page()
        pdf.section(f"Outstanding — {len(data.outstanding):,} alert(s) not reviewed")
        pdf.set_font(pdf.base, "B", 9)
        pdf.set_text_color(*CRITICAL)
        pdf.multi_cell(
            0,
            4.4,
            pdf.safe(
                "These alerts carry no decision. The review is incomplete and this "
                "record must not be represented as a completed screening."
            ),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.set_text_color(*INK)
        pdf.ln(2)
        pdf.set_font(pdf.base, "", 8)
        for m in data.outstanding[:250]:
            pdf.cell(16, 4.2, f"{m.score:.0f}%", align="R", new_x="RIGHT", new_y="TOP")
            pdf.cell(
                74,
                4.2,
                pdf.safe((m.partner_legal_name or m.ubo_name)[:44]),
                new_x="RIGHT",
                new_y="TOP",
            )
            pdf.cell(
                0,
                4.2,
                pdf.safe(f"{m.target_name[:40]}  [{m.source}]"),
                new_x="LMARGIN",
                new_y="NEXT",
            )
        if len(data.outstanding) > 250:
            pdf.para(f"… and {len(data.outstanding) - 250:,} more")

    # ---- closing ---------------------------------------------------------
    pdf.ln(4)
    pdf.rule(1, 2)
    pdf.para(
        "Generated by the DH Group Sanctions Screening Engine. Evidence SHA-256 digests "
        "can be re-verified against the stored files. Automatic document cross-checks are "
        "advisory only: every decision recorded here was made and accepted by the named "
        "person, not by the engine. Timestamps show Europe/Berlin with "
        "the UTC equivalent in brackets; UTC is the authoritative value and the one the "
        "audit chain commits to.",
        size=7.5,
    )
    if not pdf.unicode_ok:
        pdf.para(
            "Note: no Unicode font was available on the generating machine, so non-Latin "
            "names in this document have been romanised.",
            size=7.5,
            colour=CRITICAL,
        )

    return bytes(pdf.output())
