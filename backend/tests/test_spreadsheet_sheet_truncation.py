"""Accepted worksheet truncation retains bounded, explicit source evidence."""

import logging
from pathlib import Path

import openpyxl
import pytest

from backend.app.services import spreadsheet_extraction as excel


XLS_FIXTURE = Path(__file__).parent / "fixtures" / "spreadsheets" / "pricing_schedule.xls"


def make_workbook(path, visible_sheets, *, hidden_between=False, uncached_tail=False):
    book = openpyxl.Workbook()
    for index in range(1, visible_sheets + 1):
        if hidden_between:
            hidden = book.create_sheet(f"Hidden {index}")
            hidden.sheet_state = "hidden"
            hidden["A1"] = "HIDDEN OPTIONS MUST NOT BECOME EVIDENCE"
        sheet = book.active if index == 1 else book.create_sheet()
        sheet.title = f"Visible {index}"
        sheet["A1"] = f"VISIBLE CONTENT {index}"
        if uncached_tail and index == visible_sheets:
            sheet["B1"] = "=1/4"
    book.save(path)
    book.close()


def sheet_documents(documents):
    return [doc for doc in documents if doc["section"].startswith("sheet:")]


def test_exactly_forty_visible_sheets_needs_no_truncation_notice(tmp_path):
    path = tmp_path / "forty.xlsx"
    make_workbook(path, 40)
    docs = excel.extract_spreadsheet_documents(
        str(path), path.name, visible_only=True, max_sheets=40, truncate_sheets=True,
    )
    assert len(sheet_documents(docs)) == 40
    assert all(doc["section"].startswith("sheet:") for doc in docs)
    assert "A1=VISIBLE CONTENT 40" in docs[-1]["text"]
    path.unlink()


@pytest.mark.parametrize("visible_sheets", [41, 45])
def test_excess_sheets_keep_first_forty_and_log_informational_notice(tmp_path, caplog, visible_sheets):
    path = tmp_path / "many.xlsx"
    make_workbook(path, visible_sheets)
    with caplog.at_level(logging.INFO, logger=excel.__name__):
        docs = excel.extract_spreadsheet_documents(
            str(path), path.name, visible_only=True, max_sheets=40, truncate_sheets=True,
        )
    sheets = sheet_documents(docs)
    assert len(sheets) == 40
    assert [doc["section"] for doc in sheets] == [
        f"sheet:Visible {index}:rows:1-1" for index in range(1, 41)
    ]
    assert all("A1=VISIBLE CONTENT 41" not in doc["text"] for doc in sheets)
    notices = [doc for doc in docs if doc["section"] == "spreadsheet:worksheet-limit"]
    assert len(notices) == 1
    assert "40" in notices[0]["text"] and "omit" in notices[0]["text"].lower()
    assert not any(doc["section"] == "spreadsheet:extraction-notice" for doc in docs)
    records = [record for record in caplog.records if record.name == excel.__name__]
    assert any(record.levelno == logging.INFO for record in records)
    assert all(record.levelno < logging.WARNING for record in records)
    path.unlink()


def test_hidden_tabs_do_not_consume_the_forty_visible_sheet_allowance(tmp_path):
    path = tmp_path / "hidden.xlsx"
    make_workbook(path, 41, hidden_between=True)
    docs = excel.extract_spreadsheet_documents(
        str(path), path.name, visible_only=True, max_sheets=40, truncate_sheets=True,
    )
    sheets = sheet_documents(docs)
    assert len(sheets) == 40
    assert "A1=VISIBLE CONTENT 40" in sheets[-1]["text"]
    assert all("HIDDEN OPTIONS" not in doc["text"] for doc in docs)
    assert not any(doc["section"] == "spreadsheet:extraction-notice" for doc in docs)
    path.unlink()


def test_omitted_sheet_is_not_scanned_for_uncached_formulas(tmp_path):
    path = tmp_path / "unread-tail.xlsx"
    make_workbook(path, 41, uncached_tail=True)
    docs = excel.extract_spreadsheet_documents(
        str(path), path.name, visible_only=True, max_sheets=40, truncate_sheets=True,
    )
    assert len(sheet_documents(docs)) == 40
    assert any(doc["section"] == "spreadsheet:worksheet-limit" for doc in docs)
    assert not any(doc["section"] == "spreadsheet:extraction-notice" for doc in docs)
    assert all("formula results are unavailable" not in doc["text"] for doc in docs)
    path.unlink()


def test_truncation_does_not_suppress_uncached_formulas_in_included_sheet(tmp_path):
    path = tmp_path / "uncached.xlsx"
    make_workbook(path, 1, uncached_tail=True)
    docs = excel.extract_spreadsheet_documents(
        str(path), path.name, visible_only=True, max_sheets=40, truncate_sheets=True,
    )
    assert any(
        doc["section"] == "spreadsheet:extraction-notice"
        and "formula results are unavailable" in doc["notice"]
        for doc in docs
    )
    path.unlink()


@pytest.mark.parametrize("limit,value", [("MAX_ROWS", 1), ("MAX_CELLS", 1)])
def test_truncation_does_not_suppress_row_or_cell_limits(tmp_path, monkeypatch, limit, value):
    path = tmp_path / "bounded.xlsx"
    make_workbook(path, 41)
    monkeypatch.setattr(excel, limit, value)
    docs = excel.extract_spreadsheet_documents(
        str(path), path.name, visible_only=True, max_sheets=40, truncate_sheets=True,
    )
    assert any(
        doc["section"] == "spreadsheet:extraction-notice"
        and "row/cell scan limit" in doc["notice"]
        for doc in docs
    )
    path.unlink()


def test_sheet_truncation_does_not_hide_pending_text_beyond_document_budget(tmp_path, monkeypatch):
    path = tmp_path / "pending-text.xlsx"
    book = openpyxl.Workbook()
    book.active["A1"] = "Roof specification " * 80
    book.create_sheet("Outside the included scope")["A1"] = "Omitted sheet"
    book.save(path)
    book.close()
    # The included cell needs two chunks. The first chunk consumes the only
    # document slot reserved for source text, leaving a pending final chunk when
    # the next worksheet triggers the authorized sheet cutoff.
    monkeypatch.setattr(excel, "MAX_DOCUMENTS", 2)
    docs = excel.extract_spreadsheet_documents(
        str(path), path.name, max_sheets=1, truncate_sheets=True, preserve_full_cells=True,
    )
    assert any(
        doc["section"] == "spreadsheet:extraction-notice"
        and "searchable text limit" in doc["notice"]
        for doc in docs
    )
    assert not any(doc["section"] == "spreadsheet:worksheet-limit" for doc in docs)
    assert len(docs) == 2
    path.unlink()


@pytest.mark.parametrize("extension", [".xls", ".xlsx"])
def test_sheet_truncation_is_opt_in_and_supported_for_both_excel_readers(tmp_path, extension):
    path = tmp_path / ("scoped" + extension)
    if extension == ".xls":
        path.write_bytes(XLS_FIXTURE.read_bytes())
    else:
        make_workbook(path, 2)
    default = excel.extract_spreadsheet_documents(str(path), path.name, max_sheets=1)
    assert any(
        doc["section"] == "spreadsheet:extraction-notice"
        and "worksheet limit" in doc["notice"]
        for doc in default
    )
    truncated = excel.extract_spreadsheet_documents(
        str(path), path.name, max_sheets=1, truncate_sheets=True,
    )
    assert len({doc["section"].split(":")[1] for doc in sheet_documents(truncated)}) == 1
    assert any(doc["section"] == "spreadsheet:worksheet-limit" for doc in truncated)
    assert not any(doc["section"] == "spreadsheet:extraction-notice" for doc in truncated)
    path.unlink()
