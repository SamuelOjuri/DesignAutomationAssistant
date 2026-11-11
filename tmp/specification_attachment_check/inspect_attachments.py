from pathlib import Path
from email import policy
from email.parser import BytesParser
import json
import zipfile
import re

ROOT = Path(__file__).parent
EMAIL = Path(r'C:\Users\SamuelOjuri\Downloads\20261111-115947-bb164505116e4fc9b738c257c3bd9e20.eml')
ZIP = Path(r'C:\Users\SamuelOjuri\Downloads\wetransfer_srp1411-bba-01-rf-d-a-2108-154roofgaplan-nb1_p03-wip-dwg_2026-11-11_1057.zip')
msg = BytesParser(policy=policy.default).parsebytes(EMAIL.read_bytes())
result = {'email': {'headers': {k: str(msg[k]) for k in ('Subject','From','To','Cc','Date','Message-ID') if msg[k]}, 'parts': []}, 'zip': []}
for i, part in enumerate(msg.walk()):
    if part.is_multipart():
        continue
    payload = part.get_payload(decode=True) or b''
    info = {'index': i, 'content_type':part.get_content_type(), 'filename':part.get_filename(), 'bytes':len(payload)}
    if part.get_content_type() in ('text/plain', 'text/html') and len(payload) < 5_000_000:
        body = part.get_content()
        info['body'] = body
    result['email']['parts'].append(info)
with zipfile.ZipFile(ZIP) as z:
    for f in z.infolist():
        result['zip'].append({'name':f.filename,'bytes':f.file_size,'compressed':f.compress_size,'directory':f.is_dir()})
ROOT.mkdir(exist_ok=True)
OUT = ROOT/'inspect_attachments.json'
OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
print(json.dumps(result, indent=2, ensure_ascii=False))
