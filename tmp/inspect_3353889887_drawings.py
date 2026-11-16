"""Read the two small PDF members only; leave nested photo archives compressed."""
import json
from pathlib import Path
import shutil
import subprocess
from zipfile import ZipFile

archive_path = Path(r"C:\Users\SamuelOjuri\Downloads\wetransfer_13056-albermarle-music-centre_2026-11-10_1027.zip")
output = Path(__file__).resolve().parents[1] / "tmp" / "pdfs" / "3353889887"
output.mkdir(parents=True, exist_ok=True)
pdfinfo, pdftotext, pdftoppm = [shutil.which(name) for name in ("pdfinfo", "pdftotext", "pdftoppm")]
results = []
with ZipFile(archive_path) as archive:
    for index, entry in enumerate(archive.infolist()):
        if entry.is_dir() or not entry.filename.lower().endswith(".pdf"):
            continue
        assert entry.file_size < 4 * 1024 * 1024
        path = output / Path(entry.filename).name
        path.write_bytes(archive.read(entry))
        record = {"filename": path.name, "bytes": path.stat().st_size}
        if pdfinfo:
            info = subprocess.run([pdfinfo, str(path)], check=True, capture_output=True).stdout.decode(errors="replace")
            record["info"] = "\n".join(line for line in info.splitlines()
                                       if line.startswith(("Pages:", "Encrypted:", "Page size:", "PDF version:")))
        if pdftotext:
            text_path = path.with_suffix(".txt")
            subprocess.run([pdftotext, "-layout", str(path), str(text_path)], check=True, capture_output=True)
            text = text_path.read_text(encoding="utf-8")
            record.update(text_chars=len(text), text_excerpt=text[:3500])
        if pdftoppm:
            prefix = output / f"drawing-{index}"
            subprocess.run([pdftoppm, "-f", "1", "-singlefile", "-scale-to", "1800", "-png",
                            str(path), str(prefix)], check=True, capture_output=True)
            record["preview"] = str(prefix.with_suffix(".png"))
        results.append(record)
print(json.dumps(results, ensure_ascii=True, indent=2))
