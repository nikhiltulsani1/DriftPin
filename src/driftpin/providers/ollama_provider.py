"""Ollama provider for locally-served models via the /api/chat endpoint.

Structured output relies on Ollama's `format` parameter accepting a JSON
schema; conformance is inherently weaker on small local models, which is why
`providers.structured` wraps every provider in a validate-and-repair loop and
the init wizard runs a conformance probe before trusting a local model."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from driftpin.providers.base import (
    CompletionResult,
    LLMProvider,
    Message,
    ProviderValidationError,
    StreamChunk,
    ToolCall,
    ToolDefinition,
)

# Local CPU inference generating a large structured JSON response (many
# scenarios/test cases at once) can genuinely take several minutes — this
# needs to be generous, not cloud-API-latency-sized.
_DEFAULT_TIMEOUT_SECONDS = 600.0


def _raise_for_status_with_body(response: httpx.Response) -> None:
    """`raise_for_status()` alone drops Ollama's actual error message (e.g.
    "model requires more system memory", a context-length overflow, or an
    invalid `format` schema) — that detail lives in the JSON body."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise httpx.HTTPStatusError(
            f"{exc}\nResponse body: {response.text}",
            request=exc.request,
            response=exc.response,
        ) from exc


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=_DEFAULT_TIMEOUT_SECONDS)

    async def validate(self) -> None:
        try:
            response = await self._client.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderValidationError(
                f"Ollama unreachable at {self._base_url}: {exc}"
            ) from exc

        installed = {m["name"] for m in response.json().get("models", [])}
        if self.model not in installed and not any(
            name.startswith(self.model) for name in installed
        ):
            raise ProviderValidationError(
                f"Model '{self.model}' is not installed in this Ollama instance."
            )

    def _to_ollama_messages(self, messages: list[Message], system: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = [{"role": "system", "content": system}]
        result.extend({"role": m.role, "content": m.content} for m in messages)
        return result

    async def complete(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> CompletionResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_ollama_messages(messages, system),
            "stream": False,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]

        response = await self._client.post("/api/chat", json=payload)
        _raise_for_status_with_body(response)
        return self._parse_response(response.json())

    def _parse_response(self, data: dict[str, Any]) -> CompletionResult:
        message = data.get("message", {})
        tool_calls: list[ToolCall] = []
        for index, call in enumerate(message.get("tool_calls", []) or []):
            function = call.get("function", {})
            tool_calls.append(
                ToolCall(
                    tool_name=function.get("name", ""),
                    arguments=function.get("arguments", {}),
                    call_id=f"call_{index}",
                )
            )
        return CompletionResult(
            content=message.get("content", ""),
            tool_calls=tool_calls,
            tokens_in=data.get("prompt_eval_count", 0),
            tokens_out=data.get("eval_count", 0),
            stop_reason="stop" if data.get("done") else "unknown",
        )

    async def stream(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_ollama_messages(messages, system),
            "stream": True,
        }
        final_content_parts: list[str] = []
        async with self._client.stream("POST", "/api/chat", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                delta = chunk.get("message", {}).get("content", "")
                if delta:
                    final_content_parts.append(delta)
                    yield StreamChunk(text_delta=delta)
                if chunk.get("done"):
                    yield StreamChunk(
                        is_final=True,
                        final_result=CompletionResult(
                            content="".join(final_content_parts),
                            tokens_in=chunk.get("prompt_eval_count", 0),
                            tokens_out=chunk.get("eval_count", 0),
                            stop_reason="stop",
                        ),
                    )

    async def complete_structured(
        self,
        messages: list[Message],
        system: str,
        json_schema: dict[str, Any],
    ) -> CompletionResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_ollama_messages(messages, system),
            "stream": False,
            "format": json_schema,
        }
        response = await self._client.post("/api/chat", json=payload)
        _raise_for_status_with_body(response)
        return self._parse_response(response.json())
