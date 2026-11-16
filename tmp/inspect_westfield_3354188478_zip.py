"""Read-only central-directory audit of the user-provided ZIP."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import ZipFile

archive_path = Path(r"C:\Users\SamuelOjuri\Downloads\Westfield_Shepherd_s_Bush_2026-11-12_TaperedView_6001.zip")
output = Path(__file__).resolve().parents[1] / "tmp" / "westfield-3354188478"
output.mkdir(exist_ok=True)
digest = hashlib.sha256()
with archive_path.open("rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)

with ZipFile(archive_path) as archive:
    entries = archive.infolist()
    files = [entry for entry in entries if not entry.is_dir()]
    records = [{"name": entry.filename, "bytes": entry.file_size, "compressed_bytes": entry.compress_size,
                "suffix": PurePosixPath(entry.filename.replace("\\", "/")).suffix.lower(),
                "encrypted": bool(entry.flag_bits & 1)} for entry in files]
    groups = defaultdict(lambda: {"count": 0, "bytes": 0})
    for record in records:
        groups[record["suffix"]]["count"] += 1
        groups[record["suffix"]]["bytes"] += record["bytes"]
    names = Counter(entry.filename for entry in entries)
    result = {
        "archive": str(archive_path), "bytes": archive_path.stat().st_size, "sha256": digest.hexdigest(),
        "entries": len(entries), "files": len(files), "expanded_bytes": sum(entry.file_size for entry in files),
        "by_suffix": dict(groups), "members_over_40MiB": [r for r in records if r["bytes"] > 40 * 1024 * 1024],
        "nested_zips": [r for r in records if r["suffix"] == ".zip"],
        "encrypted_members": [r["name"] for r in records if r["encrypted"]],
        "unsafe_paths": [entry.filename for entry in entries if PureWindowsPath(entry.filename).is_absolute()
                         or PurePosixPath(entry.filename.replace("\\", "/")).is_absolute()
                         or ".." in PurePosixPath(entry.filename.replace("\\", "/")).parts],
        "duplicate_names": {name: count for name, count in names.items() if count > 1},
        "members": records,
    }
    small_text = []
    for entry in files:
        if PurePosixPath(entry.filename).suffix.lower() in {".txt", ".xml", ".json", ".csv"} and entry.file_size < 100000:
            small_text.append({"name": entry.filename, "text": archive.read(entry).decode("utf-8", errors="replace")[:12000]})
    result["small_text"] = small_text
(output / "inventory.json").write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=True, indent=2))
