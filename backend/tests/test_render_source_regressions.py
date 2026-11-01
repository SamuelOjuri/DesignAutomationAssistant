"""Sanitized regressions for the November source-email investigation."""

from email.message import EmailMessage
import hashlib
from types import SimpleNamespace

import pytest

from backend.app.services.legacy_enquiry.email_extraction import process_email_content, extract_text_from_email
from backend.app.services.legacy_enquiry.source_documents import extract_source_records, source_record
from backend.app.services.performance_specification_evidence import resolve_email_evidence
from backend.app.services.specification_failures import SpecificationEvidenceError
from backend.app.services.specification_recovery import extraction_payload
from backend.app.services.specification_request_context import attachment_message, select_client_request
from backend.tests.test_specification_evidence import candidate, extraction, source, OBSERVED, SENT
from backend.tests.test_specification_publication_hardening import parameters, extract_twice
from backend.tests.test_specification_sources import image_bytes


def email_with_alternatives(plain, html):
    message = EmailMessage()
    message['From'] = 'Alex Example <alex@client.example>'
    message['To'] = 'sales@taperedplus.co.uk'
    message['Date'] = SENT
    message.set_content(plain)
    message.add_alternative(html, subtype='html')
    message.get_payload()[1].add_related(image_bytes('PNG', animated=False), maintype='image',
                                       subtype='png', cid='<logo@client>', filename='logo.png')
    return message.as_bytes()


def manifest_from_email(content):
    metadata, manifest = {}, []
    header, body, attachments, inline = process_email_content(content, 'request.eml', source_metadata=metadata)
    client = SimpleNamespace(max_attachment_workers=1, process_image=lambda *_:
                             '[TEXT REGION 1]\nExample Roofing\n[/TEXT REGION 1]')
    extract_text_from_email(header + '\n' + body, attachments, extractor=client, inline_images=inline,
                            manifest=manifest, source_prefix='asset:1', observed_at=OBSERVED, source_metadata=metadata)
    return manifest


def test_html_cid_recovers_signature_association_missing_from_plain_alternative():
    manifest = manifest_from_email(email_with_alternatives('Please price.\n[logo.png]',
        '<p>Please price.</p><img src="cid:logo@client">'))
    body, logo = manifest
    assert 'cid:logo@client' not in body['text']
    assert 'cid:logo@client' in body['emailHtmlText']
    assert attachment_message(logo, {s['sourceId']: s for s in manifest})['messageIndex'] == 0
    evidence = candidate('Company', 'Example Roofing', 'Example Roofing', request_role='direct_request', message_date=SENT)
    raw, _, issues = resolve_email_evidence(extraction(manifest, [evidence]))
    assert raw['Company'] == 'Example Roofing' and not issues


def test_html_and_plain_cid_occurrences_of_one_message_are_not_ambiguous():
    manifest = manifest_from_email(email_with_alternatives('[cid:logo@client]', '<img src="cid:logo@client">'))
    assert attachment_message(manifest[1], {s['sourceId']: s for s in manifest}) is not None


@pytest.mark.parametrize('wrong_message', [False, True])
def test_company_domain_citation_can_use_only_literal_branding_in_the_same_signature(wrong_message):
    html = '<p>www.client.example</p>'
    if wrong_message:
        html += ('<p>From: Other &lt;other@elsewhere.example&gt;<br>Sent: 29 October 2026 09:00<br>'
                 'To: Alex &lt;alex@client.example&gt;<br>Subject: Earlier request</p>')
    html += '<img src="cid:logo@client">'
    manifest = manifest_from_email(email_with_alternatives('www.client.example', html))
    fact = candidate('Company', 'Example Roofing', 'www.client.example', source_id='asset:1:body',
                     request_role='direct_request', message_date=SENT)
    initial = parameters(company='Example Roofing', specification_evidence=[fact])
    if wrong_message:
        with pytest.raises(SpecificationEvidenceError, match='Company'):
            extract_twice(initial, initial, manifest)
    else:
        result = extract_twice(initial, initial, manifest)
        assert result.specification_evidence[0].source_id == manifest[1]['sourceId']
        assert result.specification_evidence[0].snippet == 'Example Roofing'
        assert result._evidence_repairs[0]['before']['snippet'] == 'www.client.example'


@pytest.mark.parametrize('other_sender', [False, True])
def test_repeated_company_cid_must_still_belong_to_the_same_sender(other_sender):
    sender = 'other@elsewhere.example' if other_sender else 'alex@client.example'
    html = ('<p>www.client.example</p><img src="cid:logo@client">'
            f'<p>From: Earlier &lt;{sender}&gt;<br>Sent: 29 October 2026 09:00<br>'
            'To: Sales &lt;sales@taperedplus.co.uk&gt;<br>Subject: Earlier request</p>'
            '<img src="cid:logo@client">')
    manifest = manifest_from_email(email_with_alternatives('www.client.example', html))
    fact = candidate('Company', 'Example Roofing', 'www.client.example', source_id='asset:1:body',
                     request_role='direct_request', message_date=SENT)
    initial = parameters(company='Example Roofing', specification_evidence=[fact])
    if other_sender:
        with pytest.raises(SpecificationEvidenceError, match='Company'):
            extract_twice(initial, initial, manifest)
    else:
        result = extract_twice(initial, initial, manifest)
        assert result.specification_evidence[0].source_id == manifest[1]['sourceId']


@pytest.mark.parametrize('whitespace', [' ', ' \r\r\n '])
def test_gateway_footer_outside_word_container_identifies_outer_sender(whitespace):
    footer = 'Example Roofing is a limited company. Company number 12345.'
    html = ('<html><body><div class="WordSection1"><p>Please price.</p>'
            '<div>From: Third Party &lt;third@other.example&gt;<br>Sent: 29 October 2026 10:00<br>'
            'To: Alex Example &lt;alex@client.example&gt;<br>Subject: Request<br><br>Third party text.</div>'
            '</div><p>' + footer.replace(' ', whitespace) + '</p></body></html>')
    manifest = manifest_from_email(email_with_alternatives('Please price.\nFrom: Third Party <third@other.example>\n'
        'Sent: 29 October 2026 10:00\n\nThird party text.\n' + footer, html))
    evidence = candidate('Company', 'Example Roofing', footer, source_id='asset:1:body', request_role='direct_request')
    raw, _, issues = resolve_email_evidence(extraction(manifest, [evidence]))
    assert raw['Company'] == 'Example Roofing' and not issues


@pytest.mark.parametrize('same_sender', [True, False])
def test_earlier_signature_identifies_only_the_same_requester(same_sender):
    sender = 'alex@client.example' if same_sender else 'third@other.example'
    latest = source('asset:1:body', f'From: Alex <alex@client.example>\nTo: sales@taperedplus.co.uk\nDate: {SENT}\n\nPlease proceed.', 'email_body')
    prior = source('asset:2:body', f'From: Alex <{sender}>\nTo: sales@taperedplus.co.uk\nSent: 29 October 2026 10:00\n\nExample Roofing\n[cid:logo@client]', 'email_body')
    logo = source('asset:2:attachment:0', 'Example Roofing')
    logo['contentId'] = 'logo@client'
    fact = candidate('Company', 'Example Roofing', 'Example Roofing', source_id=logo['sourceId'], request_role='direct_request', message_date='29 October 2026 10:00')
    data = extraction([latest, prior, logo], [fact])
    raw, provenance, _ = resolve_email_evidence(data)
    assert (raw['Company'] == 'Example Roofing') is same_sender
    assert select_client_request(data['sourceManifest'])['sentDate'] == SENT
    if not same_sender:
        assert provenance['Company']['excludedCandidates']


def test_literal_message_date_format_is_repaired_without_a_model_correction():
    text = 'From: Alex <alex@client.example>\nSent: 22 October 2026 11:48\n\nUse Rockdeck'
    fact = candidate('Tapered Insulation', 'Rockdeck', 'Use Rockdeck', source_id='asset:1:body',
                     request_role='direct_request', message_date='2026-10-22 11:48')
    initial = parameters(tapered_insulation='Rockdeck', specification_evidence=[fact])
    result = extract_twice(initial, initial, [source('asset:1:body', text, 'email_body')])
    assert result.specification_evidence[0].message_date == '22 October 2026 11:48'
    assert any('message-date' in r['reason'] for r in result._evidence_repairs)


@pytest.mark.parametrize('duplicate', [False, True])
def test_unique_quote_can_repair_an_actual_wrong_header_but_repeated_quotes_cannot(duplicate):
    text = ('From: Alex <alex@client.example>\nDate: 30 October 2026 10:00\n\nPlease proceed.\n'
            'From: Earlier <earlier@client.example>\nSent: 29 October 2026 09:00\n\nUse Rockdeck')
    if duplicate:
        text += '\nFrom: Other <other@client.example>\nSent: 28 October 2026 08:00\n\nUse Rockdeck'
    fact = candidate('Tapered Insulation', 'Rockdeck', 'Use Rockdeck', source_id='asset:1:body',
                     request_role='direct_request', message_date='30 October 2026 10:00')
    initial = parameters(tapered_insulation='Rockdeck', specification_evidence=[fact])
    manifest = [source('asset:1:body', text, 'email_body')]
    if duplicate:
        with pytest.raises(SpecificationEvidenceError, match='timestamp'):
            extract_twice(initial, initial, manifest)
    else:
        result = extract_twice(initial, initial, manifest)
        corrected = result.specification_evidence[0]
        assert corrected.message_date == '29 October 2026 09:00'
        assert corrected.request_role == 'forwarded_request'
        assert corrected.value == 'Rockdeck' and corrected.role == fact['role']
        assert result._evidence_repairs[0]['before']['message_date'] == fact['message_date']


def test_containing_message_repair_does_not_resolve_an_invented_time():
    text = 'From: Alex <alex@client.example>\nSent: 29 October 2026 09:00\n\nUse Rockdeck'
    fact = candidate('Tapered Insulation', 'Rockdeck', 'Use Rockdeck', source_id='asset:1:body',
                     request_role='direct_request', message_date='30 October 2026 10:00')
    initial = parameters(tapered_insulation='Rockdeck', specification_evidence=[fact])
    with pytest.raises(SpecificationEvidenceError, match='timestamp'):
        extract_twice(initial, initial, [source('asset:1:body', text, 'email_body')])


def test_unresolved_note_keeps_private_replay_data_out_of_exception_message():
    initial = parameters(specification_request_notes=[{'source_id': 'asset:1:body',
                         'snippet': 'PRIVATE invented constraint', 'category': 'constraint'}])
    with pytest.raises(SpecificationEvidenceError) as error:
        extract_twice(initial, initial, [source('asset:1:body', 'Actual request', 'email_body')])
    assert 'PRIVATE' not in str(error.value)
    assert error.value.evidence_failure['noteIssues']
    assert error.value.evidence_failure['extraction']['specificationRequestNotes'][0]['snippet'].startswith('PRIVATE')


def test_nul_normalization_retains_boundaries_literal_escapes_and_raw_hash():
    content = b'Roof\x00A: 1\x0050 mm; literal \\u0000'
    record, = extract_source_records(content, 'notes.txt', None, source_id='asset:1:attachment:0')
    assert record['text'] == r'Roof A: 1 50 mm; literal \u0000'
    assert record['contentSha256'] == hashlib.sha256(content).hexdigest()
    assert source_record('body', 'email', content, source_type='email_body', text='A\0B')['text'] == 'A B'


@pytest.mark.parametrize('response', [None, '', 'Unable to process this image'])
def test_empty_image_extraction_has_a_safe_actionable_code(response):
    client = SimpleNamespace(process_image=lambda *_: response)
    record, = extract_source_records(image_bytes('PNG', animated=False), 'plan.png', client, source_id='asset:1:attachment:0')
    assert record['status'].startswith('failed:')
    assert record['error']['code'] == 'no_readable_text'


def test_model_introduced_nul_is_rejected_before_persistence():
    initial = parameters(company='Example\0Roofing')
    with pytest.raises(ValueError, match='NUL'):
        extract_twice(initial, initial, [source('asset:1:body', 'Example Roofing', 'email_body')])


@pytest.mark.parametrize('field,value', [('Drawing Reference', 'TP12345_26.01 - C'), ('Revision', '26.01 - C')])
def test_redundant_parts_cannot_invalidate_a_literal_full_reference(field, value):
    text = 'Reference: TP12345_26.01 - C'
    fact = candidate(field, value, text, reference_parts=['TP12345', '26.01 - C'])
    raw, _, issues = resolve_email_evidence(extraction([source('asset:1:attachment:0', text)], [fact]))
    assert raw[field] == value and not issues
    fact['reference_parts'] = ['TP99999', '26.01 - C']
    assert field in resolve_email_evidence(extraction([source('asset:1:attachment:0', text)], [fact]))[2]


@pytest.mark.parametrize('name,accepted', [("John O'Example", True), ('J O\'Example', False), ('John Example', False)])
def test_contact_can_reorder_only_the_complete_literal_header_name(name, accepted):
    text = f'From: "O\'Example, John" <john@client.example>\nTo: sales@taperedplus.co.uk\nDate: {SENT}\n\nPlease proceed.'
    fact = candidate('Contact', name, text.splitlines()[0], source_id='asset:1:body', request_role='direct_request')
    raw, _, issues = resolve_email_evidence(extraction([source('asset:1:body', text, 'email_body')], [fact]))
    assert (raw['Contact'] == name and not issues) is accepted


def test_verified_long_note_splits_without_dropping_text():
    from backend.app.services.specification_recovery import bound_verified_request_notes
    text = 'Do not exceed 150 mm. ' * 150
    note = {'source_id': 'asset:1:body', 'snippet': text, 'scope': None, 'category': 'constraint'}
    data, repairs = bound_verified_request_notes({'specification_request_notes': [note]},
                                                 [source('asset:1:body', text, 'email_body')])
    assert all(len(n['snippet']) <= 2000 for n in data['specification_request_notes'])
    assert ' '.join(n['snippet'].strip() for n in data['specification_request_notes']) == text.strip()
    assert len(repairs) == 1
    bad, repairs = bound_verified_request_notes({'specification_request_notes': [note]},
                                                [source('asset:1:body', 'Different constraint', 'email_body')])
    assert bad['specification_request_notes'] == [note] and not repairs
