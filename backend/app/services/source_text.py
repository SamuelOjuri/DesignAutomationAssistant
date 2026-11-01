"""Text normalization before prompting, evidence checks and JSON persistence."""


def clean_source_text(text: str) -> str:
    # NUL is not representable as PostgreSQL text. A separator preserves token
    # boundaries (1<NUL>50 must never become 150). Literal '\\u0000' stays literal.
    return text.replace("\x00", " ")


def require_json_safe_text(value):
    """Reject model-introduced NULs rather than silently changing validated facts."""
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("Extraction response contains a NUL character")
    elif isinstance(value, dict):
        for key, child in value.items():
            require_json_safe_text(key)
            require_json_safe_text(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            require_json_safe_text(child)
