"""Upload parsing tests.

Covers the two real input shapes: the live Delivery Hero export (semicolon
delimited, quoted, `Legal Name`) and the shape named in the specification
(comma delimited, `Entity_Name` / `UBO_Name` / `Tax_ID`).
"""

from __future__ import annotations

import pytest

from app.screening import parse_upload

DH_EXPORT = (
    '"GRID";"Type";"Account Country";"Account Status";"Created Date";"Legal Name"\r\n'
    '"4OYHPJ";"Group";"Bahrain";"Active";"07/12/2017";"Acme Trading W.L.L."\r\n'
    '"45KJ1A";"Brand";"United Arab Emirates";"Active";"28/04/2020";""\r\n'
    '"4C3Z8M";"Group";"Singapore";"Active";"27/07/2022";"Example Foods Pte Ltd"\r\n'
).encode("utf-8")

SPEC_FORMAT = (
    "Entity_Name,UBO_Name,Country,Tax_ID\n"
    "Acme Trading LLC,Jane Roe,Germany,DE123456789\n"
    "Beta Foods SA,,Spain,ESB12345678\n"
).encode("utf-8")


def test_parses_live_dh_export() -> None:
    parsed = parse_upload(DH_EXPORT)

    assert parsed.delimiter == ";"
    assert parsed.total_rows == 3
    # The row with an empty Legal Name carries no screenable name.
    assert parsed.skipped_rows == 1
    assert len(parsed.records) == 2

    first = parsed.records[0]
    assert first.grid == "4OYHPJ"
    assert first.entity_name == "Acme Trading W.L.L."
    assert first.country == "Bahrain"
    assert first.account_status == "Active"


def test_parses_specification_format_including_ubo() -> None:
    parsed = parse_upload(SPEC_FORMAT)

    assert parsed.delimiter == ","
    assert len(parsed.records) == 2
    assert parsed.records[0].ubo_name == "Jane Roe"
    assert parsed.records[0].tax_id == "DE123456789"
    assert parsed.records[1].ubo_name == ""


def test_column_matching_is_insensitive_to_case_and_punctuation() -> None:
    payload = b"partner legal name,ACCOUNT_COUNTRY\nAcme Trading,Germany\n"
    parsed = parse_upload(payload)
    assert parsed.records[0].entity_name == "Acme Trading"
    assert parsed.records[0].country == "Germany"


def test_missing_name_column_is_rejected_with_a_useful_message() -> None:
    # Two low-cardinality, non-name columns: nothing here looks like a name.
    payload = (
        b"GRID,Account Country\n"
        + b"".join(f"G{i},Bahrain\n".encode() for i in range(20))
    )
    with pytest.raises(ValueError, match="column holding partner names"):
        parse_upload(payload)


def test_max_rows_bounds_the_validation_pass() -> None:
    rows = b"Entity_Name,Country\n" + b"".join(
        f"Partner {i},Germany\n".encode() for i in range(500)
    )
    parsed = parse_upload(rows, max_rows=10)
    assert parsed.total_rows == 10
    assert len(parsed.records) == 10


def test_a_ubo_only_row_is_still_screened() -> None:
    payload = b"Entity_Name,UBO_Name,Country\n,Jane Roe,Germany\n"
    parsed = parse_upload(payload)
    assert len(parsed.records) == 1
    assert parsed.records[0].ubo_name == "Jane Roe"


def test_blank_named_prefix_does_not_break_the_row_cap() -> None:
    """Real exports carry long runs of empty Legal Name rows.

    The cap must count every data row, not only screenable ones — otherwise
    validation reads the entire file instead of a bounded prefix.
    """
    payload = b'"GRID";"Legal Name"\n' + b"".join(
        f'"G{i}";""\n'.encode() for i in range(5000)
    )
    parsed = parse_upload(payload, max_rows=100)
    assert parsed.total_rows == 100
    assert parsed.records == []
    assert parsed.skipped_rows == 100


# ---------------------------------------------------------------------------
# Header row may not be row 1
# ---------------------------------------------------------------------------
def test_header_is_found_below_a_banner_row() -> None:
    """The UK list opens with "Report Date:"; internal workbooks use titles."""
    payload = (
        "Report Date: 16-Jun-2026\n"
        "GRID,Legal Name,Account Country\n"
        "4OYHPJ,Acme Trading LLC,Bahrain\n"
        "45KJ1A,Beta Foods SA,Spain\n"
    ).encode()
    parsed = parse_upload(payload)
    assert parsed.header_row == 2
    assert len(parsed.records) == 2
    assert parsed.records[0].entity_name == "Acme Trading LLC"
    assert parsed.records[0].country == "Bahrain"


def test_header_found_below_a_merged_title_and_blank_row() -> None:
    payload = (
        "DH Group - Partner Screening Extract,,\n"
        ",,\n"
        "GRID,Partner Legal Name,Account Country\n"
        "G1,Gamma Restaurants Ltd,Germany\n"
    ).encode()
    parsed = parse_upload(payload)
    assert parsed.header_row == 3
    assert parsed.records[0].entity_name == "Gamma Restaurants Ltd"


# ---------------------------------------------------------------------------
# Name column identified from content, not header
# ---------------------------------------------------------------------------
def test_name_column_detected_when_header_is_unrecognised() -> None:
    """A Spanish-language export with no header we know."""
    rows = [
        "Codigo;Razon Social;Pais;Alta",
        *[
            f"V{i:05d};Restaurante Buen Sabor {i} SL;Spain;01/02/2024"
            for i in range(40)
        ],
    ]
    parsed = parse_upload("\n".join(rows).encode())
    assert parsed.records, "content detection should have found the name column"
    assert parsed.records[0].entity_name.startswith("Restaurante Buen Sabor")
    assert "detected by content" in parsed.detection_note
    assert "Razon Social" in parsed.detection_note


def test_content_detection_prefers_names_over_ids_dates_and_countries() -> None:
    rows = ["A;B;C;D"]
    for i in range(60):
        # A: short code  B: country (low cardinality)  C: date  D: the name
        rows.append(f"AB{i:04d};Malaysia;12/03/2024;Nasi Kandar Pelita {i} Sdn Bhd")
    parsed = parse_upload("\n".join(rows).encode())
    assert parsed.records[0].entity_name.startswith("Nasi Kandar Pelita")


def test_known_header_wins_over_content_heuristic() -> None:
    rows = ["Notes;Legal Name"]
    for i in range(30):
        rows.append(f"Some long free text remark number {i};Acme {i} LLC")
    parsed = parse_upload("\n".join(rows).encode())
    assert parsed.records[0].entity_name == "Acme 0 LLC"
    assert parsed.detection_note == ""  # matched by header, no heuristic needed


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def _xlsx(rows: list[list[object]]) -> bytes:
    import io as _io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_reads_an_excel_workbook() -> None:
    payload = _xlsx(
        [
            ["GRID", "Legal Name", "Account Country", "Created Date"],
            ["4OYHPJ", "Acme Trading W.L.L.", "Bahrain", "2017-12-07"],
            ["45KJ1A", "Example Foods Pte Ltd", "Singapore", "2020-04-28"],
        ]
    )
    parsed = parse_upload(payload, filename="partners.xlsx")
    assert parsed.delimiter == "xlsx"
    assert len(parsed.records) == 2
    assert parsed.records[0].entity_name == "Acme Trading W.L.L."
    assert parsed.records[1].country == "Singapore"


def test_excel_detected_from_content_when_filename_lies() -> None:
    payload = _xlsx([["Legal Name"], ["Acme Trading LLC"]])
    parsed = parse_upload(payload, filename="export.csv")
    assert parsed.records[0].entity_name == "Acme Trading LLC"


def test_excel_numeric_and_date_cells_render_as_people_expect() -> None:
    import datetime as _dt

    payload = _xlsx(
        [
            ["GRID", "Legal Name", "Created Date"],
            [4712, "Acme Trading LLC", _dt.datetime(2024, 3, 12)],
        ]
    )
    parsed = parse_upload(payload, filename="p.xlsx")
    assert parsed.records[0].grid == "4712"          # not "4712.0"
    assert parsed.records[0].created_date == "2024-03-12"


def test_legacy_xls_is_rejected_with_instructions() -> None:
    with pytest.raises(ValueError, match="Save As"):
        parse_upload(b"\xd0\xcf\x11\xe0anything", filename="old.xls")


def test_an_id_column_is_never_mistaken_for_a_name_column() -> None:
    """Identifiers are highly distinct — distinctness alone must not win.

    Screening account codes against sanctions lists yields pure noise, so with
    no real name column present the parser must refuse rather than guess.
    """
    rows = ["Codigo;Pais"]
    for i in range(40):
        rows.append(f"V{i:05d};Spain")
    with pytest.raises(ValueError, match="column holding partner names"):
        parse_upload("\n".join(rows).encode())


def test_the_data_sheet_is_chosen_over_a_summary_sheet() -> None:
    """Workbooks often lead with a pivot; the records may be on sheet 2."""
    import io as _io

    from openpyxl import Workbook

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary.append(["COUNTUNIQUE of GRID", "Source"])
    summary.append(["Region", "EU Sanctions", "OFAC SDN"])
    summary.append(["APAC", 16, 51])
    summary.append(["Europe", 2, 22])

    detail = wb.create_sheet("Matches")
    detail.append(["Restaurant Information", None, None])
    detail.append(["GRID", "Partner Legal Name", "Account Country"])
    detail.append(["4OYHPJ", "Acme Trading WLL", "Bahrain"])
    detail.append(["45KJ1A", "Beta Foods SA", "Spain"])

    buf = _io.BytesIO()
    wb.save(buf)

    parsed = parse_upload(buf.getvalue(), filename="workbook.xlsx")
    assert parsed.header_row == 2
    assert len(parsed.records) == 2
    assert parsed.records[0].entity_name == "Acme Trading WLL"
    assert parsed.records[0].country == "Bahrain"
