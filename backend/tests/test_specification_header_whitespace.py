"""Literal Outlook headers rendered with HTML non-breaking spaces."""

from copy import deepcopy

import pytest

from backend.app.services.performance_specification_evidence import resolve_baseline_drawing_identity
from backend.app.services.specification_request_context import email_messages, source_email_messages
from backend.tests.test_specification_evidence import candidate, extraction, source


OUTER_DATE = "Wed, 11 Nov 2026 16:57:31 +0000"
APPLE_DATE = "11 Nov 2026 16:49"
FORWARDED_DATE = "09 November 2026 09:25"
REFERENCE = "TP19019_26.01 - A"
FILENAME = f"<{REFERENCE} - Drawing.pdf>"
OUTER = ("From: Alex Example <alex@client.example>\nTo: Sales <sales@taperedplus.co.uk>\n"
         f"Date: {OUTER_DATE}\nSubject: Re: Roof\n\nPlease use the alternative.\n")
APPLE = "On 11 Nov 2026, at 16:49, Sales <sales@taperedplus.co.uk> wrote:\nReview the earlier drawing.\n"


def forwarded_headers(space):
    return (f"From:{space}Alex Example <alex@client.example>\n"
            f"Sent:{space}{FORWARDED_DATE}\nTo:{space}sales@taperedplus.co.uk\n"
            f"Cc:{space}engineer@taperedplus.co.uk\nSubject:{space}Roof\n")


def baseline_payload():
    forwarded = forwarded_headers("\u00a0") + f"\nOriginal request.\n{FILENAME}\n"
    # The plain alternative retains quote prefixes, while HTML exposes the
    # literal headers. The earlier message must be found within HTML itself.
    plain = OUTER + APPLE + "\n".join("> " + line for line in forwarded.splitlines())
    record = source("asset:1:body", plain, "email_body")
    record["emailHtmlText"] = OUTER + APPLE + forwarded
    evidence = candidate("Drawing Reference", REFERENCE, FILENAME, source_id=record["sourceId"],
                         role="baseline", request_role="forwarded_request", message_date=FORWARDED_DATE)
    return extraction([record], [evidence])


@pytest.mark.parametrize("space", [" ", "\t", "\u00a0", " \u00a0\t"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_forwarded_header_spacing_preserves_literal_message_boundaries(space, newline):
    headers = forwarded_headers(space)
    text = OUTER + APPLE + headers + f"\nOriginal request.\n{FILENAME}\n"
    messages = email_messages(text.replace("\n", newline))
    assert [message["sentDate"] for message in messages] == [OUTER_DATE, APPLE_DATE, FORWARDED_DATE]
    earlier = messages[2]
    assert earlier["sender"] == "alex@client.example"
    assert earlier["recipients"] == ["sales@taperedplus.co.uk", "engineer@taperedplus.co.uk"]
    assert earlier["sentAt"] == "2026-11-09T09:25:00+00:00"
    assert earlier["requestRole"] == "forwarded_request"
    assert earlier["headerSnippet"] == headers.rstrip("\n")
    assert earlier["text"] == text[len(OUTER + APPLE):]
    assert all(FILENAME not in message["text"] for message in messages[:2])


def test_baseline_reference_verifies_using_unchanged_nbsp_html_source():
    payload = baseline_payload()
    original = deepcopy(payload)
    facts, reason = resolve_baseline_drawing_identity(payload)
    assert reason is None
    fact, = facts
    assert fact["value"] == REFERENCE and fact["snippet"] == FILENAME
    assert fact["message_date"] == FORWARDED_DATE
    assert fact["observedAt"] == "2026-11-09T09:25:00+00:00"
    assert payload == original
    messages = source_email_messages(payload["sourceManifest"][0])
    assert len(messages) == 3 and "From:\u00a0Alex Example" in messages[2]["headerSnippet"]


@pytest.mark.parametrize("case", ["outer_date", "apple_date", "wrong_role", "body_date", "duplicate_date"])
def test_nbsp_header_support_does_not_accept_unverified_request_dates(case):
    payload = baseline_payload()
    evidence = payload["specificationEvidence"][0]
    record = payload["sourceManifest"][0]
    if case == "outer_date":
        evidence.update(message_date=OUTER_DATE, request_role="direct_request")
    elif case == "apple_date":
        evidence["message_date"] = APPLE_DATE
    elif case == "wrong_role":
        evidence["request_role"] = "direct_request"
    elif case == "body_date":
        record["emailHtmlText"] = record["emailHtmlText"].replace(f"Sent:\u00a0{FORWARDED_DATE}\n", "")
        record["emailHtmlText"] += f"\nSent:\u00a0{FORWARDED_DATE}\n"
    else:
        record["emailHtmlText"] = record["emailHtmlText"].replace(
            f"Sent:\u00a0{FORWARDED_DATE}\n", f"Sent:\u00a0{FORWARDED_DATE}\nDate:\u00a0{FORWARDED_DATE}\n")
    facts, reason = resolve_baseline_drawing_identity(payload)
    assert not facts and reason == "Candidate request timestamp could not be verified"
