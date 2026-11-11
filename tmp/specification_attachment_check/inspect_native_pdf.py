from pathlib import Path
import time
import json
from pypdf import PdfReader
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.services.legacy_enquiry.native_pdf_text import native_pdf_text

root = Path(__file__).parent
path = next((root/'diagnostic_input').glob('*.pdf'))
started = time.monotonic()
reader = PdfReader(path)
probe_started = time.monotonic()
probe = native_pdf_text(reader)
print(json.dumps({'native_fast_path_reason':probe.reason,'accepted':probe.text is not None,
                  'native_gate_elapsed':time.monotonic()-probe_started}),flush=True)
result = {'root_keys': list(reader.trailer['/Root']), 'pages':[]}
for n, page in enumerate(reader.pages):
    data = {'page':n+1, 'resources':list(page['/Resources']), 'annotations':len(page.get('/Annots',[])), 'fonts':[],'xobjects':[]}
    for name, raw in page['/Resources'].get('/Font',{}).items():
        font = raw.get_object()
        data['fonts'].append({'name':name,'subtype':font.get('/Subtype'),'base':font.get('/BaseFont'),'unicode':'/ToUnicode' in font, 'encoding':str(font.get('/Encoding'))[:160]})
    for name, raw in page['/Resources'].get('/XObject',{}).items():
        obj = raw.get_object()
        data['xobjects'].append({'name':name,'subtype':obj.get('/Subtype'),'width':obj.get('/Width'),'height':obj.get('/Height'),'length':len(obj.get_data()),'resources':list(obj.get('/Resources',{}))})
    print(json.dumps(data),flush=True)
    def visitor_operand(operator, operands, ctm, tm):
        if operator == b'Do':
            print(json.dumps({'operator':'Do','name':str(operands[0]),'ctm':ctm}),flush=True)
    text = page.extract_text(visitor_operand_before=visitor_operand)
    for idx, image in enumerate(page.images):
        (root/f'inspect_native_embedded_{idx}{Path(image.name).suffix}').write_bytes(image.data)
    (root/'inspect_native_pdf_text.txt').write_text(text,encoding='utf-8')
    data['text_length'] = len(text)
    result['pages'].append(data)
result['elapsed'] = time.monotonic()-started
(root/'inspect_native_pdf_result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result),flush=True)
