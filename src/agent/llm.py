"""The model boundary. Agents talk to an `LLM`; production uses Claude, tests use
a scripted fake, so the whole graph runs offline at zero cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agent import config


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int = 0
    output_tokens: int = 0
    # The assistant content exactly as the API returned it, so the tool-use loop
    # can send it back on the next turn.
    raw_content: list[dict[str, Any]] = field(default_factory=list)


class LLM(Protocol):
    model: str

    def complete(
        self,
        *,
        agent: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...


class ClaudeLLM:
    """Anthropic Messages API with tool use."""

    def __init__(self, model: str | None = None) -> None:
        import anthropic  # imported here so tests never need a key

        self.model = model or config.MODEL
        self._client = anthropic.Anthropic()

    def complete(self, *, agent, system, messages, tools=None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": config.MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        resp = self._client.messages.create(**kwargs)
        text_parts, calls, raw = [], [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
                raw.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input)))
                raw.append({"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input)})
        return LLMResponse(
            text="\n".join(text_parts),
            tool_calls=calls,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            raw_content=raw,
        )
