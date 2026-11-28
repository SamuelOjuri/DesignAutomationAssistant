"""Attached email bodies retain their identity, ancestry and extraction bounds."""

import hashlib
import io
import base64
from email.message import EmailMessage
from email.mime.base import MIMEBase
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from backend.app.services.legacy_enquiry import email_extraction as email
from backend.app.services.legacy_enquiry import source_documents as documents
from backend.app.services.specification_request_context import select_client_request
from backend.tests.test_specification_sources import image_bytes


OUTER_DATE = "Mon, 16 Nov 2026 11:23:25 +0000"
CHILD_DATE = "Mon, 16 Nov 2026 10:45:51 +0000"


def message(body="Please amend as per the attached email.", *, sender="client@external.example", date=OUTER_DATE):
    mail = EmailMessage()
    mail["From"], mail["To"], mail["Subject"], mail["Date"] = sender, "sales@taperedplus.co.uk", "Roof request", date
    mail.set_content(body)
    return mail


def child():
    return message("Use TP12345_22.02-D. Plywood deck; target 0.10 W/m2K.",
                   sender="architect@thirdparty.example", date=CHILD_DATE)


def extract(mail, *, extractor=None, progress=None):
    metadata, manifest = {}, []
    header, body, attachments, images = email.process_email_content(
        mail.as_bytes(), "outer.eml", source_metadata=metadata)
    context = email.extract_text_from_email(header + "\n" + body, attachments,
        inline_images=images, extractor=extractor or SimpleNamespace(max_attachment_workers=1),
        source_prefix="asset:1", observed_at="2026-11-16T11:23:25+00:00",
        source_metadata=metadata, manifest=manifest, on_source=progress)
    return context, manifest


def test_unnamed_rfc822_is_preserved_without_flattening_its_body():
    mail = message()
    mail.add_attachment(child())
    header, body, attachments, images = email.process_email_content(mail.as_bytes(), "outer.eml")
    assert "Plywood" not in body and not images
    assert len(attachments) == 1 and attachments[0]["filename"] == "attached-email-1.eml"
    context, manifest = extract(mail)
    outer, nested = manifest
    assert outer["sourceId"] == "asset:1:body" and "parentSourceId" not in outer
    assert nested["sourceId"] == "asset:1:attachment:0:body"
    assert nested["parentSourceId"] == outer["sourceId"] and nested["attachedEmail"]
    assert nested["observedAt"] == "2026-11-16T10:45:51+00:00"
    assert nested["contentSha256"] == hashlib.sha256(nested["text"].encode()).hexdigest()
    assert nested["emailContainerSha256"] == hashlib.sha256(attachments[0]["content"]).hexdigest()
    assert "Plywood" in nested["text"] and "PARENT SOURCE ID: asset:1:body" in context
    assert "ATTACHED EMAIL: separate message" in context
    assert select_client_request(manifest)["sender"] == "client@external.example"


@pytest.mark.parametrize("html_only", [False, True])
def test_child_plain_and_html_alternatives_remain_own_literal_sources(html_only):
    nested = child()
    if html_only:
        nested.clear_content()
        nested.add_alternative("<p>Current plywood request</p><blockquote><p>From: old@other.example</p><p>Old request</p></blockquote>", subtype="html")
    else:
        nested.add_alternative("<p>HTML variant with cid:child-logo</p>", subtype="html")
    mail = message()
    mail.add_attachment(nested)
    _, records = extract(mail)
    assert "emailHtmlText" in records[1]
    assert "architect@thirdparty.example" in records[1]["emailHtmlText"]
    if html_only:
        assert "Current plywood request" in records[1]["text"] and "\nFrom: old@other.example\n" in records[1]["text"]
    else:
        assert "Plywood" in records[1]["text"] and "HTML variant" in records[1]["emailHtmlText"]


def test_child_documents_and_cids_keep_nested_parent_and_do_not_leak_to_outer_images():
    nested = child()
    nested.add_alternative('<p>See figure</p><img src="cid:figure">', subtype="html")
    nested.get_payload()[1].add_related(image_bytes("PNG", animated=False), maintype="image", subtype="png",
                                     cid="<figure>", filename="figure.png")
    nested.add_attachment(b"Decking: plywood", maintype="text", subtype="plain", filename="request.txt")
    mail = message()
    mail.add_attachment(nested)
    _, _, attachments, images = email.process_email_content(mail.as_bytes(), "outer.eml")
    assert len(attachments) == 1 and not images
    _, records = extract(mail, extractor=SimpleNamespace(max_attachment_workers=1, process_image=lambda *_: "Deck figure"))
    nested_body = next(record for record in records if record.get("attachedEmail"))
    nested_children = [record for record in records if record.get("parentSourceId") == nested_body["sourceId"]]
    assert {record["filename"] for record in nested_children} == {"request.txt", "figure.png"}
    figure = next(record for record in nested_children if record["filename"] == "figure.png")
    assert figure["contentId"] == "figure" and figure["observedAt"] == nested_body["observedAt"]
    assert figure["containingMessage"]["sourceId"] == nested_body["sourceId"]


def test_duplicate_attached_filenames_have_distinct_deterministic_source_ids():
    mail = message()
    mail.add_attachment(child(), filename="request.eml")
    mail.add_attachment(child(), filename="request.eml")
    _, first = extract(mail)
    _, second = extract(mail)
    assert first == second
    assert len({record["sourceId"] for record in first}) == 3
    assert first[1]["filename"] == first[2]["filename"] == "request.eml"


def test_named_octet_stream_eml_is_parsed_as_an_email_not_as_a_document():
    mail = message()
    mail.add_attachment(child().as_bytes(), maintype="application", subtype="octet-stream", filename="request.eml")
    _, records = extract(mail)
    assert records[1]["sourceType"] == "email_body" and "Plywood" in records[1]["text"]


def test_base64_encoded_rfc822_attachment_retains_the_nested_email_body():
    part = MIMEBase("message", "rfc822")
    part.set_payload(base64.b64encode(child().as_bytes()).decode("ascii"))
    part["Content-Transfer-Encoding"] = "base64"
    part["Content-Disposition"] = "attachment"
    mail = message()
    mail.make_mixed()
    mail.attach(part)
    _, records = extract(mail)
    assert "Plywood" in records[1]["text"]


def test_nested_attachment_count_is_bounded_across_messages(monkeypatch):
    monkeypatch.setattr(email, "MAX_EMAIL_ATTACHMENTS", 4)
    mail = message()
    nested = child()
    nested.add_attachment(b"one", maintype="text", subtype="plain", filename="one.txt")
    nested.add_attachment(b"two", maintype="text", subtype="plain", filename="two.txt")
    mail.add_attachment(nested)
    mail.add_attachment(nested)
    with pytest.raises(documents.IncompleteSourceExtraction) as error:
        extract(mail)
    assert any(record.get("error", {}).get("code") == "email_attachment_limit" for record in error.value.manifest)


def test_zip_email_progress_resolves_the_actual_body_identity():
    archive = io.BytesIO()
    with ZipFile(archive, "w") as output:
        output.writestr("request.eml", child().as_bytes())
    events = {}
    records = documents.extract_source_records(archive.getvalue(), "messages.zip", SimpleNamespace(), source_id="zip:1",
        on_source=lambda event: events.update({event["sourceId"]: event["status"]}))
    assert set(events) == {record["sourceId"] for record in records}
    assert set(events.values()) == {"read", "expanded:archive"}


def test_nested_email_depth_limit_retains_explicit_permanent_failure():
    mail = child()
    for _ in range(4):
        wrapper = message()
        wrapper.add_attachment(mail)
        mail = wrapper
    manifest = []
    header, body, attachments, images = email.process_email_content(mail.as_bytes(), "outer.eml")
    with pytest.raises(documents.IncompleteSourceExtraction):
        email.extract_text_from_email(header + "\n" + body, attachments, inline_images=images,
            extractor=SimpleNamespace(max_attachment_workers=1), manifest=manifest)
    failed = [record for record in manifest if record["status"].startswith("failed:")]
    assert len(failed) == 1 and failed[0]["error"]["code"] == "email_depth_limit"
    assert documents.permanent_source_failure(manifest)


def test_attached_message_count_is_shared_across_siblings(monkeypatch):
    monkeypatch.setattr(email, "MAX_ATTACHED_EMAIL_MESSAGES", 2)
    mail = message()
    for _ in range(3):
        mail.add_attachment(child())
    with pytest.raises(documents.IncompleteSourceExtraction) as error:
        extract(mail)
    assert sum(record.get("error", {}).get("code") == "email_message_limit" for record in error.value.manifest) == 1


def test_attached_message_count_is_shared_through_zip_members(monkeypatch):
    monkeypatch.setattr(email, "MAX_ATTACHED_EMAIL_MESSAGES", 1)
    archive = io.BytesIO()
    with ZipFile(archive, "w") as output:
        output.writestr("first.eml", child().as_bytes())
        output.writestr("second.eml", child().as_bytes())
    records = documents.extract_source_records(archive.getvalue(), "messages.zip", SimpleNamespace(), source_id="zip:1")
    assert sum(record.get("error", {}).get("code") == "email_message_limit" for record in records) == 1
    assert records[1]["sourceType"] == "email_body" and records[1]["parentSourceId"] == "zip:1"


def test_message_byte_and_text_limits_are_explicit_not_empty_success(monkeypatch):
    monkeypatch.setattr(email, "MAX_SOURCE_BYTES", 20)
    records = email.extract_attached_email_records(child().as_bytes(), "request.eml", SimpleNamespace(), source_id="x")
    assert records[0]["error"]["code"] == "email_expansion_limit"
    monkeypatch.setattr(email, "MAX_SOURCE_BYTES", documents.MAX_SOURCE_BYTES)
    monkeypatch.setattr(email, "MAX_CONTEXT_CHARS", 20)
    records = email.extract_attached_email_records(child().as_bytes(), "request.eml", SimpleNamespace(), source_id="x")
    assert records[0]["error"]["code"] == "email_text_limit"


def test_default_text_budget_preserves_skipped_sources_and_original_failures(monkeypatch):
    monkeypatch.setattr(email, "MAX_CONTEXT_CHARS", 300)
    mail = message("Synthetic request")
    mail.add_attachment(b"Readable notes.\n" * 30, maintype="text", subtype="plain", filename="large.txt")
    mail.add_attachment(b"Synthetic CAD source", maintype="application", subtype="octet-stream", filename="plan.dwg")
    mail.add_attachment(b"\xff", maintype="text", subtype="plain", filename="unreadable.txt")

    with pytest.raises(documents.IncompleteSourceExtraction) as error:
        extract(mail)

    body, large, drawing, unreadable = error.value.manifest
    assert body["status"] == "read"
    assert large["error"]["code"] == "email_text_limit"
    assert drawing["status"] == "skipped:dwg"
    assert unreadable["status"] == "failed:UnicodeDecodeError"


def test_malformed_attached_email_is_an_audited_permanent_failure():
    records = documents.extract_source_records(b"not an email", "bad.eml", SimpleNamespace(), source_id="x")
    assert records[0]["status"] == "failed:SourceDocumentError"
    assert records[0]["error"]["code"] == "unreadable_attached_email"
    assert documents.permanent_source_failure(records)


def test_truncated_attached_multipart_is_not_accepted_as_an_empty_email():
    content = (b"From: architect@external.example\nTo: sales@taperedplus.co.uk\n"
               b"Subject: Plywood request\nMIME-Version: 1.0\n"
               b"Content-Type: multipart/mixed; boundary=missing\n\nNo MIME boundary exists.")
    records = documents.extract_source_records(content, "bad.eml", SimpleNamespace(), source_id="x")
    assert records[0]["error"]["code"] == "unreadable_attached_email"
    assert records[0]["status"] == "failed:SourceDocumentError"


def test_progress_does_not_leave_attached_email_container_pending():
    mail = message()
    mail.add_attachment(child())
    events = {}
    extract(mail, progress=lambda event: events.update({event["sourceId"]: event["status"]}))
    assert all(status == "read" for status in events.values())
    assert set(events) == {"asset:1:body", "asset:1:attachment:0:body"}


def test_outlook_embedded_message_is_exported_in_memory_even_without_filename(monkeypatch):
    exported = []
    nested = SimpleNamespace(exportBytes=lambda: exported.append(True) or b"nested Outlook bytes")

    class Message:
        sender, to, subject, date, body, htmlBody = "a@external.example", "sales@taperedplus.co.uk", "Request", OUTER_DATE, "Please amend", b""
        attachments = [SimpleNamespace(longFilename=None, shortFilename=None, data=nested)]

        def close(self):
            pass

    monkeypatch.setattr(email.extract_msg, "Message", lambda _: Message())
    _, _, attachments, inline = email.process_email_content(b"Outlook bytes", "outer.msg")
    assert exported == [True] and not inline
    assert attachments == [{"filename": "attached-email-1.msg", "content": b"nested Outlook bytes"}]


def test_unexportable_outlook_embedded_message_is_not_silently_skipped(monkeypatch):
    class Message:
        sender, to, subject, date, body, htmlBody = "a@external.example", "sales@taperedplus.co.uk", "Request", OUTER_DATE, "Please amend", b""
        attachments = [SimpleNamespace(longFilename=None, shortFilename=None, data=object())]

        def close(self):
            pass

    monkeypatch.setattr(email.extract_msg, "Message", lambda _: Message())
    with pytest.raises(documents.SourceDocumentError, match="cannot be exported"):
        email.process_email_content(b"Outlook bytes", "outer.msg")


def test_nested_source_progress_cancellation_is_not_relabelled_as_bad_email():
    class StopWork(RuntimeError):
        pass

    def stop(_):
        raise StopWork("Stop this execution")

    with pytest.raises(StopWork):
        documents.extract_source_records(child().as_bytes(), "nested.eml", SimpleNamespace(),
            source_id="x", on_source=stop)
