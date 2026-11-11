"""Outlook layout changes must not lose or fabricate the requesting message."""

import pytest

from backend.app.config import settings
from backend.app.services.specification_request_context import (
    email_messages, select_client_request,
)


@pytest.fixture(autouse=True)
def internal_domains(monkeypatch):
    monkeypatch.setattr(settings, "design_processing_internal_email_domains", "taperedplus.co.uk")


def source(text, **extra):
    return {"sourceId": "asset:1:body", "sourceType": "email_body", "status": "read",
            "text": text, **extra}


@pytest.mark.parametrize("sender_line", ["Client <client@example.com>", " Client <client@example.com>"])
def test_split_html_sender_and_outlook_recipients_keep_literal_message_boundaries(sender_line):
    outer = ("From: Internal <engineer@taperedplus.co.uk>\n"
             "To: Client <client@example.com>\nDate: 11 November 2026 11:59\n\nReceived.\n\n")
    current_header = (f"From:\n{sender_line}\n\nSent: 11 November 2026 10:59\n\n"
                      'To: "Service; Design" <sales@taperedplus.co.uk>; Colleague <colleague@example.com>\n\n'
                      "Cc: Team: backup@taperedplus.co.uk, technical@example.com;\n\nSubject: Roof plans")
    current = current_header + "\n\nThe drawings are uploaded.\n[cid:current-logo]\n"
    earlier = ("From:\n Earlier <earlier@example.com>\n\nSent: 10 November 2026 12:10\n\n"
               "To: Sales <sales@taperedplus.co.uk>; Client <client@example.com>\n\n"
               "Subject: Earlier request\n\nPlease revise the existing scheme.\n[cid:old-logo]\n")
    html_text = outer + current + earlier
    context = select_client_request([source("Plain alternative", emailHtmlText=html_text)])
    assert context["sender"] == "client@example.com"
    assert context["sentAt"] == "2026-11-11T10:59:00+00:00"
    assert context["messageIndex"] == 1 and context["requestRole"] == "forwarded_request"
    assert set(context["recipients"]) == {
        "sales@taperedplus.co.uk", "colleague@example.com", "backup@taperedplus.co.uk", "technical@example.com",
    }
    assert context["headerSnippet"] == current_header
    assert context["headerSnippet"] in html_text
    assert context["text"] == current
    assert "old-logo" not in context["text"] and "current-logo" in context["text"]


def test_plain_outlook_mailto_addresses_and_semicolon_lists_are_readable():
    text = ("From: Client <client@example.com<mailto:client@example.com>>\n"
            "Sent: 11 November 2026 10:59\n"
            "To: Other <other@example.com<mailto:other@example.com>>; "
            "Sales <sales@taperedplus.co.uk<mailto:sales@taperedplus.co.uk>>\n\nPlease review.")
    context = select_client_request([source(text)])
    assert context["sender"] == "client@example.com"
    assert context["recipients"] == ["other@example.com", "sales@taperedplus.co.uk"]


@pytest.mark.parametrize("body", [
    "From:\nNo sender was supplied.\nSent: 12 November 2026 10:00\nTo: sales@taperedplus.co.uk",
    "From:\nclient@example.com\nThis is ordinary body text.\nDate: 12 November 2026 10:00\nTo: sales@taperedplus.co.uk",
    "Sent: 12 November 2026 10:00\nTo: sales@taperedplus.co.uk\nA body date has no From header.",
])
def test_body_dates_and_incomplete_split_headers_cannot_create_client_context(body):
    outer = ("From: Internal <engineer@taperedplus.co.uk>\nTo: sales@taperedplus.co.uk\n"
             "Date: 11 November 2026 11:59\n\n" + body)
    assert select_client_request([source(outer)]) is None
    assert all(message["sender"] != "client@example.com" for message in email_messages(outer))


def test_blank_body_boundary_stops_indented_text_extending_a_complete_date():
    text = ("From: client@example.com\nTo: sales@taperedplus.co.uk\nDate: 11 November 2026 10:59\n\n"
            "    Maximum height 150 mm.\nSent: 12 November 2026 12:00")
    context = select_client_request([source(text)])
    assert context["sentDate"] == "11 November 2026 10:59"
    assert "Maximum height" not in context["headerSnippet"]
