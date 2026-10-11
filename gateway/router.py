"""Decide which alias handles a request when the client asks for model "auto".

This is a deliberately simple rules-based router. It's the baseline that the
learned classifier (step 5) has to beat in the evals.
"""

from __future__ import annotations

import re
from typing import Any

from gateway.config import RoutingConfig
from gateway.usage import message_text


class RulesRouter:
    def __init__(self, cfg: RoutingConfig):
        self.cfg = cfg
        # Whole-word matches only, so "code" doesn't fire on "decode".
        self.keyword_patterns = [
            (kw, re.compile(rf"\b{re.escape(kw.lower())}\b")) for kw in cfg.smart_keywords
        ]

    def route(self, messages: list[dict[str, Any]]) -> tuple[str, str]:
        """Return (alias, reason)."""
        text = message_text(messages)
        if len(text) > self.cfg.long_prompt_chars:
            return "smart", "rule:long_prompt"

        turns = sum(1 for m in messages if m.get("role") == "user")
        if turns > self.cfg.long_conversation_turns:
            return "smart", "rule:long_conversation"

        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"), None
        )
        last_text = message_text([last_user]).lower() if last_user else ""
        if "```" in last_text:
            return "smart", "rule:code_block"
        for kw, pattern in self.keyword_patterns:
            if pattern.search(last_text):
                return "smart", f"rule:keyword:{kw}"

        return "fast", "rule:default"
