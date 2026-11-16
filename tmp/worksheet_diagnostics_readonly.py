"""Read-only workbook XML diagnostics and repository parser reproduction."""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.services.spreadsheet_extraction import extract_spreadsheet_documents
from backend.app.services.legacy_enquiry.source_documents import extract_document

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
FILES = [Path(r"C:\Users\SamuelOjuri\Downloads\3343973241 - Worksheet.xlsm"),
         Path(r"C:\Users\SamuelOjuri\Downloads\3342464181 - worksheet.xlsx")]

def read_book(path):
    result = {"file": str(path), "bytes": path.stat().st_size}
    with ZipFile(path) as archive:
        result["expanded_bytes"] = sum(e.file_size for e in archive.infolist())
        wb = ET.fromstring(archive.read("xl/workbook.xml"))
        calc = wb.find("m:calcPr", NS)
        result["calculation_properties"] = calc.attrib if calc is not None else None
        relations = {e.attrib["Id"]: e.attrib["Target"] for e in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(x.itertext()) for x in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        result["vba"] = "xl/vbaProject.bin" in archive.namelist()
        result["external_links"] = [n for n in archive.namelist() if n.startswith("xl/externalLinks/") and not "/_rels/" in n]
        sheets = []
        for index, sh in enumerate(wb.find("m:sheets", NS)):
            target = relations[sh.attrib[REL]]
            target = target.lstrip("/") if target.startswith("/") else "xl/" + target
            tree = ET.fromstring(archive.read(target))
            cell_nodes = tree.findall(".//m:sheetData/m:row/m:c", NS)
            data = {}
            formulas, missing, errors = [], [], []
            counts = collections.Counter()
            for cell in cell_nodes:
                addr, typ = cell.attrib["r"], cell.attrib.get("t", "n")
                f, v = cell.find("m:f", NS), cell.find("m:v", NS)
                value = None if v is None else v.text
                if typ == "s" and value is not None:
                    value = strings[int(value)]
                elif typ == "inlineStr":
                    value = "".join(cell.find("m:is", NS).itertext())
                if value is not None or f is not None:
                    data[addr] = {"value": value, "type": typ}
                if typ == "e":
                    errors.append({"cell": addr, "value": value})
                if f is not None:
                    item = {"cell": addr, "formula": f.text, "type": typ, "cache": value,
                            "v_exists": v is not None, "formula_attrs": f.attrib}
                    formulas.append(item)
                    counts["all"] += 1
                    if value is None:
                        missing.append(item)
                        counts["missing_" + typ] += 1
            sheet = {"index": index + 1, "name": sh.attrib["name"], "state": sh.attrib.get("state", "visible"),
                     "dimension": (tree.find("m:dimension", NS).attrib if tree.find("m:dimension", NS) is not None else None),
                     "xml_rows": len(tree.findall(".//m:sheetData/m:row", NS)), "xml_cells": len(cell_nodes),
                     "populated_cells": len(data), "formula_counts": dict(counts), "missing_formula_samples": missing[:30],
                     "error_count": len(errors), "error_samples": errors[:15], "first_values": dict(list(data.items())[:12])}
            if missing:
                relevant_rows = set("".join(filter(str.isdigit, x["cell"])) for x in missing[:30])
                sheet["missing_rows_context"] = {a: v for a, v in data.items() if "".join(filter(str.isdigit, a)) in relevant_rows}
            sheets.append(sheet)
        result["sheet_count"] = len(sheets)
        result["visible_sheet_count"] = sum(s["state"] == "visible" for s in sheets)
        result["sheets"] = sheets
    try:
        text = extract_document(path.read_bytes(), path.name, object())
        result["actual_design_extract"] = {"ok": True, "chars": len(text)}
    except Exception as exc:
        result["actual_design_extract"] = {"ok": False, "type": type(exc).__name__, "message": str(exc), "details": getattr(exc, "details", None)}
    for limit in [40, 100]:
        docs = extract_spreadsheet_documents(str(path), path.name, visible_only=True, preserve_full_cells=True,
                                             max_file_bytes=40*1024*1024, max_sheets=limit)
        result[f"extract_limit_{limit}"] = {"documents": len(docs), "chars": sum(len(d["text"]) for d in docs),
                                               "notices": [d.get("notice") for d in docs if d.get("notice")],
                                               "last_sections": [d["section"] for d in docs[-5:]]}
    return result

results = [read_book(p) for p in FILES]
output = ROOT / "tmp" / "worksheet_diagnostics_readonly.json"
output.write_text(json.dumps(results, indent=2), encoding="utf-8")
for result in results:
    print(json.dumps({k: v for k, v in result.items() if k != "sheets"}, indent=2))
    for sheet in result["sheets"]:
        print(json.dumps({k: v for k, v in sheet.items() if k not in {"missing_rows_context", "first_values", "missing_formula_samples"}}))
        if sheet["missing_formula_samples"]:
            print(json.dumps({"sheet": sheet["name"], "missing_samples": sheet["missing_formula_samples"][:8]}))
