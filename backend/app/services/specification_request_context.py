"""Literal message boundaries and client identity, independent of model claims."""

from datetime import timezone
from email.utils import getaddresses
import re
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse, parse

from ..config import settings


CLIENT_FIELDS = frozenset({"Company", "Contact", "Date Received"})


def _addresses(values):
    # Outlook's plain-text alternative repeats linked addresses as <mailto:...>.
    cleaned = [re.sub(r"<mailto:[^>]*>", "", value, flags=re.I) for value in values]
    addresses = []
    for value in cleaned:
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


def message_timestamp(value):
    if not value or not re.search(r"\b(?:19|20)\d{2}\b", value):
        return None
    try:
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
    starts = [match.start() for match in re.finditer(r"(?mi)^(?:From:(?:[ \t]|\n)|" + apple + r")", text)]
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
            match = re.match(r"^(From|To|Cc|Bcc|Subject|Date|Sent|Importance):[ \t]*(.*)$", line, re.I)
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
        if source.get("sourceType") != "email_body" or source.get("status") != "read":
            continue
        for message in source_email_messages(source):
            if (message["sender"] and not internal_address(message["sender"])
                    and any(internal_address(a) for a in message["recipients"])):
                candidates.append({**message, "sourceId": source["sourceId"]})
    if not candidates:
        return None  # Legacy/unidentified sources retain their existing checks.
    if any(not message["sentAt"] for message in candidates):
        raise ValueError("Client request has an unreadable sent date; chronology requires review")
    latest = max(message["sentAt"] for message in candidates)
    current = [message for message in candidates if message["sentAt"] == latest]
    if len({message["sender"] for message in current}) != 1:
        raise ValueError("Simultaneous client requests have conflicting senders")
    # Prefer the original outer email over an otherwise identical quoted copy.
    return min(current, key=lambda message: (message["messageIndex"], message["sourceId"]))


def request_context_summary(context):
    return {key: value for key, value in context.items() if key != "text"} if context else None


def parent_email(source, sources):
    identity = source.get("sourceId", "")
    if source.get("sourceType") != "attachment" or ":attachment:" not in identity:
        return None
    parent = sources.get(identity.split(":attachment:", 1)[0] + ":body")
    return parent if parent and parent.get("status") == "read" and parent.get("sourceType") == "email_body" else None


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
