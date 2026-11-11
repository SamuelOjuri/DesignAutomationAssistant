"""Isolated preview of user attachments; never connects to Monday or a database."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.config import settings
from backend.app.services.design_processing_inputs import DesignEmailAsset, DownloadedDesignEmailAsset
from backend.app.services.design_processing_pipeline import _extraction_payload
from backend.app.services.design_processing_state import ProcessingIdentity
from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient, _default_generate_content
from backend.app.services.legacy_enquiry.source_documents import extract_source_records, MAX_SOURCE_BYTES

WORK = Path(__file__).resolve().parent
DOWNLOADS = Path('C:/Users/SamuelOjuri/Downloads')
EMAIL = DOWNLOADS / '20261111-115947-bb164505116e4fc9b738c257c3bd9e20.eml'
ARCHIVE = DOWNLOADS / 'wetransfer_srp1411-bba-01-rf-d-a-2108-154roofgaplan-nb1_p03-wip-dwg_2026-11-11_1057.zip'


def write_json(name, value):
    (WORK / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def asset(path, identity):
    content = path.read_bytes()
    source = DesignEmailAsset(str(identity), path.name, path.suffix, len(content),
                             '2026-11-11T12:00:00Z', 'local-preview', False)
    return DownloadedDesignEmailAsset(source, str(path), '', hashlib.sha256(content).hexdigest(), len(content))


def logged_generate(model, contents, config):
    # Cache exact provider outputs locally for audit; never print source text or credentials.
    sequence = len(list(WORK.glob('model_response_*.json'))) + 1
    started = time.monotonic()
    print(f'Model request {sequence} started: {model}', flush=True)
    response = _default_generate_content(model, contents, config)
    write_json(f'model_response_{sequence:02d}.json', response.model_dump(mode='json'))
    print(f'Model request {sequence} complete in {time.monotonic()-started:.1f}s', flush=True)
    return response


def main():
    # Actual input result is reproducible without any model call.
    class UnusedExtractor:
        def __getattr__(self, name):
            raise AssertionError('Oversized ZIP must fail before invoking a text model')

    original = extract_source_records(ARCHIVE.read_bytes(), ARCHIVE.name, UnusedExtractor(), source_id='asset:2')
    write_json('original_zip_result.json', {'archive_bytes': ARCHIVE.stat().st_size,
               'limit_bytes': MAX_SOURCE_BYTES, 'manifest': original})
    print(json.dumps({'original_zip_bytes': ARCHIVE.stat().st_size,
                      'limit_bytes': MAX_SOURCE_BYTES,
                      'original_manifest': [{key: record[key] for key in ('status', 'error') if key in record}
                                            for record in original]}), flush=True)
    if '--original-only' in sys.argv:
        return

    # Diagnostic supported alternative: the same email plus its supplied PDF separately.
    with ZipFile(ARCHIVE) as archive:
        pdfs = [entry for entry in archive.infolist() if entry.filename.lower().endswith('.pdf')]
        assert len(pdfs) == 1
        pdf_path = WORK / 'diagnostic_input' / Path(pdfs[0].filename).name
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(archive.read(pdfs[0]))
    write_json('diagnostic_inputs.json', {'scenario': 'PDF extracted from original oversized ZIP and supplied separately',
               'model': settings.design_processing_extraction_model,
               'thinking_level': settings.design_processing_thinking_level,
               'pipeline_version': settings.design_processing_pipeline_version,
               'paths': [str(EMAIL), str(pdf_path)]})
    client = LegacyGeminiClient(settings.design_processing_extraction_model,
                               thinking_level=settings.design_processing_thinking_level,
                               generate_content=logged_generate)
    try:
        result = analyze_downloaded_email_assets([asset(EMAIL, 1), asset(pdf_path, 2)], client=client)
    except Exception as exc:
        details = {'exception_type': type(exc).__name__, 'message': str(exc),
                   'manifest': getattr(exc, 'manifest', None),
                   'evidence_failure': getattr(exc, 'evidence_failure', None)}
        write_json('diagnostic_error.json', details)
        print(json.dumps({'diagnostic_error_type': type(exc).__name__, 'message': str(exc)[:1400]}), flush=True)
        raise
    write_json('analysis_result.json', asdict(result))
    payload = _extraction_payload(result, ProcessingIdentity('local-attachment-preview', settings.design_processing_pipeline_version))
    write_json('extraction_payload.json', payload)
    print(json.dumps({'parameters': result.performance_parameters, 'intent': result.specification_request_intent,
                      'source_statuses': [{'filename': entry['filename'], 'status': entry['status']} for entry in result.source_manifest]},
                     ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
