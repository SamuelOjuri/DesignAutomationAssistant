"""Local replay of the supplied archive; its PDF reader is stubbed, no model calls."""
import json
from pathlib import Path
from types import SimpleNamespace

from backend.app.services.legacy_enquiry.source_documents import MAX_SOURCE_BYTES, extract_source_records
from backend.app.services.specification_semantics import COMPLETE_SOURCE_STATUSES


def test_supplied_zip_passes_new_size_gate_and_skips_four_dwgs():
    archive = Path('C:/Users/SamuelOjuri/Downloads/wetransfer_srp1411-bba-01-rf-d-a-2108-154roofgaplan-nb1_p03-wip-dwg_2026-11-11_1057.zip')
    calls = []
    reader = SimpleNamespace(process_pdf=lambda content, filename: calls.append(filename) or 'Local reader stub; no document facts extracted')
    records = extract_source_records(archive.read_bytes(), archive.name, reader, source_id='local:zip')
    assert MAX_SOURCE_BYTES == 40 * 1024 * 1024
    assert records[0]['status'] == 'expanded:archive'
    assert len(calls) == 1
    assert sum(record['status'] == 'skipped:dwg' for record in records) == 4
    assert sum(record['status'] == 'read' for record in records) == 1
    assert all(record['status'] in COMPLETE_SOURCE_STATUSES for record in records)
    result = {'reader_stub': True, 'model_calls': 0, 'archive_bytes': archive.stat().st_size,
              'limit_bytes': MAX_SOURCE_BYTES,
              'sources': [{key: record[key] for key in ('filename', 'status', 'sizeBytes', 'contentSha256')}
                          for record in records]}
    Path(__file__).with_name('updated_zip_result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
