"""Token counting and cost estimation."""

from __future__ import annotations

from typing import Any

from gateway.config import Deployment, Price


def message_text(messages: list[dict[str, Any]]) -> str:
    """Flatten chat messages to plain text (handles string and multi-part content)."""
    parts: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(p.get("text", "") for p in content if isinstance(p, dict))
    return "\n".join(parts)


def estimate_tokens(text: str) -> int:
    """Rough rule of thumb: ~4 characters per token for English text.

    Only used when a provider doesn't report real usage numbers.
    """
    return (len(text) + 3) // 4


def estimate_cost(
    prices: dict[str, Price], dep: Deployment, prompt_tokens: int, completion_tokens: int
) -> float | None:
    """Estimated USD cost at list price, or None if we have no price for this model."""
    price = prices.get(dep.key)
    if price is None:
        return None
    return (prompt_tokens * price.input_per_m + completion_tokens * price.output_per_m) / 1_000_000
