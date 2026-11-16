"""Unsupported mesh members stay compressed while useful archive files proceed."""
import hashlib
import io
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from backend.app.services.legacy_enquiry import source_documents as sources
from backend.app.services.specification_semantics import ARCHIVE_MEMBER_REVIEW_STATUS, COMPLETE_SOURCE_STATUSES


def archive_bytes(entries):
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize("suffix", ["obj", "OBJ", "mtl"])
def test_mesh_stays_unopened_and_cannot_consume_expansion_work(monkeypatch, suffix):
    content = archive_bytes([("roof_scan." + suffix, b"UNREAD GEOMETRY\n" * 1000),
                             ("project_summary.txt", b"Target U-value: 0.15")])
    monkeypatch.setattr(sources, "MAX_ARCHIVE_EXPANDED_BYTES", 100)
    original_open = ZipFile.open
    opened = []

    def guarded_open(archive, entry, *args, **kwargs):
        name = entry.filename if hasattr(entry, "filename") else entry
        assert not name.lower().endswith((".obj", ".mtl")), "Mesh bytes must never be decompressed"
        opened.append(name)
        return original_open(archive, entry, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "open", guarded_open)
    progress = []
    records = sources.extract_source_records(content, "survey.zip", None, source_id="asset:1", on_source=progress.append)
    root, mesh, summary = records
    assert root["status"] == "expanded:archive" and summary["status"] == "read"
    assert summary["text"] == "Target U-value: 0.15" and opened == ["project_summary.txt"]
    assert mesh["status"] == ARCHIVE_MEMBER_REVIEW_STATUS and mesh["text"] == ""
    assert mesh["contentSha256"] is None and mesh["archiveMemberSizeVerified"] is False
    assert mesh["parentSourceId"] == root["sourceId"] and mesh["archiveMemberIndex"] == 0
    assert mesh["archiveContentSha256"] == hashlib.sha256(content).hexdigest()
    with ZipFile(io.BytesIO(content)) as archive:
        entry = archive.infolist()[0]
        assert mesh["archiveMemberCrc32"] == f"{entry.CRC:08x}"
        assert mesh["sizeBytes"] == entry.file_size
    assert all(record["status"] in COMPLETE_SOURCE_STATUSES for record in records)
    assert any(record.get("status") == ARCHIVE_MEMBER_REVIEW_STATUS for record in progress)


def test_other_unsupported_formats_still_use_the_expansion_budget(monkeypatch):
    content = archive_bytes([("export.bin", b"x" * 200), ("summary.txt", b"Roof")])
    monkeypatch.setattr(sources, "MAX_ARCHIVE_EXPANDED_BYTES", 100)
    record, = sources.extract_source_records(content, "survey.zip", None, source_id="asset:1")
    assert record["error"]["code"] == "archive_expansion_limit"


def test_mesh_does_not_hide_supported_document_expansion_limit(monkeypatch):
    content = archive_bytes([("roof.obj", b"x" * 1000), ("summary.txt", b"y" * 200)])
    monkeypatch.setattr(sources, "MAX_ARCHIVE_EXPANDED_BYTES", 100)
    record, = sources.extract_source_records(content, "survey.zip", None, source_id="asset:1")
    assert record["error"]["code"] == "archive_expansion_limit"


def test_encrypted_mesh_is_not_silently_referred_as_readable_archive():
    content = bytearray(archive_bytes([("roof.obj", b"geometry")]))
    content[6] |= 1
    content[content.index(b"PK\x01\x02") + 8] |= 1
    record, = sources.extract_source_records(bytes(content), "survey.zip", None, source_id="asset:1")
    assert record["status"] == "failed:SourceDocumentError" and record["error"]["code"] == "encrypted_archive"


def test_mesh_still_counts_toward_archive_member_limit(monkeypatch):
    content = archive_bytes([("roof.obj", b"geometry"), ("roof.mtl", b"material")])
    monkeypatch.setattr(sources, "MAX_ARCHIVE_MEMBERS", 1)
    record, = sources.extract_source_records(content, "survey.zip", None, source_id="asset:1")
    assert record["error"]["code"] == "archive_member_limit"


def test_duplicate_mesh_names_keep_distinct_metadata_identities():
    with pytest.warns(UserWarning, match="Duplicate name"):
        content = archive_bytes([("roof.obj", b"first"), ("roof.obj", b"second")])
    records = sources.extract_source_records(content, "survey.zip", None, source_id="asset:1")
    assert records[1]["sourceId"] != records[2]["sourceId"]
    assert records[1]["archiveContentSha256"] == records[2]["archiveContentSha256"]
    assert records[1]["archiveMemberCrc32"] != records[2]["archiveMemberCrc32"]
