from __future__ import annotations

from .source_analysis_failures import SourceAnalysisReviewRequired


# Bound provider work even when an item has many individually readable assets.
MAX_CONTEXT_BATCHES = 8
# Preserve any complete specification excerpt across a chunk boundary.
MAX_EXCERPT_CHARS = 4_000


def source_context_parts(record):
    """Render one immutable source without changing the text used to verify citations."""
    heading = (
        f"SOURCE ID: {record['sourceId']}\nSOURCE TYPE: {record['sourceType']}\n"
        f"FILE: {record['filename']}\nCARRYING EMAIL TIMESTAMP (not the fact's date): {record['observedAt']}\n"
        + (f"PARENT SOURCE ID: {record['parentSourceId']}\n" if record.get("parentSourceId") else "")
        + ("ATTACHED EMAIL: separate message; its quoted history is not the current request.\n"
           if record.get("attachedEmail") else "")
        + (f"CONTENT-ID: {record['contentId']}\n" if record.get("contentId") else "")
        + (f"VERIFIED CONTAINING MESSAGE DATE: {record['containingMessage']['sentDate']}\n"
           if record.get("containingMessage") else "")
        + ("EMAIL CONTENT:\n" if record["sourceType"] == "email_body" else "ATTACHMENT CONTENT:\n")
    )
    content = record["text"]
    if record.get("emailHtmlText"):
        content += "\nHTML ALTERNATIVE OF THE SAME EMAIL (literal text and inline Content-IDs):\n" + record["emailHtmlText"]
    return heading, content


def source_context_review(manifest, *, limit, character_count, batch_limit):
    contributions = sorted((
        {"source_id": record["sourceId"], "character_count": len(record["text"]) + len(record.get("emailHtmlText", ""))}
        for record in manifest if record["status"] == "read"
    ), key=lambda entry: (-entry["character_count"], entry["source_id"]))
    return SourceAnalysisReviewRequired(
        "Combined source text exceeds extraction limit; review required",
        code="combined_source_text_limit", scope="item", character_count=character_count,
        character_limit=limit, batch_limit=batch_limit, largest_sources=contributions[:5],
    )


def bounded_source_contexts(manifest, *, limit, character_count):
    """Partition a large item into complete-source blocks or overlapping excerpts."""
    def review_required():
        return source_context_review(manifest, limit=limit, character_count=character_count,
                                     batch_limit=MAX_CONTEXT_BATCHES)

    contexts = []
    blocks = []
    size = 0
    for record in manifest:
        if record["status"] != "read":
            continue
        heading, content = source_context_parts(record)
        parts = (heading + content,)
        if len(parts[0]) > limit:
            continuation = "SOURCE EXCERPT: one part of this source; preserve its original source ID.\n"
            capacity = limit - len(continuation) - len(heading)
            if capacity < 2:
                raise review_required()
            parts = _source_excerpts(continuation + heading, content, capacity)
        for part in parts:
            if blocks and size + 2 + len(part) > limit:
                contexts.append("\n\n".join(blocks))
                if len(contexts) == MAX_CONTEXT_BATCHES:
                    raise review_required()
                blocks = []
                size = 0
            size += len(part) + (2 if blocks else 0)
            blocks.append(part)
    if blocks:
        contexts.append("\n\n".join(blocks))
    return tuple(contexts)


def _source_excerpts(heading, content, capacity):
    overlap = min(MAX_EXCERPT_CHARS, capacity // 4)
    start = 0
    while start < len(content):
        end = min(start + capacity, len(content))
        if end < len(content):
            line_end = content.rfind("\n", start + capacity // 2, end)
            if line_end >= 0:
                end = line_end + 1
        yield heading + content[start:end]
        if end == len(content):
            break
        start = end - overlap
