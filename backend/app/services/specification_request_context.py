"""Literal message boundaries and client identity, independent of model claims."""

from datetime import datetime, timezone
from email.utils import getaddresses
import re
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse, parse

from ..config import settings
from .legacy_enquiry.source_analysis_failures import SourceAnalysisReviewRequired
from .specification_semantics import ARCHIVE_EXPANDED_STATUS, DOCX_EXPANDED_STATUS


CLIENT_FIELDS = frozenset({"Company", "Contact", "Date Received"})
ADOPTED_THREAD_PRIORITY = 1
ADOPTED_MESSAGE_PRIORITY = 2
CURRENT_REQUEST_PRIORITY = 3
_OUTLOOK_GMT_DATE = re.compile(
    r"\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s+\(GMT[+-]\d{2}:\d{2}\)", re.I,
)


def _addresses(values):
    # Outlook's plain-text alternative repeats linked addresses as <mailto:...>.
    cleaned = [re.sub(r"<mailto:[^>]*>", "", value, flags=re.I) for value in values]
    addresses = []
    for value in cleaned:
        # Some forwarded Outlook headers repeat the mailbox as its display
        # name. Strict parsing rejects the unquoted address before the brackets.
        repeated = re.fullmatch(r"([^<>\s,;@]+@[^<>\s,;@]+)\s+<\1>", value.strip(), re.I)
        if repeated:
            value = repeated[1]
        parsed = getaddresses([value])
        if not any("@" in address for _, address in parsed) and ";" in value:
            # Outlook separates displayed recipients with semicolons, which
            # strict RFC parsers reject. Quoted names/local parts stay literal;
            # valid RFC groups already parsed successfully above.
            quoted = escaped = False
            angle_depth = 0
            characters = []
            for character in value:
                if escaped:
                    escaped = False
                elif character == "\\" and quoted:
                    escaped = True
                elif character == '"':
                    quoted = not quoted
                elif not quoted:
                    if character == "<":
                        angle_depth += 1
                    elif character == ">":
                        angle_depth = max(0, angle_depth - 1)
                    elif character == ";" and not angle_depth:
                        character = ","
                characters.append(character)
            parsed = getaddresses(["".join(characters)])
        addresses.extend(address.lower() for _, address in parsed if "@" in address)
    return list(dict.fromkeys(addresses))


def parse_outlook_gmt_date(value: str) -> datetime | None:
    """Recognize Outlook's numeric UK date and its explicit GMT offset."""
    value = value.strip()
    if _OUTLOOK_GMT_DATE.fullmatch(value):
        return datetime.strptime(value, "%d/%m/%Y %H:%M (GMT%z)")
    return None


def message_timestamp(value):
    if not value or not re.search(r"\b(?:19|20)\d{2}\b", value):
        return None
    value = value.strip()
    try:
        stamp = parse_outlook_gmt_date(value)
        if stamp is None:
            stamp = isoparse(value) if re.match(r"^\d{4}-\d{2}-\d{2}(?:T|\s|$)", value) else parse(value, dayfirst=True)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo("Europe/London"))
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def email_messages(text):
    """Only header runs beginning with From; body dates cannot create headers."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Apple reply headers are boundaries too, even when they cannot establish
    # recipients for selecting a new client request.
    apple = r"On (.+?), at (\d{1,2}:\d{2}(?::\d{2})?), (.+?<[^>]+>) wrote:"
    # Outlook HTML uses non-breaking spaces after header labels. Recognize
    # those delimiters without rewriting the source text used for citations.
    starts = [match.start() for match in re.finditer(r"(?mi)^(?:From:(?:[ \t\u00a0]|\n)|" + apple + r")", text)]
    messages = []
    for index, start in enumerate(starts):
        block = text[start:starts[index + 1] if index + 1 < len(starts) else len(text)]
        reply = re.match(apple, block, re.I)
        if reply:
            senders = _addresses([reply[3]])
            date = reply[1] + " " + reply[2]
            messages.append({"messageIndex": index, "sender": senders[0] if len(senders) == 1 else None,
                "recipients": [], "sentDate": date, "sentAt": message_timestamp(date),
                "requestRole": "forwarded_request", "headerSnippet": reply[0], "text": block})
            continue
        headers = []
        values = {}
        after_blank = False
        cursor = header_end = 0
        for literal_line in block.splitlines(keepends=True):
            cursor += len(literal_line)
            line = literal_line.rstrip("\n")
            if not line.strip():
                after_blank = True
                continue  # Outlook puts blank lines between forwarded headers.
            match = re.match(r"^(From|To|Cc|Bcc|Subject|Date|Sent|Importance):[ \t\u00a0]*(.*)$", line, re.I)
            if match:
                key = match[1].lower()
                values.setdefault(key, []).append(match[2])
                headers.append(line)
                header_end = cursor
                after_blank = False
            elif (headers and not values[key][-1] and
                  ((_addresses([line]) if key in {"from", "to", "cc", "bcc"} else
                    message_timestamp(line) if key in {"date", "sent"} else False))):
                # HTML layout can put an address/date below its empty label,
                # with or without indentation. Only recognizable header values
                # can fill that slot; ordinary body lines cannot extend it.
                headers[-1] += "\n" + line
                values[key][-1] = line.strip()
                header_end = cursor
                after_blank = False
            elif line[0].isspace() and headers and not after_blank:
                headers[-1] += "\n" + line
                # Folded recipient lines matter when selecting a client request.
                values[key][-1] += " " + line.strip()
                header_end = cursor
            else:
                break
        dates = values.get("date", []) + values.get("sent", [])
        senders = _addresses(values.get("from", []))
        recipients = _addresses(values.get("to", []) + values.get("cc", []))
        if len(dates) != 1 or len(senders) > 1:
            continue
        messages.append({
            "messageIndex": index, "sender": senders[0] if senders else None, "recipients": recipients,
            "sentDate": dates[0], "sentAt": message_timestamp(dates[0]),
            "requestRole": "direct_request" if index == 0 else "forwarded_request",
            "headerSnippet": block[:header_end].rstrip("\n"), "text": block,
        })
    return messages


def source_email_messages(source, *, include_plain=False):
    """Associate facts with literal message boundaries in both MIME alternatives."""
    html = source.get("emailHtmlText")
    # HTML owns structural attribution when present. Plain text flattens a
    # gateway footer into the last forwarded message and loses inline CIDs.
    variants = ([source["text"], html] if include_plain else [html]) if html else [source["text"]]
    messages = []
    for text in variants:
        parsed = email_messages(text)
        footer = source.get("emailOuterFooterText")
        if text == html and footer and text.endswith(footer) and parsed:
            # This footer is a sibling of the outer HTML mail container, not
            # part of its last forwarded message. Preserve the original text.
            normalized_footer = footer.replace("\r\n", "\n").replace("\r", "\n")
            parsed[-1]["text"] = parsed[-1]["text"].removesuffix(normalized_footer)
            parsed[0]["text"] += "\n" + normalized_footer
        messages.extend(parsed)
    return messages


def internal_address(address):
    domain = str(address or "").rsplit("@", 1)[-1].lower()
    domains = {part.strip().lower() for part in settings.design_processing_internal_email_domains.split(",") if part.strip()}
    return domain in domains


def select_client_request(manifest):
    """Select the latest external message sent to the configured organisation."""
    candidates = []
    for source in manifest:
        if (source.get("sourceType") != "email_body" or source.get("status") != "read"
                or source.get("parentSourceId") or source.get("attachedEmail")):
            continue
        for message in source_email_messages(source):
            if (message["sender"] and not internal_address(message["sender"])
                    and any(internal_address(a) for a in message["recipients"])):
                candidates.append({**message, "sourceId": source["sourceId"]})
    if not candidates:
        return None  # Legacy/unidentified sources retain their existing checks.
    undated = next((message for message in candidates if not message["sentAt"]), None)
    if undated is not None:
        raise SourceAnalysisReviewRequired(
            "Client request has an unreadable sent date; chronology requires review",
            code="unreadable_request_date", source_id=undated["sourceId"],
            message_index=undated["messageIndex"],
        )
    latest = max(message["sentAt"] for message in candidates)
    current = [message for message in candidates if message["sentAt"] == latest]
    if len({message["sender"] for message in current}) != 1:
        raise SourceAnalysisReviewRequired(
            "Simultaneous client requests have conflicting senders",
            code="conflicting_request_senders", candidate_count=len(current),
        )
    # Prefer the original outer email over an otherwise identical quoted copy.
    return min(current, key=lambda message: (message["messageIndex"], message["sourceId"]))


def internal_only_correspondence(manifest):
    """Every outer email is staff-sent and no external client request exists.

    A client named inside internal correspondence is not a verified requester,
    so client identity and chronology stay unset for reviewer confirmation.
    """
    outer = [source for source in manifest if source.get("sourceType") == "email_body"
             and not source.get("parentSourceId") and not source.get("attachedEmail")]
    # Outer email text starts with its own headers; messageIndex 0 is that header.
    return bool(outer) and all(
        (messages := email_messages(str(source.get("text") or "")))
        and messages[0]["messageIndex"] == 0 and internal_address(messages[0]["sender"])
        for source in outer
    ) and select_client_request(manifest) is None


def request_context_summary(context):
    return {key: value for key, value in context.items() if key != "text"} if context else None


def source_parent_chain(source, sources):
    """Validate explicit MIME, archive and embedded Word drawing ownership."""
    child, visited, chain = source, {source.get("sourceId")}, []
    for _ in range(16):
        parent_id = child.get("parentSourceId")
        if parent_id is None:
            return chain
        parent = sources.get(parent_id) if isinstance(parent_id, str) else None
        if parent is None or parent_id in visited:
            return None
        visited.add(parent_id)
        is_body = parent.get("sourceType") == "email_body" and parent.get("status") == "read"
        is_attachment = parent.get("sourceType") == "attachment"
        is_archive = is_attachment and parent.get("status") == ARCHIVE_EXPANDED_STATUS
        is_docx_drawing = (is_attachment and parent.get("status") in {"read", DOCX_EXPANDED_STATUS}
                           and str(parent.get("filename", "")).lower().endswith(".docx")
                           and child.get("sourceType") == "attachment" and child.get("docxDrawingPart"))
        prefix = (parent_id.removesuffix(":body") + ":attachment:" if is_body
                  else parent_id + ":member:" if is_archive
                  else parent_id + ":drawing:" if is_docx_drawing else "")
        # Every provenance edge follows a deterministic source ID.
        if not prefix or not child.get("sourceId", "").startswith(prefix):
            return None
        chain.append(parent)
        child = parent
    return None


def parent_email(source, sources):
    identity = source.get("sourceId", "")
    if source.get("parentSourceId") is not None:
        chain = source_parent_chain(source, sources)
        return next((parent for parent in chain if parent.get("sourceType") == "email_body"), None) if chain is not None else None
    if source.get("sourceType") != "attachment" or ":attachment:" not in identity:
        return None
    parent = sources.get(identity.split(":attachment:", 1)[0] + ":body")
    return parent if parent and parent.get("status") == "read" and parent.get("sourceType") == "email_body" else None


def _head_message(source):
    heads = [message for message in source_email_messages(source) if message["messageIndex"] == 0]
    return heads[0] if len(heads) == 1 else None


def _adopts_attachments(message):
    text = message["text"].removeprefix(message["headerSnippet"])
    if (re.search(r"\b(?:ignore|disregard|do not use|don't use)\b.{0,100}\battach", text, re.I | re.S)
            or re.search(r"\battach\w*\b.{0,80}\b(?:for information only|fyi only|do not use)\b", text, re.I | re.S)):
        return False
    return bool(re.search(
        r"\b(?:see|refer to|review|amend|revise|update|use|follow|as per|in accordance with)\b"
        r".{0,120}\battach(?:ed|ment)s?\b", text, re.I | re.S))


def adopted_attached_message(source, sources, context=None):
    """An attached email's own current message, explicitly adopted by its parent.

    Parent links and literal request pointers establish applicability; neither
    a newer attachment date nor a model's direct-request label can do so.
    """
    context = context or select_client_request(list(sources.values()))
    if context is None:
        return None
    body = source if source.get("sourceType") == "email_body" else parent_email(source, sources)
    if not body or not body.get("attachedEmail"):
        return None
    current = body
    visited = set()
    head = _head_message(body)
    if not head or not head["sender"] or not head["sentAt"]:
        return None
    for _ in range(4):
        identity = current.get("sourceId")
        if identity in visited or not current.get("attachedEmail"):
            return None
        visited.add(identity)
        parent = parent_email(current, sources)
        if parent is None:
            return None
        parent_message = (context if parent["sourceId"] == context["sourceId"] else _head_message(parent))
        current_head = _head_message(current)
        if (not parent_message or not current_head or not current_head["sentAt"]
                or not parent_message["sentAt"] or current_head["sentAt"] > parent_message["sentAt"]
                or not _adopts_attachments(parent_message)):
            return None
        if parent["sourceId"] == context["sourceId"]:
            return {**head, "sourceId": body["sourceId"], "parentSourceId": body["parentSourceId"],
                    "adoptedBySourceId": context["sourceId"]}
        current = parent
    return None


def adopted_request_contexts(manifest):
    sources = {source["sourceId"]: source for source in manifest}
    context = select_client_request(manifest)
    return [message for source in manifest
            if source.get("sourceType") == "email_body" and source.get("attachedEmail")
            and (message := adopted_attached_message(source, sources, context))]


def _thread_subject(message):
    subject = re.search(r"^Subject:[ \t\u00a0]*(.+)$", message["headerSnippet"], re.I | re.M)
    if not subject:
        return ""
    value = re.sub(r"^(?:(?:re|fw|fwd):\s*|\[external\]\s*)+", "", subject[1], flags=re.I)
    return " ".join(value.casefold().split())


def _is_adopted_thread_reply(message, adopted, context):
    body = context["text"].removeprefix(context["headerSnippet"])
    # Generic "see attached" establishes the attached message's current head.
    # Its quoted thread needs an explicit pointer to attached correspondence.
    if not re.search(r"\b(?:attached\s+(?:e-?mail|message|thread|correspondence)|"
                     r"(?:e-?mail|message|thread|correspondence)\s+(?:is\s+)?attached)\b", body, re.I):
        return False
    if re.search(r"\b(?:ignore|disregard|do not use|don't use)\s+(?:the\s+)?"
                 r"(?:earlier|previous|prior|older|quoted)\s+(?:e-?mails?|messages?|correspondence|history|replies)\b",
                 body, re.I):
        return False
    client = context["sender"]
    if client == adopted["sender"]:
        recipients = set(adopted["recipients"]) - {client}
        if len(recipients) != 1:
            return False
        peer = next(iter(recipients))
    else:
        if client not in adopted["recipients"]:
            return False
        peer = adopted["sender"]
    if not ((message["sender"] == client and peer in message["recipients"])
            or (message["sender"] == peer and client in message["recipients"])):
        return False
    subject = _thread_subject(adopted)
    return bool(message["messageIndex"] > 0 and message["sentAt"] and message["sentAt"] < adopted["sentAt"]
                and subject and _thread_subject(message) == subject)


def technical_evidence_context(evidence, source, sources, context, contains):
    """Return applicability priority and verified own-message attribution.

    None means attached correspondence has no applicable source message.
    An explicitly adopted, attributable thread reply ranks below the current
    message and cannot establish requester identity or request notes.
    """
    if source.get("parentSourceId") is not None and source_parent_chain(source, sources) is None:
        return None
    body = source if source.get("sourceType") == "email_body" else parent_email(source, sources)
    if ((source.get("sourceType") == "email_body" and (source.get("attachedEmail") or source.get("parentSourceId")))
            or body and body.get("attachedEmail")):
        adopted = adopted_attached_message(source, sources, context)
        if adopted is None:
            return None
        if source.get("sourceType") == "email_body":
            if contains(evidence["snippet"], adopted["text"]):
                return ADOPTED_MESSAGE_PRIORITY, adopted
            matches = [message for message in source_email_messages(body)
                       if contains(evidence["snippet"], message["text"])]
        elif source.get("contentId"):
            containing = attachment_message(source, sources)
            if containing and containing["messageIndex"] == 0:
                return ADOPTED_MESSAGE_PRIORITY, adopted
            matches = [containing] if containing else []
        else:
            return ADOPTED_MESSAGE_PRIORITY, adopted
        if (context and evidence.get("field") and len(matches) == 1
                and _is_adopted_thread_reply(matches[0], adopted, context)):
            return ADOPTED_THREAD_PRIORITY, {**matches[0], "sourceId": adopted["sourceId"]}
        return None
    if (context and source.get("sourceId") == context["sourceId"]
            and contains(evidence["snippet"], context["text"])):
        return CURRENT_REQUEST_PRIORITY, context
    if (context and body and body.get("sourceId") == context["sourceId"]
            and source.get("sourceType") == "attachment" and _adopts_attachments(context)):
        containing = attachment_message(source, sources) if source.get("contentId") else None
        if (not source.get("contentId") and evidence.get("role") == "requested"
                or containing and containing["sender"] == context["sender"]
                and containing["sentAt"] == context["sentAt"]):
            # Other explicitly requested documents in the same current envelope
            # remain peers of an adopted email, not silently displaced history.
            return ADOPTED_MESSAGE_PRIORITY, {**context, "sourceId": body["sourceId"]}
    return 0, None


def contains_content_id(text, content_id):
    content_id = str(content_id or "").strip("<>")
    return bool(content_id and re.search(r"(?i:cid:)" + re.escape(content_id) + r"(?=[>\]\s\"']|$)", text))


def attachment_messages(source, sources):
    parent = parent_email(source, sources)
    if not parent or not source.get("contentId"):
        return []
    matches = {}
    for message in source_email_messages(parent):
        if contains_content_id(message["text"], source["contentId"]):
            key = (message["messageIndex"], message["sender"], message["sentAt"], message["requestRole"])
            matches.setdefault(key, message)
    return list(matches.values())


def attachment_message(source, sources):
    parent = parent_email(source, sources)
    if parent and source.get("observedAt") != parent.get("observedAt"):
        return None
    messages = attachment_messages(source, sources)
    return messages[0] if len(messages) == 1 else None


def client_evidence_applies(evidence, source, sources, context, contains):
    """Company/contact/date must belong to the same selected client message."""
    parent = parent_email(source, sources)
    if (source.get("docxDrawingPart") or source.get("attachedEmail")
            or source.get("sourceType") == "email_body" and source.get("parentSourceId")
            or source.get("parentSourceId") is not None and source_parent_chain(source, sources) is None
            or parent and parent.get("attachedEmail")):
        return False  # Attached correspondents do not replace the outer requester.
    if context is None:
        parent = source if source.get("sourceType") == "email_body" else parent_email(source, sources)
        messages = source_email_messages(parent) if parent else []
        return (evidence["request_role"] == "direct_request"
                and not (messages and internal_address(messages[0]["sender"])))
    if source.get("sourceType") == "email_body":
        return any(_client_message_applies(evidence, message, context)
                   and contains(evidence["snippet"], message["text"])
                   for message in source_email_messages(source))
    if source.get("sourceType") == "reviewed_crm":
        return evidence["request_role"] == "direct_request"
    parent = parent_email(source, sources)
    if parent and source.get("contentId"):
        messages = attachment_messages(source, sources)
        return (any(_client_message_applies(evidence, message, context) for message in messages)
                and all(message["sender"] in {None, context["sender"]} for message in messages))
    # Explicit requester forms, including standalone source documents.
    return evidence["request_role"] == "direct_request" and (
        parent is None or parent["sourceId"] == context["sourceId"])


def _client_message_applies(evidence, message, context):
    if message["sender"] != context["sender"] or not message["sentAt"]:
        return False
    # An earlier signature from this exact sender can identify the requester;
    # it must not change Date Received or date another technical fact.
    return (message["sentAt"] == context["sentAt"] or
            (evidence["field"] in {"Company", "Contact"} and message["sentAt"] < context["sentAt"]))
