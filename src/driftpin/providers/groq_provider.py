"""Groq provider: OpenAI-compatible chat completions over Groq's hosted inference API.

Structured output uses forced tool-calling, same strategy as the Anthropic
provider — the schema is registered as a single function and `tool_choice`
forces the model to call it, which is far more reliable on hosted models than
asking for bare JSON in prose.
"""

from __future__ import annotations

import asyncio
import json
import re
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

_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_TOKENS = 4096
_STRUCTURED_TOOL_NAME = "emit_structured_output"
_MAX_RATE_LIMIT_RETRIES = 3
_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 10.0
_RETRY_AFTER_PATTERN = re.compile(r"try again in ([\d.]+)s")


def _parse_retry_after_seconds(message: str) -> float:
    match = _RETRY_AFTER_PATTERN.search(message)
    if match:
        return float(match.group(1))
    return _DEFAULT_RATE_LIMIT_BACKOFF_SECONDS


def _raise_for_status_with_body(response: httpx.Response) -> None:
    """`raise_for_status()` alone drops Groq's actual error message (status
    code and generic reason phrase only) — the detail that actually explains
    *why* a request failed lives in the JSON body. Re-raise with it attached."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise httpx.HTTPStatusError(
            f"{exc}\nResponse body: {response.text}",
            request=exc.request,
            response=exc.response,
        ) from exc


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    async def validate(self) -> None:
        try:
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                },
            )
            _raise_for_status_with_body(response)
        except httpx.HTTPError as exc:
            raise ProviderValidationError(f"Groq validation failed: {exc}") from exc

    def _to_groq_messages(self, messages: list[Message], system: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = [{"role": "system", "content": system}]
        result.extend({"role": m.role, "content": m.content} for m in messages)
        return result

    def _to_groq_tools(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
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

    async def _post_with_rate_limit_retry(self, payload: dict[str, Any]) -> httpx.Response:
        """POSTs to /chat/completions, backing off and retrying on 429 up to
        `_MAX_RATE_LIMIT_RETRIES` times. Any other status is returned as-is
        for the caller to interpret (different endpoints treat 400 differently)."""
        attempt = 0
        while True:
            response = await self._client.post("/chat/completions", json=payload)
            if response.status_code != 429 or attempt >= _MAX_RATE_LIMIT_RETRIES:
                return response

            message = response.json().get("error", {}).get("message", "")
            await asyncio.sleep(_parse_retry_after_seconds(message))
            attempt += 1

    def _parse_response(self, data: dict[str, Any]) -> CompletionResult:
        choice = data["choices"][0]
        message = choice.get("message", {})
        tool_calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            arguments_raw = function.get("arguments", "{}")
            arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
            tool_calls.append(
                ToolCall(tool_name=function.get("name", ""), arguments=arguments, call_id=call.get("id", ""))
            )

        usage = data.get("usage", {})
        return CompletionResult(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            stop_reason=choice.get("finish_reason") or "unknown",
        )

    async def complete(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> CompletionResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": self._to_groq_messages(messages, system),
        }
        if tools:
            payload["tools"] = self._to_groq_tools(tools)

        response = await self._post_with_rate_limit_retry(payload)
        _raise_for_status_with_body(response)
        return self._parse_response(response.json())

    async def stream(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": self._to_groq_messages(messages, system),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = self._to_groq_tools(tools)

        content_parts: list[str] = []
        tokens_in = 0
        tokens_out = 0
        stop_reason = "unknown"

        async with self._client.stream("POST", "/chat/completions", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload_text = line[len("data: ") :].strip()
                if payload_text == "[DONE]":
                    break

                chunk = json.loads(payload_text)
                usage = chunk.get("usage")
                if usage:
                    tokens_in = usage.get("prompt_tokens", tokens_in)
                    tokens_out = usage.get("completion_tokens", tokens_out)

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                finish_reason = choices[0].get("finish_reason")
                if finish_reason:
                    stop_reason = finish_reason

                text_delta = delta.get("content") or ""
                if text_delta:
                    content_parts.append(text_delta)
                    yield StreamChunk(text_delta=text_delta)

        yield StreamChunk(
            is_final=True,
            final_result=CompletionResult(
                content="".join(content_parts),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                stop_reason=stop_reason,
            ),
        )

    async def complete_structured(
        self,
        messages: list[Message],
        system: str,
        json_schema: dict[str, Any],
    ) -> CompletionResult:
        forced_tool = ToolDefinition(
            name=_STRUCTURED_TOOL_NAME,
            description="Emit the structured result conforming to the required schema.",
            input_schema=json_schema,
        )
        payload = {
            "model": self.model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": self._to_groq_messages(messages, system),
            "tools": self._to_groq_tools([forced_tool]),
            "tool_choice": {"type": "function", "function": {"name": _STRUCTURED_TOOL_NAME}},
        }
        response = await self._post_with_rate_limit_retry(payload)

        if response.status_code == 400:
            error = response.json().get("error", {})
            if error.get("code") == "tool_use_failed":
                # The model produced a call Groq's backend couldn't parse into
                # valid arguments. Surface the raw attempt as `content` rather
                # than raising — providers.structured's retry loop treats
                # unparseable content exactly like a schema-validation
                # failure and re-prompts with the concrete error, which is
                # the same recovery path a malformed-but-200 response gets.
                return CompletionResult(
                    content=error.get("failed_generation", ""),
                    tokens_in=0,
                    tokens_out=0,
                    stop_reason="tool_use_failed",
                )

        _raise_for_status_with_body(response)
        result = self._parse_response(response.json())
        if not result.tool_calls:
            return result

        structured_json = json.dumps(result.tool_calls[0].arguments)
        return result.model_copy(update={"content": structured_json})
