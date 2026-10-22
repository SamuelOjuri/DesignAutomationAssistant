from typing import Literal


ThinkingLevel = Literal["minimal", "low", "medium", "high"]


def validate_thinking_level(model: str, thinking_level: str) -> None:
    if thinking_level not in {"minimal", "low", "medium", "high"}:
        raise ValueError("thinking_level must be minimal, low, medium, or high")
    if (
        model.strip().removeprefix("models/") == "gemini-3.8-flash"
        and thinking_level == "minimal"
    ):
        raise ValueError(
            "gemini-3.8-flash does not support minimal thinking; "
            "choose low, medium, or high"
        )
