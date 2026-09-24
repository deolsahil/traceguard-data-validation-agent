"""
Read data-model spreadsheets attached to Jira tickets.

Stakeholders often attach the table's data model as a CSV or Excel file and let
the ticket just say "create table X, see attached data model". This module turns
those files into a plain list of {column_name, data_type, description} so the
agent can validate every column without the ticket spelling them out.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

# Header cells that identify the column-name and data-type columns of the sheet.
# Ordered most-specific first so "column_name" wins over a bare "name".
_NAME_HEADERS = (
    "column_name", "column name", "columnname", "field_name", "field name",
    "attribute_name", "attribute name", "column", "field", "attribute", "name",
)
_TYPE_HEADERS = (
    "data_type", "data type", "datatype", "column_type", "column type",
    "field_type", "field type", "type",
)
_DESC_HEADERS = ("description", "comment", "comments", "definition", "notes")

# Spreadsheet types we can parse. Anything else (PDF, PNG, DOCX) is skipped.
_CSV_EXT = (".csv", ".tsv", ".txt")
_XLSX_EXT = (".xlsx", ".xlsm")

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_parseable(filename: str) -> bool:
    """True if this attachment is a spreadsheet we know how to read."""
    lower = (filename or "").lower()
    return lower.endswith(_CSV_EXT + _XLSX_EXT)


def _match_header(cell: str, candidates: tuple[str, ...]) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", (cell or "").strip().lower()).strip("_")
    return any(normalized == re.sub(r"[^a-z0-9]+", "_", c).strip("_") for c in candidates)


def _find_header_row(rows: list[list[str]]) -> tuple[int, int, int | None, int | None] | None:
    """
    Locate the header row and the indexes of the name/type/description columns.

    Sheets often start with a title or blank rows, so scan the first 20 rows
    rather than assuming row 0. Returns None if no column-name header is found.
    """
    for row_idx, row in enumerate(rows[:20]):
        name_idx = type_idx = desc_idx = None
        for col_idx, cell in enumerate(row):
            if name_idx is None and _match_header(cell, _NAME_HEADERS):
                name_idx = col_idx
            elif type_idx is None and _match_header(cell, _TYPE_HEADERS):
                type_idx = col_idx
            elif desc_idx is None and _match_header(cell, _DESC_HEADERS):
                desc_idx = col_idx
        if name_idx is not None:
            return row_idx, name_idx, type_idx, desc_idx
    return None


def _rows_to_model(rows: list[list[str]]) -> list[dict[str, str]]:
    header = _find_header_row(rows)
    if not header:
        return []
    header_row, name_idx, type_idx, desc_idx = header

    def cell(row: list[str], idx: int | None) -> str:
        return row[idx].strip() if idx is not None and idx < len(row) and row[idx] else ""

    columns: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows[header_row + 1:]:
        name = cell(row, name_idx)
        # Stop-words for footer rows; skip anything that isn't a valid identifier
        if not name or not _IDENT_RE.match(name) or name.lower() in seen:
            continue
        seen.add(name.lower())
        columns.append({
            "column_name": name,
            "data_type": cell(row, type_idx).upper(),
            "description": cell(row, desc_idx),
        })
    return columns


def _parse_csv(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # single-column or ambiguous — comma is a safe default
    return [[(c or "") for c in row] for row in csv.reader(io.StringIO(text), dialect)]


def _parse_xlsx(data: bytes) -> list[list[str]]:
    from openpyxl import load_workbook  # imported lazily — only needed for Excel

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rows: list[list[str]] = []
    for sheet in workbook.worksheets:
        sheet_rows = [
            ["" if cell is None else str(cell) for cell in row]
            for row in sheet.iter_rows(max_row=500, values_only=True)
        ]
        # Use the first sheet that actually looks like a data model
        if _find_header_row(sheet_rows):
            workbook.close()
            return sheet_rows
        if not rows:
            rows = sheet_rows
    workbook.close()
    return rows


def parse_data_model(filename: str, data: bytes) -> list[dict[str, str]]:
    """
    Extract [{column_name, data_type, description}] from a spreadsheet.

    Returns [] for unreadable or non-data-model files — callers fall back to
    whatever the ticket text says rather than failing the run.
    """
    if not data or not is_parseable(filename):
        return []
    try:
        lower = filename.lower()
        rows = _parse_xlsx(data) if lower.endswith(_XLSX_EXT) else _parse_csv(data)
        return _rows_to_model(rows)
    except Exception as error:
        # A spreadsheet we could not read is different from one with no data model
        # in it. Both yield no columns, but only this one is a problem worth seeing.
        print(f"  [attachment] could not parse {filename}: {error}")
        return []


def collect_data_model(ticket: dict[str, Any], reader: Any) -> dict[str, Any] | None:
    """
    Download and parse the first attachment that yields a usable data model.

    Returns {'filename', 'columns': [...]} or None if the ticket has no readable
    data-model attachment.
    """
    for attachment in ticket.get("attachments") or []:
        filename = attachment.get("filename") or ""
        if not is_parseable(filename):
            continue
        data = reader.download_attachment(attachment["content_url"])
        if not data:
            continue
        columns = parse_data_model(filename, data)
        if columns:
            return {"filename": filename, "columns": columns}
    return None
