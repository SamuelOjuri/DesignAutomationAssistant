"""Saved blank formula results are evidence, rather than missing calculations."""

import io
from xml.etree import ElementTree
from zipfile import ZipFile

import openpyxl
import pytest

from backend.app.services.legacy_enquiry.source_documents import (
    SourceDocumentError,
    extract_document,
)
from backend.app.services.spreadsheet_extraction import extract_spreadsheet_documents


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def formula_workbook(*, cache_type="str", cache_value=None, cache_present=True,
                     shared=False, missing_second_formula=False, omit_cell_references=False):
    """Emulate Excel's cached results without requiring an Excel process."""
    book = openpyxl.Workbook()
    book.active.title = "Request"
    book.active.append(["Roof scope", '=IF(ISNUMBER(C1),C1,"")'])
    if cache_type == "n" and cache_value == "0":
        book.active["B1"] = "=1-1"
    if shared:
        book.active.append(["Second roof", '=IF(ISNUMBER(C2),C2,"")'])
    if missing_second_formula:
        book.active["D1"] = "=1/4"
    output = io.BytesIO()
    book.save(output)
    book.close()

    with ZipFile(io.BytesIO(output.getvalue())) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    sheet = ElementTree.fromstring(entries["xl/worksheets/sheet1.xml"])
    addresses = ["B1", "B2"] if shared else ["B1"]
    for index, address in enumerate(addresses):
        cell = sheet.find(f".//s:c[@r='{address}']", NS)
        cell.set("t", cache_type)
        cache = cell.find("s:v", NS)
        if cache_present:
            if cache is None:
                cache = ElementTree.SubElement(cell, f"{{{NS['s']}}}v")
            cache.text = cache_value
        elif cache is not None:
            cell.remove(cache)
        if shared:
            formula = cell.find("s:f", NS)
            formula.set("t", "shared")
            formula.set("si", "0")
            if index == 0:
                formula.set("ref", "B1:B2")
            else:
                formula.text = None
    if omit_cell_references:
        for cell in sheet.findall(".//s:c", NS):
            cell.attrib.pop("r", None)
    entries["xl/worksheets/sheet1.xml"] = ElementTree.tostring(sheet)
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize("extension", [".xlsx", ".xlsm"])
def test_explicit_empty_string_cache_is_readable_for_search_and_design(tmp_path, extension):
    content = formula_workbook()
    path = tmp_path / ("request" + extension)
    path.write_bytes(content)

    documents = extract_spreadsheet_documents(str(path), path.name)
    assert all(doc["section"] != "spreadsheet:extraction-notice" for doc in documents)
    text = "\n".join(doc["text"] for doc in documents)
    assert "A1=Roof scope" in text
    assert "cached result unavailable" not in text
    assert "B1=" not in text
    design_text = extract_document(content, path.name, None)
    assert "A1=Roof scope" in design_text and "cached result unavailable" not in design_text
    path.unlink()  # Both paths must close their read-only Windows file handles.


@pytest.mark.parametrize("cache_type,cache_present", [
    ("str", False),  # A string formula alone is not proof of a cached blank.
    ("n", False),
    ("n", True),  # An empty numeric cache still means calculation is missing.
])
def test_genuinely_missing_formula_cache_remains_an_incomplete_source(tmp_path, cache_type, cache_present):
    content = formula_workbook(cache_type=cache_type, cache_present=cache_present)
    path = tmp_path / "uncalculated.xlsx"
    path.write_bytes(content)

    documents = extract_spreadsheet_documents(str(path), path.name)
    assert "formula results are unavailable" in documents[-1]["notice"]
    with pytest.raises(SourceDocumentError, match="formula results are unavailable") as error:
        extract_document(content, path.name, None)
    assert error.value.details["code"] == "workbook_incomplete"


@pytest.mark.parametrize("extension", [".xlsx", ".xlsm"])
def test_cached_numeric_zero_is_preserved(tmp_path, extension):
    content = formula_workbook(cache_type="n", cache_value="0")
    path = tmp_path / ("zero" + extension)
    path.write_bytes(content)

    documents = extract_spreadsheet_documents(str(path), path.name)
    assert all(doc["section"] != "spreadsheet:extraction-notice" for doc in documents)
    assert "B1=0" in "\n".join(doc["text"] for doc in documents)
    assert "B1=0" in extract_document(content, path.name, None)


def test_shared_formulas_with_cached_blanks_are_readable(tmp_path):
    content = formula_workbook(shared=True)
    path = tmp_path / "shared.xlsx"
    path.write_bytes(content)

    documents = extract_spreadsheet_documents(str(path), path.name)
    assert all(doc["section"] != "spreadsheet:extraction-notice" for doc in documents)
    text = extract_document(content, path.name, None)
    assert "A1=Roof scope" in text and "A2=Second roof" in text
    assert "cached result unavailable" not in text


def test_cached_blank_without_explicit_cell_references_is_readable(tmp_path):
    content = formula_workbook(omit_cell_references=True)
    path = tmp_path / "implicit-cell-references.xlsx"
    path.write_bytes(content)

    documents = extract_spreadsheet_documents(str(path), path.name)
    assert all(doc["section"] != "spreadsheet:extraction-notice" for doc in documents)
    text = extract_document(content, path.name, None)
    assert "A1=Roof scope" in text
    assert "cached result unavailable" not in text and "B1=" not in text


def test_cached_blank_does_not_hide_another_uncalculated_formula():
    content = formula_workbook(missing_second_formula=True)
    with pytest.raises(SourceDocumentError, match="formula results are unavailable") as error:
        extract_document(content, "mixed.xlsx", None)
    assert error.value.details["code"] == "workbook_incomplete"
