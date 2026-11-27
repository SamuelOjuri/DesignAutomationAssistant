"""Attached client requests retain identity, provenance and quoted-history bounds."""

import hashlib

import pytest

from backend.app.config import settings
from backend.app.services.performance_specification_evidence import (
    FIELD_NAMES, VALUE_CONFLICT, resolve_baseline_drawing_identity, resolve_email_evidence, resolve_request_notes,
)
from backend.app.services.specification_request_context import (
    adopted_request_contexts, parent_email, select_client_request,
)
from backend.app.services.specification_semantics import EVIDENCE_VERSION


OUTER = "asset:1:body"
CHILD = "asset:1:attachment:0:body"
DATE = "Mon, 16 Nov 2026 11:23:25 +0000"
CHILD_DATE = "Mon, 16 Nov 2026 10:45:51 +0000"
OLD_DATE = "Thu, 12 Nov 2026 13:52:00 +0000"
REFERENCE = "TP12345_22.02-D"
OLD_REFERENCE = "TP12345_26.03-A"


@pytest.fixture(autouse=True)
def internal_domains(monkeypatch):
    monkeypatch.setattr(settings, "design_processing_internal_email_domains", "taperedplus.co.uk")


def email(identity, body, *, sender="Client <client@manufacturer.example>", recipient="sales@taperedplus.co.uk",
          date=DATE, parent=None):
    text = f"From: {sender}\nTo: {recipient}\nSubject: Existing roof\nDate: {date}\n\n{body}"
    record = {"sourceId": identity, "sourceType": "email_body", "status": "read", "text": text,
              "filename": "attached.eml" if parent else "Email body",
              "contentSha256": hashlib.sha256(text.encode()).hexdigest(), "observedAt":
              "2026-11-16T10:45:51+00:00" if parent else "2026-11-16T11:23:25+00:00"}
    if parent:
        record.update(parentSourceId=parent, attachedEmail=True, emailContainerSha256="f" * 64)
    return record


def candidate(label, value, snippet, *, source_id=CHILD, role="requested", date=CHILD_DATE,
              request_role="document", scope=None):
    return {"field": label, "value": value, "source_id": source_id, "snippet": snippet,
            "location": "email message", "scope": scope, "issuer": "TaperedPlus" if label == "Drawing Reference" else None,
            "role": role, "request_role": request_role, "message_date": date,
            "supersedes": [], "rationale": "Literal current request"}


def extraction(sources, evidence, **extra):
    return {"specificationEvidenceVersion": EVIDENCE_VERSION, "sourceManifest": sources,
            "specificationAssessedFields": list(FIELD_NAMES), "performanceParameters": {},
            "specificationEvidence": evidence, **extra}


def history():
    return (f"\n\nFrom: Sales <sales@taperedplus.co.uk>\nTo: client@manufacturer.example\nDate: {OLD_DATE}\n\n"
            f"Previously issued {REFERENCE} and {OLD_REFERENCE}.\n16mm LEWIS decking.")


def request_sources(*, outer_body="Please see attached. Could you amend as per the attached please?",
                    child_body=None):
    return [email(OUTER, outer_body + "\nROCKWOOL UK" + history()),
            email(CHILD, child_body or f"Follow the same principles as {REFERENCE}. The build-up includes a plywood deck.\nMSMR Architects",
                  sender="Architect <architect@designer.example>", recipient="client@manufacturer.example",
                  date=CHILD_DATE, parent=OUTER)]


def test_attached_correspondent_cannot_replace_current_client_identity_or_date():
    sources = request_sources()
    assert select_client_request(sources)["sourceId"] == OUTER
    assert select_client_request(sources)["sender"] == "client@manufacturer.example"
    evidence = [candidate("Company", "ROCKWOOL UK", "ROCKWOOL UK", source_id=OUTER,
                          date=DATE, request_role="direct_request"),
                candidate("Company", "MSMR Architects", "MSMR Architects"),
                candidate("Date Received", "16 Nov 2026", f"Date: {CHILD_DATE}")]
    raw, provenance, issues = resolve_email_evidence(extraction(sources, evidence))
    assert raw["Company"] == "ROCKWOOL UK" and raw["Date Received"] is None and not issues
    assert provenance["Company"]["excludedCandidates"][0]["value"] == "MSMR Architects"
    contexts = adopted_request_contexts(sources)
    assert len(contexts) == 1 and contexts[0]["sourceId"] == CHILD
    assert contexts[0]["sentAt"] == "2026-11-16T10:45:51+00:00"
    assert "plywood" in contexts[0]["text"]


def test_adopted_current_baseline_resolves_older_outer_reference_ambiguity():
    sources = request_sources()
    old_snippet = f"Previously issued {REFERENCE} and {OLD_REFERENCE}."
    evidence = [candidate("Drawing Reference", reference, old_snippet, source_id=OUTER,
                          role="baseline", date=OLD_DATE, request_role="forwarded_request")
                for reference in (REFERENCE, OLD_REFERENCE)]
    evidence.append(candidate("Drawing Reference", REFERENCE, f"Follow the same principles as {REFERENCE}.", role="baseline"))
    baselines, issue = resolve_baseline_drawing_identity(extraction(sources, evidence))
    assert [entry["value"] for entry in baselines] == [REFERENCE]
    assert issue is None and baselines[0]["attachedMessageSourceId"] == CHILD


def test_current_plywood_request_replaces_historical_deck_and_nested_quotes_stay_excluded():
    sources = request_sources(child_body=f"The build-up includes a plywood deck.\n{history()}")
    evidence = [candidate("Decking", "16mm LEWIS decking", "16mm LEWIS decking.", source_id=OUTER,
                          role="baseline", date=OLD_DATE, request_role="forwarded_request"),
                candidate("Decking", "16mm LEWIS decking", "16mm LEWIS decking.",
                          role="baseline", date=OLD_DATE, request_role="forwarded_request"),
                candidate("Decking", "plywood deck", "The build-up includes a plywood deck.")]
    raw, provenance, issues = resolve_email_evidence(extraction(sources, evidence))
    assert raw["Decking"] == "plywood deck" and not issues
    assert provenance["Decking"]["selected"]["observedAt"] == "2026-11-16T10:45:51+00:00"
    assert len(provenance["Decking"]["excludedCandidates"]) == 1


@pytest.mark.parametrize("body", ["Earlier correspondence for information.",
    "Please ignore the attached email.", "Please see attached for information only."])
def test_attached_message_requires_positive_adoption_and_is_not_a_new_request(body):
    sources = request_sources(outer_body=body)
    assert not adopted_request_contexts(sources)
    raw, _, issues = resolve_email_evidence(extraction(sources, [candidate("Decking", "plywood deck", "plywood deck")]))
    assert raw["Decking"] is None and not issues
    assert select_client_request(sources)["sourceId"] == OUTER


@pytest.mark.parametrize("alteration", ["missing_parent", "grafted_parent", "cycle", "unmarked_body", "future_message"])
def test_invalid_attached_provenance_cannot_supply_an_adopted_fact(alteration):
    sources = request_sources()
    if alteration == "missing_parent":
        sources[1]["parentSourceId"] = "missing:body"
    elif alteration == "grafted_parent":
        sources[1]["sourceId"] = "another:attachment:0:body"
    elif alteration == "cycle":
        sources[0]["parentSourceId"] = CHILD
    elif alteration == "unmarked_body":
        sources[1].pop("attachedEmail")
    else:
        sources[1]["text"] = sources[1]["text"].replace(CHILD_DATE, "Tue, 17 Nov 2026 10:45:51 +0000")
    evidence = candidate("Decking", "plywood deck", "plywood deck", source_id=sources[1]["sourceId"])
    raw, _, _ = resolve_email_evidence(extraction(sources, [evidence]))
    assert raw["Decking"] is None and not adopted_request_contexts(sources)


def test_multiple_adopted_messages_do_not_choose_a_drawing_or_deck_by_timestamp():
    sources = request_sources(child_body=f"Follow {REFERENCE}. Use plywood deck.")
    second_id = "asset:1:attachment:1:body"
    second_date = "Mon, 16 Nov 2026 10:46:51 +0000"
    sources.append(email(second_id, f"Follow {OLD_REFERENCE}. Use concrete deck.",
        sender="Architect <architect@designer.example>", date=second_date, parent=OUTER))
    evidence = [candidate("Drawing Reference", REFERENCE, f"Follow {REFERENCE}.", role="baseline"),
                candidate("Drawing Reference", OLD_REFERENCE, f"Follow {OLD_REFERENCE}.", source_id=second_id,
                          role="baseline", date=second_date),
                candidate("Decking", "plywood deck", "Use plywood deck."),
                candidate("Decking", "concrete deck", "Use concrete deck.", source_id=second_id, date=second_date)]
    baselines, issue = resolve_baseline_drawing_identity(extraction(sources, evidence))
    assert len(baselines) == 2 and issue == VALUE_CONFLICT
    raw, _, issues = resolve_email_evidence(extraction(sources, evidence))
    assert raw["Decking"] is None and issues["Decking"] == VALUE_CONFLICT


def test_outer_explicit_current_override_takes_priority_over_adopted_attachment():
    sources = request_sources(outer_body="Please amend as per attached. Use concrete deck.")
    evidence = [candidate("Decking", "plywood deck", "The build-up includes a plywood deck."),
                candidate("Decking", "concrete deck", "Use concrete deck.", source_id=OUTER,
                          date=DATE, request_role="direct_request")]
    raw, _, issues = resolve_email_evidence(extraction(sources, evidence))
    assert raw["Decking"] == "concrete deck" and not issues


def test_current_priority_does_not_merge_or_hide_different_roof_scopes():
    sources = request_sources(outer_body="Please amend as per attached. Roof 1: concrete deck.",
                              child_body="Roof 2: plywood deck.")
    evidence = [candidate("Decking", "concrete deck", "Roof 1: concrete deck.", source_id=OUTER,
                          date=DATE, request_role="direct_request", scope="Roof 1"),
                candidate("Decking", "plywood deck", "Roof 2: plywood deck.", scope="Roof 2")]
    data = extraction(sources, evidence)
    assert resolve_email_evidence(data)[2]["Decking"] == VALUE_CONFLICT
    raw, _, issues = resolve_email_evidence(data, ("Roof 2",))
    assert raw["Decking"] == "plywood deck" and not issues


def test_nested_cid_image_is_attributed_to_owning_message_and_not_quoted_signature():
    sources = request_sources(child_body="Current [cid:new-details]\n" + history() + "\n[cid:old-details]")
    for index, cid in enumerate(("new-details", "old-details")):
        sources.append({"sourceId": f"asset:1:attachment:0:attachment:{index}", "sourceType": "attachment",
            "status": "read", "filename": "details.png", "contentSha256": str(index), "text": "Deck: concrete",
            "observedAt": sources[1]["observedAt"], "contentId": cid, "parentSourceId": CHILD})
    evidence = [candidate("Decking", "concrete", "Deck: concrete", source_id=sources[index]["sourceId"])
                for index in (2, 3)]
    raw, provenance, issues = resolve_email_evidence(extraction(sources, evidence))
    assert raw["Decking"] == "concrete" and not issues
    assert len(provenance["Decking"]["candidates"]) == 1
    assert len(provenance["Decking"]["excludedCandidates"]) == 1


def test_an_email_inside_an_attached_archive_preserves_email_owner_and_adoption():
    sources = request_sources()
    archive_id = "asset:1:attachment:0"
    sources[1]["sourceId"] = archive_id + ":member:0:body"
    sources[1]["parentSourceId"] = archive_id
    sources.append({"sourceId": archive_id, "sourceType": "attachment", "status": "expanded:archive",
        "parentSourceId": OUTER, "text": "", "filename": "messages.zip", "contentSha256": "x"})
    source_map = {entry["sourceId"]: entry for entry in sources}
    assert parent_email(sources[1], source_map)["sourceId"] == OUTER
    assert adopted_request_contexts(sources)[0]["sourceId"] == sources[1]["sourceId"]


def test_request_notes_keep_adopted_current_constraints_and_exclude_nested_quotes():
    sources = request_sources(child_body="Keep 150mm within the cassette.\n" + history())
    notes = [{"source_id": CHILD, "snippet": text, "category": "constraint", "scope": None}
             for text in ("Keep 150mm within the cassette.", "16mm LEWIS decking.")]
    resolved = resolve_request_notes(extraction(sources, [], specificationRequestNotes=notes))
    assert [entry["snippet"] for entry in resolved] == ["Keep 150mm within the cassette."]


def test_a_pointer_only_in_quoted_outer_history_cannot_adopt_an_attached_request():
    sources = request_sources(outer_body="This is prior correspondence for information.")
    sources[0]["text"] += "\nPlease amend as per the attached."
    assert adopted_request_contexts(sources) == []


def test_recursive_adoption_requires_each_containing_message_to_adopt_its_child():
    sources = request_sources(child_body="Please see attached for the revised build-up.")
    grandchild_id = "asset:1:attachment:0:attachment:0:body"
    grandchild_date = "Mon, 16 Nov 2026 10:40:00 +0000"
    sources.append(email(grandchild_id, "Use plywood deck.", date=grandchild_date,
                         sender="Engineer <engineer@designer.example>", parent=CHILD))
    data = extraction(sources, [candidate("Decking", "plywood deck", "Use plywood deck.",
                                         source_id=grandchild_id, date=grandchild_date)])
    assert resolve_email_evidence(data)[0]["Decking"] == "plywood deck"
    sources[1]["text"] = sources[1]["text"].replace(
        "Please see attached for the revised build-up.", "Previous calculations for information.")
    assert resolve_email_evidence(data)[0]["Decking"] is None


def test_undated_nested_document_does_not_acquire_its_carrier_email_fact_date():
    sources = request_sources()
    document_id = "asset:1:attachment:0:attachment:0"
    sources.append({"sourceId": document_id, "sourceType": "attachment", "status": "read",
                    "filename": "build-up.txt", "text": "Deck: concrete", "contentSha256": "a",
                    "observedAt": sources[1]["observedAt"], "parentSourceId": CHILD})
    data = extraction(sources, [candidate("Decking", "concrete", "Deck: concrete", source_id=document_id, date=None)])
    raw, provenance, issues = resolve_email_evidence(data)
    assert raw["Decking"] == "concrete" and not issues
    assert provenance["Decking"]["selected"]["observedAt"] is None


def test_another_explicit_current_attachment_is_not_silently_displaced_by_attached_email():
    sources = request_sources()
    document_id = "asset:1:attachment:1"
    sources.append({"sourceId": document_id, "sourceType": "attachment", "status": "read",
                    "filename": "amendment.txt", "text": "Use concrete deck", "contentSha256": "a",
                    "observedAt": sources[0]["observedAt"], "parentSourceId": OUTER})
    evidence = [candidate("Decking", "plywood deck", "The build-up includes a plywood deck."),
                candidate("Decking", "concrete deck", "Use concrete deck", source_id=document_id, date=None)]
    raw, _, issues = resolve_email_evidence(extraction(sources, evidence))
    assert raw["Decking"] is None and issues["Decking"] == VALUE_CONFLICT


def adopted_thread_sources(*, outer_body="Please see the link below and email attached. Could you design this roof?",
                           quoted_sender="architect@designer.example", quoted_subject="[External] Re: Existing roof",
                           quoted_date=OLD_DATE, child_body="Can you start the roof layout using our drawings?"):
    quoted = (f"\n\nFrom: {quoted_sender}\nTo: client@manufacturer.example\n"
              f"Subject: {quoted_subject}\nSent: {quoted_date}\n\n"
              "Target U-value: 0.18 W/m2K.\n\n[Image: cid:roof-detail]")
    return request_sources(outer_body=outer_body, child_body=child_body + quoted)


@pytest.mark.parametrize("ownership", ["unique", "ambiguous", "wrong_date", "outlook_date"])
def test_explicitly_adopted_thread_supports_verified_text_and_uniquely_owned_inline_detail(ownership):
    quoted_date = "12/11/2026 13:52 (GMT+00:00)" if ownership == "outlook_date" else OLD_DATE
    sources = adopted_thread_sources(child_body="See [Image: cid:roof-detail]" if ownership == "ambiguous"
                                     else "Can you start the roof layout using our drawings?",
                                     quoted_date=quoted_date)
    detail = {"sourceId": "asset:1:attachment:0:attachment:0", "sourceType": "attachment", "status": "read",
              "filename": "detail.png", "contentSha256": "a" * 64, "text": "Scale: 1:10",
              "observedAt": sources[1]["observedAt"], "contentId": "roof-detail", "parentSourceId": CHILD}
    sources.append(detail)
    target = candidate("Target U Value", "0.18", "Target U-value: 0.18 W/m2K.",
                       date=quoted_date, request_role="forwarded_request")
    scale = candidate("Scale", "1:10", "Scale: 1:10", source_id=detail["sourceId"],
                      date=CHILD_DATE if ownership == "wrong_date" else quoted_date)
    raw, provenance, issues = resolve_email_evidence(extraction(sources, [target, scale]))
    assert raw["Target U Value"] == "0.18"
    assert provenance["Target U Value"]["selected"]["observedAt"] == "2026-11-12T13:52:00+00:00"
    if ownership in {"unique", "outlook_date"}:
        assert raw["Scale"] == "1:10" and not issues
        assert provenance["Scale"]["selected"]["attachedMessageSourceId"] == CHILD
    elif ownership == "ambiguous":
        assert raw["Scale"] is None and not issues
        assert provenance["Scale"]["excludedCandidates"]
    else:
        assert raw["Scale"] is None and "Scale" in issues
        assert provenance["Scale"]["rejectedCandidates"][0]["status"] == "unverified"


@pytest.mark.parametrize("change", [
    {"outer_body": "Please see the attachment. Could you design this roof?"},
    {"outer_body": "Please see the attached email. Ignore the earlier replies."},
    {"quoted_sender": "unrelated@designer.example"},
    {"quoted_subject": "Unrelated building"},
    {"quoted_date": "Fri, 20 Nov 2026 13:52:00 +0000"},
])
def test_quoted_claim_without_verified_thread_relationship_stays_excluded(change):
    sources = adopted_thread_sources(**change)
    target = candidate("Target U Value", "0.18", "Target U-value: 0.18 W/m2K.", date=None)
    raw, provenance, issues = resolve_email_evidence(extraction(sources, [target]))
    assert raw["Target U Value"] is None
    assert provenance["Target U Value"]["excludedCandidates"] and not issues


def test_attached_head_overrides_adopted_thread_without_inheriting_its_notes():
    sources = adopted_thread_sources(child_body="Target U-value: 0.20 W/m2K.")
    target_earlier = candidate("Target U Value", "0.18", "Target U-value: 0.18 W/m2K.", date=OLD_DATE)
    target_head = candidate("Target U Value", "0.20", "Target U-value: 0.20 W/m2K.")
    note = {"source_id": CHILD, "snippet": target_earlier["snippet"], "category": "constraint", "scope": None}
    data = extraction(sources, [target_earlier, target_head], specificationRequestNotes=[note])
    raw, provenance, issues = resolve_email_evidence(data)
    assert raw["Target U Value"] == "0.20"
    assert len(provenance["Target U Value"]["candidates"]) == 2 and not issues
    assert resolve_request_notes(data) == []
