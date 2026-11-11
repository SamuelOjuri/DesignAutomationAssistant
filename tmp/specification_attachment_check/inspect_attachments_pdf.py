from pathlib import Path
from zipfile import ZipFile
import json
from pypdf import PdfReader

ROOT = Path(__file__).parent
ZIP = Path(r'C:\Users\SamuelOjuri\Downloads\wetransfer_srp1411-bba-01-rf-d-a-2108-154roofgaplan-nb1_p03-wip-dwg_2026-11-11_1057.zip')
with ZipFile(ZIP) as z:
    pdf_info = [f for f in z.infolist() if f.filename.lower().endswith('.pdf')]
    assert len(pdf_info) == 1
    f = pdf_info[0]
    assert f.file_size < 20_000_000
    target = ROOT / Path(f.filename).name
    target.write_bytes(z.read(f))
doc = PdfReader(target)
result = {'file':str(target.resolve()), 'pages':len(doc.pages), 'metadata':dict(doc.metadata), 'contents':[]}
for n, page in enumerate(doc.pages):
    content = {'page': n+1, 'size':[float(x) for x in page.mediabox], 'text':page.extract_text()}
    result['contents'].append(content)
(ROOT/'inspect_attachments_pdf_text.json').write_text(json.dumps(result, indent=2, ensure_ascii=False),encoding='utf-8')
print(json.dumps(result, indent=2, ensure_ascii=True))
