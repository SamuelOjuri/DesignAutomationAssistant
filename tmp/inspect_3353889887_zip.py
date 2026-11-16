"""Read-only ZIP central-directory inventory; does not decompress members."""
from collections import Counter, defaultdict
import json
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import struct
from zipfile import ZipFile

ARCHIVE = Path(r"C:\Users\SamuelOjuri\Downloads\wetransfer_13056-albermarle-music-centre_2026-11-10_1027.zip")
ROOT = Path(__file__).resolve().parents[1]
out = ROOT / "tmp" / "wetransfer-3353889887"
out.mkdir(exist_ok=True)


class StoredMemberView:
    """Seek directly within a stored outer ZIP member without materializing it."""
    def __init__(self, source, offset, size):
        self.source, self.offset, self.size, self.position = source, offset, size, 0
    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if position < 0:
            raise ValueError("Negative ZIP view seek")
        self.position = position
        return position
    def tell(self):
        return self.position
    def read(self, size=-1):
        available = max(0, self.size - self.position)
        length = available if size < 0 else min(size, available)
        self.source.seek(self.offset + self.position)
        content = self.source.read(length)
        self.position += len(content)
        return content
    def seekable(self):
        return True


def nested_inventory(source, info):
    assert info.compress_type == 0 and not info.flag_bits & 1
    source.seek(info.header_offset)
    header = source.read(30)
    assert header[:4] == b"PK\x03\x04"
    filename_length, extra_length = struct.unpack_from("<HH", header, 26)
    start = info.header_offset + 30 + filename_length + extra_length
    with ZipFile(StoredMemberView(source, start, info.file_size)) as nested:
        inner = nested.infolist()
        files = [entry for entry in inner if not entry.is_dir()]
        by_suffix = defaultdict(lambda: {"count": 0, "compressed_bytes": 0, "expanded_bytes": 0})
        for entry in files:
            group = by_suffix[PurePosixPath(entry.filename).suffix.lower() or "[none]"]
            group["count"] += 1
            group["compressed_bytes"] += entry.compress_size
            group["expanded_bytes"] += entry.file_size
        dimensions = Counter()
        for entry in files:
            if not entry.filename.lower().endswith((".jpg", ".jpeg")):
                continue
            with nested.open(entry) as member:
                preview = member.read(256 * 1024)
            offset = 2
            size = "unknown in first 256 KiB"
            while offset + 4 < len(preview):
                if preview[offset] != 0xFF:
                    offset += 1
                    continue
                offset += 1
                while offset < len(preview) and preview[offset] == 0xFF:
                    offset += 1
                if offset >= len(preview):
                    break
                marker = preview[offset]
                offset += 1
                if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                    continue
                if offset + 2 > len(preview):
                    break
                length = int.from_bytes(preview[offset:offset + 2], "big")
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    height = int.from_bytes(preview[offset + 3:offset + 5], "big")
                    width = int.from_bytes(preview[offset + 5:offset + 7], "big")
                    size = f"{width}x{height}"
                    break
                if length < 2:
                    break
                offset += length
            dimensions[size] += 1
        return {"name": info.filename, "entries": len(inner), "files": len(files),
                "expanded_bytes": sum(entry.file_size for entry in files),
                "compressed_bytes": sum(entry.compress_size for entry in files),
                "by_suffix": dict(sorted(by_suffix.items())),
                "jpeg_dimensions_bounded_header_read": dict(dimensions),
                "encrypted": [entry.filename for entry in files if entry.flag_bits & 1],
                "nested_zip": [entry.filename for entry in files if entry.filename.lower().endswith(".zip")],
                "largest_members": [{"name": entry.filename, "bytes": entry.file_size}
                                    for entry in sorted(files, key=lambda entry: entry.file_size, reverse=True)[:10]],
                "member_names": [entry.filename for entry in files]}


archive_digest = hashlib.sha256()
with ARCHIVE.open("rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        archive_digest.update(chunk)
with ZipFile(ARCHIVE) as archive:
    entries = archive.infolist()
    records = []
    for index, entry in enumerate(entries):
        normalized = entry.filename.replace("\\", "/")
        suffix = PurePosixPath(normalized).suffix.lower() if not entry.is_dir() else "[directory]"
        records.append({"index": index, "name": entry.filename, "directory": entry.is_dir(),
                        "suffix": suffix or "[none]", "compressed_bytes": entry.compress_size,
                        "expanded_bytes": entry.file_size, "compression": entry.compress_type,
                        "encrypted": bool(entry.flag_bits & 1),
                        "unsafe_path": (PureWindowsPath(entry.filename).is_absolute()
                                        or PurePosixPath(normalized).is_absolute()
                                        or ".." in PurePosixPath(normalized).parts),
                        "symlink": stat.S_ISLNK(entry.external_attr >> 16),
                        "crc32": format(entry.CRC, "08x")})
    with ARCHIVE.open("rb") as source:
        nested_summaries = [nested_inventory(source, entry) for entry in entries
                            if entry.filename.lower().endswith(".zip")]
files = [record for record in records if not record["directory"]]
by_suffix = defaultdict(lambda: {"count": 0, "compressed_bytes": 0, "expanded_bytes": 0})
for record in files:
    group = by_suffix[record["suffix"]]
    group["count"] += 1
    group["compressed_bytes"] += record["compressed_bytes"]
    group["expanded_bytes"] += record["expanded_bytes"]
counts = Counter(record["name"] for record in records)
relevant = [record for record in files if any(token in record["name"].lower() for token in
            ("roof", "spec", "plan", "drain", "section", "thermal", "insul", "zinc", "taper", "drawing"))]
summary = {"archive": str(ARCHIVE), "archive_bytes": ARCHIVE.stat().st_size,
           "archive_sha256": archive_digest.hexdigest(),
           "entries": len(records), "files": len(files), "directories": len(records) - len(files),
           "compressed_member_bytes": sum(record["compressed_bytes"] for record in files),
           "expanded_member_bytes": sum(record["expanded_bytes"] for record in files),
           "by_suffix": dict(sorted(by_suffix.items())),
           "members_over_40MiB": [record for record in files if record["expanded_bytes"] > 40 * 1024 * 1024],
           "encrypted_members": [record["name"] for record in records if record["encrypted"]],
           "unsafe_paths": [record["name"] for record in records if record["unsafe_path"]],
           "symlinks": [record["name"] for record in records if record["symlink"]],
           "duplicate_names": {name: count for name, count in counts.items() if count > 1},
           "nested_archives": [record for record in files if record["suffix"] in {".zip", ".7z", ".rar"}],
           "nested_inventory": nested_summaries,
           "largest_members": sorted(files, key=lambda record: record["expanded_bytes"], reverse=True)[:20],
           "likely_relevant_names": [record["name"] for record in relevant],
           "all_members": records}
(out / "inventory.json").write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
print(json.dumps({key: value for key, value in summary.items() if key != "all_members"}, ensure_ascii=True, indent=2))
