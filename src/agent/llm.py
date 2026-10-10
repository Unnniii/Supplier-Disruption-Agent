"""The model boundary. Agents talk to an `LLM`; which model sits behind it is a
setting (SDA_PROVIDER), not a code change.

Message format inside the agents is one neutral shape (Anthropic-style blocks:
text, tool_use, tool_result). Each adapter translates to its own API.

    GeminiLLM   free tier, Google AI Studio key        (default)
    OllamaLLM   local model, free, offline
    ClaudeLLM   Anthropic API, paid
    FakeLLM     rule-based, for tests (agent/fake.py)
    CappedLLM   wraps any of them and enforces the per-run call cap
"""

from __future__ import annotations

import json
import re
import time
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
    # What the tool-use loop sends back as the assistant turn. Adapters may add a
    # private block (type starting with "_") carrying their native object.
    raw_content: list[dict[str, Any]] = field(default_factory=list)


class LLM(Protocol):
    model: str

    def complete(self, *, agent: str, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None) -> LLMResponse: ...


class CapExceeded(RuntimeError):
    pass


class CappedLLM:
    """Stops a run after MAX_LLM_CALLS_PER_RUN calls, whatever the backend."""

    def __init__(self, inner: LLM, max_calls: int | None = None) -> None:
        self.inner = inner
        self.model = inner.model
        self.max_calls = max_calls if max_calls is not None else config.MAX_LLM_CALLS_PER_RUN
        self.calls = 0

    def complete(self, **kwargs) -> LLMResponse:
        if self.calls >= self.max_calls:
            raise CapExceeded(f"call cap of {self.max_calls} reached for this run")
        self.calls += 1
        return self.inner.complete(**kwargs)


def _tool_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    """tool_use id -> tool name, so tool results can be labelled by name."""
    names = {}
    for m in messages:
        if isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
    return names


class QuotaExhausted(RuntimeError):
    """The provider's daily quota is used up. Retrying wastes time; stop the run."""


def _status(e: Exception) -> int | None:
    status = getattr(e, "code", None) or getattr(e, "status_code", None)
    if isinstance(status, int):
        return status
    head = str(e)[:200]
    for code in (429, 503):
        if str(code) in head:
            return code
    return None


def _daily_quota_hit(e: Exception) -> bool:
    """A per-day quota 429 (Gemini: quotaId ...PerDay..., or a retry delay in hours)."""
    msg = str(e)
    return "PerDay" in msg or bool(re.search(r"retry in \d+h", msg))


def _retry_on_rate_limit(fn):
    """Per-minute 429 and 503 'overloaded' are temporary: wait and retry.
    A per-day 429 is not: raise QuotaExhausted at once so the run stops cleanly."""
    for attempt in range(config.RATE_LIMIT_RETRIES + 1):
        try:
            return fn()
        except Exception as e:  # SDKs use different exception classes
            status = _status(e)
            if status == 429 and _daily_quota_hit(e):
                m = re.search(r"retry in ([\dhms.]+)", str(e))
                when = f"; it resets in about {m.group(1)}" if m else ""
                raise QuotaExhausted(f"daily free-tier quota for this model is used up{when}") from e
            if status not in (429, 503) or attempt == config.RATE_LIMIT_RETRIES:
                raise
            wait = min(60, 5 * 2 ** attempt)
            reason = "rate limited by the free tier" if status == 429 else "model overloaded (503)"
            print(f"    {reason}; waiting {wait}s and retrying "
                  f"({attempt + 1}/{config.RATE_LIMIT_RETRIES})", flush=True)
            time.sleep(wait)


# --- Gemini (free tier) ---------------------------------------------------------

class GeminiLLM:
    def __init__(self, model: str | None = None, client: Any = None) -> None:
        self.model = model or config.MODEL
        if client is None:
            from google import genai
            client = genai.Client()  # reads GEMINI_API_KEY
        self._client = client

    @staticmethod
    def _declarations(tools: list[dict[str, Any]]):
        from google.genai import types
        decls = []
        for t in tools:
            schema = t["input_schema"]
            decls.append(types.FunctionDeclaration(
                name=t["name"], description=t["description"],
                parameters_json_schema=schema if schema.get("properties") else None))
        return [types.Tool(function_declarations=decls)]

    @staticmethod
    def to_contents(messages: list[dict[str, Any]]) -> list[Any]:
        from google.genai import types
        names = _tool_names(messages)
        contents = []
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=c)]))
            elif m["role"] == "assistant":
                native = next((b["content"] for b in c if b.get("type") == "_gemini_content"), None)
                if native is not None:
                    # Send the model's own turn back unchanged: Gemini 3 attaches
                    # thought signatures to function calls and rejects edited history.
                    contents.append(native)
                else:
                    parts = [types.Part.from_function_call(name=b["name"], args=b["input"])
                             for b in c if b.get("type") == "tool_use"]
                    contents.append(types.Content(role="model", parts=parts))
            else:  # user turn carrying tool results
                parts = []
                for b in c:
                    if b.get("type") == "tool_result":
                        key = "error" if b.get("is_error") else "result"
                        parts.append(types.Part.from_function_response(
                            name=names.get(b["tool_use_id"], "tool"), response={key: b["content"]}))
                contents.append(types.Content(role="user", parts=parts))
        return contents

    def complete(self, *, agent, system, messages, tools=None) -> LLMResponse:
        from google.genai import types
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            max_output_tokens=config.MAX_TOKENS,
            tools=self._declarations(tools) if tools else None,
            # We run the tool loop ourselves (logging, caps, error handling).
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp = _retry_on_rate_limit(lambda: self._client.models.generate_content(
            model=self.model, contents=self.to_contents(messages), config=cfg))
        cand = resp.candidates[0] if resp.candidates else None
        parts = (cand.content.parts or []) if cand and cand.content else []
        text, calls, raw = [], [], []
        for i, p in enumerate(parts):
            if getattr(p, "function_call", None):
                fc = p.function_call
                call = ToolCall(fc.id or f"g{i}_{fc.name}", fc.name, dict(fc.args or {}))
                calls.append(call)
                raw.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.args})
            elif getattr(p, "text", None) and not getattr(p, "thought", False):
                text.append(p.text)
        if cand and cand.content:
            raw.append({"type": "_gemini_content", "content": cand.content})
        um = resp.usage_metadata
        return LLMResponse(
            text="\n".join(text), tool_calls=calls, raw_content=raw,
            input_tokens=(um.prompt_token_count or 0) if um else 0,
            output_tokens=((um.candidates_token_count or 0) + (um.thoughts_token_count or 0)) if um else 0,
        )


# --- Ollama (local) -------------------------------------------------------------

class OllamaLLM:
    def __init__(self, model: str | None = None, post: Any = None) -> None:
        self.model = model or config.MODEL
        if post is None:
            import httpx
            client = httpx.Client(base_url=config.OLLAMA_URL, timeout=300)
            post = lambda path, body: client.post(path, json=body).raise_for_status().json()  # noqa: E731
        self._post = post

    @staticmethod
    def to_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        names = _tool_names(messages)
        out = [{"role": "system", "content": system}]
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                out.append({"role": m["role"], "content": c})
            elif m["role"] == "assistant":
                text = "\n".join(b["text"] for b in c if b.get("type") == "text")
                calls = [{"function": {"name": b["name"], "arguments": b["input"]}} for b in c if b.get("type") == "tool_use"]
                out.append({"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})})
            else:
                for b in c:
                    if b.get("type") == "tool_result":
                        out.append({"role": "tool", "tool_name": names.get(b["tool_use_id"], "tool"),
                                    "content": b["content"]})
        return out

    def complete(self, *, agent, system, messages, tools=None) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "stream": False, "messages": self.to_messages(system, messages),
                                "options": {"temperature": 0, "num_predict": config.MAX_TOKENS}}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": t["input_schema"]}} for t in tools]
        data = self._post("/api/chat", body)
        msg = data.get("message", {})
        calls, raw = [], []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc["function"]
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args or "{}")
            call = ToolCall(f"o{i}_{fn['name']}", fn["name"], args)
            calls.append(call)
            raw.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.args})
        if msg.get("content"):
            raw.insert(0, {"type": "text", "text": msg["content"]})
        return LLMResponse(text=msg.get("content", ""), tool_calls=calls, raw_content=raw,
                           input_tokens=data.get("prompt_eval_count", 0), output_tokens=data.get("eval_count", 0))


# --- Claude (paid) --------------------------------------------------------------

class ClaudeLLM:
    def __init__(self, model: str | None = None) -> None:
        import anthropic  # imported here so tests never need a key

        self.model = model or config.MODEL
        self._client = anthropic.Anthropic()

    def complete(self, *, agent, system, messages, tools=None) -> LLMResponse:
        clean = [{"role": m["role"], "content": m["content"] if isinstance(m["content"], str)
                  else [b for b in m["content"] if not b.get("type", "").startswith("_")]} for m in messages]
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": config.MAX_TOKENS,
                                  "system": system, "messages": clean}
        if tools:
            kwargs["tools"] = tools
        resp = _retry_on_rate_limit(lambda: self._client.messages.create(**kwargs))
        text_parts, calls, raw = [], [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
                raw.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input)))
                raw.append({"type": "tool_use", "id": block.id, "name": block.name, "input": dict(block.input)})
        return LLMResponse(text="\n".join(text_parts), tool_calls=calls, raw_content=raw,
                           input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)


def make_llm(provider: str | None = None) -> CappedLLM:
    provider = provider or config.PROVIDER
    if provider == "fake":
        from agent.fake import FakeLLM
        inner: LLM = FakeLLM()
    elif provider == "gemini":
        inner = GeminiLLM()
    elif provider == "ollama":
        inner = OllamaLLM()
    elif provider == "claude":
        inner = ClaudeLLM()
    else:
        raise ValueError(f"unknown SDA_PROVIDER {provider!r}")
    return CappedLLM(inner)
