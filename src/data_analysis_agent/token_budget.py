"""Token accounting for bounded conversation context.

The production provider does not currently expose its exact tokenizer in this
workspace.  ``TokenCounter`` therefore has two explicit modes: a local
transformers tokenizer (only exact for the matching model revision/template)
and a conservative deterministic estimate.  Callers must persist the mode.
"""

from __future__ import annotations

import math
import os
import json
from dataclasses import dataclass
from typing import Iterable

from vanna.core.storage import Message


class ContextCapacityError(ValueError):
    """Required context cannot fit without dropping authoritative state."""


@dataclass(frozen=True)
class TokenCount:
    value: int
    mode: str
    tokenizer_id: str


class TokenCounter:
    """Count messages without silently pretending an estimate is exact."""

    def __init__(self, tokenizer_path: str | None = None):
        self.tokenizer_path = tokenizer_path or os.getenv("DATA_ANALYSIS_TOKENIZER_PATH")
        self._tokenizer = None
        self.mode = "estimated"
        self.tokenizer_id = "fallback-compatible-counter"
        if self.tokenizer_path:
            try:
                from transformers import AutoTokenizer

                self._tokenizer = AutoTokenizer.from_pretrained(
                    self.tokenizer_path, local_files_only=True, use_fast=True
                )
            except Exception:
                # An unavailable or incompatible local tokenizer must never
                # prevent the Agent from serving a bounded request.
                self._tokenizer = None
            else:
                # A local tokenizer is not automatically the provider's
                # tokenizer.  Opt into the stronger label only when the
                # operator has verified model revision and chat template.
                self.mode = (
                    "exact"
                    if os.getenv("DATA_ANALYSIS_TOKENIZER_TRUST_EXACT") == "1"
                    else "local_tokenizer_estimate"
                )
                self.tokenizer_id = os.path.abspath(self.tokenizer_path)

    def count_text(self, text: str | None) -> int:
        value = text or ""
        if self._tokenizer is not None:
            return max(1, len(self._tokenizer(value, add_special_tokens=False)["input_ids"]))
        # Use one unit per non-ASCII character and two ASCII characters per
        # unit. This remains an estimate, not the provider's chat tokenizer.
        ascii_chars = sum(ord(c) < 128 for c in value)
        return max(1, len(value) - ascii_chars + math.ceil(ascii_chars / 2) + 4)

    def count_message(self, message: Message) -> int:
        content = self.count_text(message.content)
        role = self.count_text(message.role)
        tool_id = self.count_text(message.tool_call_id)
        tool_calls = 0
        if message.tool_calls:
            for call in message.tool_calls:
                payload = call.model_dump(mode="json")
                tool_calls += self.count_text(str(payload))
        return content + role + tool_id + tool_calls + 8

    def count_messages(self, messages: Iterable[Message]) -> int:
        return sum(self.count_message(message) for message in messages)

    def evidence(self, value: int) -> dict[str, object]:
        return {
            "tokens": max(0, int(value)),
            "tokenizer_mode": self.mode,
            "tokenizer_id": self.tokenizer_id,
        }

    def count_request(self, request) -> int:
        """Include system, Catalog, messages, tool definitions and wrappers."""
        payload = {
            "system": request.system_prompt,
            "messages": [m.model_dump(mode="json") for m in request.messages],
            "tools": [t.model_dump(mode="json") if hasattr(t, "model_dump") else t for t in request.tools or []],
        }
        return self.count_text(json.dumps(payload, ensure_ascii=False)) + 16


DEFAULT_TOKEN_COUNTER = TokenCounter()
