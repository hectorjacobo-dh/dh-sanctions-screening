"""Tests for automatic document cross-checking.

The property that matters most is negative: an unreadable document must never
be treated as corroboration. A scanned licence that yields no text looking like
supporting evidence is how an alert gets cleared on nothing at all.
"""

from __future__ import annotations

from app.doc_crosscheck import (
    VERDICT_CONTRADICTS,
    VERDICT_CORROBORATES,
    VERDICT_UNREADABLE,
    cross_check,
)
from app.doc_extract import extract

DESIGNATION = {
    "partner_name": "Bella Napoli Pizzeria Hamburg GmbH",
    "target_name": "BANCO CENTRAL DE VENEZUELA",
    "target_dob": "",
    "target_geography": "Venezuela",
    "target_watchlist_id": "26727",
    "target_remarks": "VENEZUELA-EO13850",
}


def _check(text: str, status: str = "ok", detail: str = "") -> object:
    return cross_check(text=text, extraction_status=status,
                       extraction_detail=detail, **DESIGNATION)


# ---------------------------------------------------------------------------
# The safety property
# ---------------------------------------------------------------------------
def test_an_unreadable_document_never_corroborates() -> None:
    result = _check("", status="empty", detail="scanned image, no text layer")
    assert result.readable is False
    assert result.confidence == "none"
    assert [f.verdict for f in result.findings] == [VERDICT_UNREADABLE]
    assert "NO automatic corroboration" in result.recommendation
    assert not result.suggested_reason_code


def test_an_image_is_reported_as_unreadable_not_empty() -> None:
    got = extract(b"\x89PNG\r\n\x1a\n fake", "scan.png")
    assert got.status == "unsupported"
    assert "OCR" in got.detail
    assert got.readable is False


def test_legacy_office_formats_explain_how_to_fix_them() -> None:
    got = extract(b"\xd0\xcf\x11\xe0", "registry.doc")
    assert got.status == "unsupported"
    assert ".docx" in got.detail


def test_every_result_requires_human_confirmation() -> None:
    for text in ("", "unrelated content", "BANCO CENTRAL DE VENEZUELA Caracas"):
        result = _check(text, status="ok" if text else "empty")
        assert result.as_dict()["requires_human_confirmation"] is True


# ---------------------------------------------------------------------------
# Substantive findings
# ---------------------------------------------------------------------------
def test_a_document_naming_the_designated_party_warns_against_clearing() -> None:
    result = _check(
        "SHAREHOLDER REGISTER\nCompany: Bella Napoli Pizzeria Hamburg GmbH\n"
        "Ultimate beneficial owner: BANCO CENTRAL DE VENEZUELA\n"
        "Registered office: Caracas, Venezuela"
    )
    verdicts = {f.check: f.verdict for f in result.findings}
    assert verdicts["Designated party named in document"] == VERDICT_CONTRADICTS
    assert "Do not clear" in result.recommendation
    assert not result.suggested_reason_code


def test_a_document_about_an_unrelated_party_may_support_dismissal() -> None:
    result = _check(
        "REGISTRO MERCANTIL\nRazon social: Bella Napoli Pizzeria Hamburg GmbH\n"
        "Domicilio: Hamburg, Germany\nNumero de registro: HRB-884213"
    )
    verdicts = {f.check: f.verdict for f in result.findings}
    assert verdicts["Designated party named in document"] == VERDICT_CORROBORATES
    assert verdicts["Country comparison"] == VERDICT_CORROBORATES
    assert "may support a dismissal" in result.recommendation


def test_country_finding_states_the_third_country_caveat() -> None:
    result = _check("Registered in Hamburg, Germany. Trading since 2011.")
    country = next(f for f in result.findings if f.check == "Country comparison")
    assert "third country" in country.detail


def test_a_shared_identifier_warns_against_clearing() -> None:
    result = cross_check(
        text="Counterparty tax registration RIF-J300029222 verified on the register.",
        extraction_status="ok",
        extraction_detail="",
        partner_name="Some Partner SA",
        target_name="BANCO CENTRAL DE VENEZUELA",
        target_dob="",
        target_geography="Venezuela",
        target_watchlist_id="26727",
        target_remarks="Tax ID No. RIF-J300029222; Caracas, Venezuela",
    )
    ident = next(f for f in result.findings if f.check == "Identifier comparison")
    assert ident.verdict == VERDICT_CONTRADICTS
    # Normalisation turns "RIF-J300029222" into two tokens, so the identifier
    # recovered is the numeric part.
    assert "J300029222" in ident.detail


def test_short_numeric_references_are_deliberately_not_matched() -> None:
    """A bare 5-digit number is not an identifier.

    Matching one wherever it appears would turn postcodes, invoice totals and
    page numbers into "contradicts" findings against the designation.
    """
    result = _check("Entity reference 26727 confirmed against the register.")
    assert not [f for f in result.findings if f.check == "Identifier comparison"]


def test_dob_year_overlap_points_at_the_same_subject() -> None:
    result = cross_check(
        text="Passport issued to subject born 12/03/1975 in Caracas.",
        extraction_status="ok", extraction_detail="",
        partner_name="Some Partner", target_name="MADURO MOROS, Nicolas",
        target_dob="23 Nov 1975", target_geography="Venezuela",
        target_watchlist_id="22790", target_remarks="",
    )
    dates = next(f for f in result.findings if f.check == "Date comparison")
    assert dates.verdict == VERDICT_CONTRADICTS
    assert "1975" in dates.detail


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def test_extracts_text_from_a_real_pdf() -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.multi_cell(0, 6, "Trade licence 00123 issued Hamburg 2011",
                   new_x="LMARGIN", new_y="NEXT")
    got = extract(bytes(pdf.output()), "licence.pdf")
    assert got.status == "ok"
    assert "00123" in got.text and "Hamburg" in got.text


def test_a_pdf_with_no_text_layer_is_flagged_as_a_probable_scan() -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_fill_color(200, 200, 200)
    pdf.rect(20, 20, 100, 60, style="F")
    got = extract(bytes(pdf.output()), "scan.pdf")
    assert got.status == "empty"
    assert "scan" in got.detail.lower()


def test_extracts_text_from_xlsx_and_csv() -> None:
    import io as _io

    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Company", "Bella Napoli GmbH"])
    buf = _io.BytesIO()
    wb.save(buf)
    assert "Bella Napoli GmbH" in extract(buf.getvalue(), "x.xlsx").text
    assert "Hamburg" in extract(b"city,country\nHamburg,Germany\n", "x.csv").text
