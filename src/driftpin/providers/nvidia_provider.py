"""NVIDIA NIM provider: OpenAI-compatible chat completions over NVIDIA's
hosted inference API (`integrate.api.nvidia.com`), e.g. Nemotron models.

Same integration shape as `GroqProvider` — direct `httpx` calls against an
OpenAI-compatible `/chat/completions` endpoint, forced tool-calling for
structured output — rather than pulling in the `openai` SDK as a dependency
for one more provider that speaks the same wire format.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from driftpin.providers.base import (
    CompletionResult,
    LLMProvider,
    Message,
    PayloadTooHeavyError,
    ProviderValidationError,
    RequestTooLargeError,
    ServerExhaustedError,
    StreamChunk,
    ToolCall,
    ToolDefinition,
)

_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_DEFAULT_TIMEOUT_SECONDS = 600.0
_DEFAULT_MAX_TOKENS = 16384
_STRUCTURED_TOOL_NAME = "emit_structured_output"
_MAX_RATE_LIMIT_RETRIES = 5
_RATE_LIMIT_BACKOFF_SECONDS = 15.0
_MAX_GATEWAY_RETRIES = 2
_GATEWAY_BACKOFF_SECONDS = 15.0
_GATEWAY_STATUS_CODES = {502, 503, 504}
_TOO_LARGE_MESSAGE_MARKERS = ("context length", "context_length", "maximum context", "too large", "reduce the length")
# A 502/503/504 body matching one of these (case-insensitive) means the
# provider's own capacity is exhausted — a shared worker pool or request
# queue being full — not that our payload is too large. Checked BEFORE a
# gateway error counts toward the payload-too-heavy threshold, since the
# two failure modes need opposite remedies (patience vs. splitting).
_SERVER_EXHAUSTION_PATTERNS = ("resourceexhausted", "worker", "request limit", "capacity", "overloaded", "quota")
_MAX_SERVER_EXHAUSTED_RETRIES = 4
_SERVER_EXHAUSTED_BACKOFF_SECONDS = 30.0
_SERVER_EXHAUSTED_BACKOFF_CAP_SECONDS = 300.0


def _match_server_exhaustion_pattern(body_text: str) -> str | None:
    lowered = body_text.lower()
    for pattern in _SERVER_EXHAUSTION_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def _raise_for_status_with_body(response: httpx.Response) -> None:
    """`raise_for_status()` alone drops NVIDIA's actual error message — the
    detail that explains *why* a request failed lives in the JSON body.

    A request-too-large response (413, or a 400 naming a context/length
    ceiling) is raised as `RequestTooLargeError` instead — retrying an
    oversized request unchanged can never succeed, so it needs a
    distinguishable signal rather than looking like any other HTTP failure
    that might get blind-retried the way a 503/504 capacity error is."""
    if response.status_code == 413 or (
        response.status_code == 400
        and any(marker in response.text.lower() for marker in _TOO_LARGE_MESSAGE_MARKERS)
    ):
        raise RequestTooLargeError(f"NVIDIA rejected the request as too large: {response.text}")

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise httpx.HTTPStatusError(
            f"{exc}\nResponse body: {response.text}",
            request=exc.request,
            response=exc.response,
        ) from exc


class NvidiaProvider(LLMProvider):
    name = "nvidia"

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
            raise ProviderValidationError(f"NVIDIA validation failed: {exc}") from exc

    def _to_nvidia_messages(self, messages: list[Message], system: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = [{"role": "system", "content": system}]
        result.extend({"role": m.role, "content": m.content} for m in messages)
        return result

    def _to_nvidia_tools(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
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

    async def _post_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        """Three distinct retry policies, not one blind loop over "any error
        that smells transient":

        - 429 (rate limit, NVIDIA's own capacity ceiling — "Worker local
          total request limit reached") is genuinely transient; waiting
          actually helps, so it gets the more generous retry budget.
        - 502/503/504 whose body names server-side capacity exhaustion (see
          `_SERVER_EXHAUSTION_PATTERNS`) get a long, patient backoff and
          then a hard failure — never a split. Live evidence: NVIDIA
          returned 503 with body "ResourceExhausted: Worker local total
          request limit reached (32/32)" on three consecutive identical-
          payload attempts; this used to fall into the payload-too-heavy
          path below, whose prescribed remedy (split the request smaller,
          fire two requests instead of one) makes server-side exhaustion
          *worse*, not better, since it adds load to an already-full pool.
          This check runs BEFORE the payload-too-heavy counter below, so an
          exhaustion-pattern body never counts toward that threshold.
        - Any other 502/503/504 gets a hard cap of `_MAX_GATEWAY_RETRIES`.
          Live testing separately found NVIDIA's reviewer call returning
          504 three attempts in a row on the *same* payload shape with an
          empty/unrelated body — not a transient blip, a systematic timeout
          on that request's size. Beyond the cap this raises
          `PayloadTooHeavyError` instead of trying a 4th identical request:
          a caller that can shrink the payload (a smaller review group) is
          far better positioned to fix this than blind retrying is.
        """
        rate_limit_attempt = 0
        gateway_attempt = 0
        server_exhausted_attempt = 0
        while True:
            response = await self._client.post("/chat/completions", json=payload)

            if response.status_code == 429 and rate_limit_attempt < _MAX_RATE_LIMIT_RETRIES:
                await asyncio.sleep(_RATE_LIMIT_BACKOFF_SECONDS)
                rate_limit_attempt += 1
                continue

            if response.status_code in _GATEWAY_STATUS_CODES:
                matched_pattern = _match_server_exhaustion_pattern(response.text)
                if matched_pattern is not None:
                    if server_exhausted_attempt >= _MAX_SERVER_EXHAUSTED_RETRIES:
                        raise ServerExhaustedError(
                            f"NVIDIA returned {response.status_code} on {server_exhausted_attempt + 1} "
                            f"consecutive attempts, body matching server-capacity-exhaustion pattern "
                            f"'{matched_pattern}' — this is NVIDIA's own capacity limit (a shared "
                            "worker pool or request queue being full), not our payload. Retry later "
                            f"once its queue has cleared. Response body: {response.text}",
                            matched_pattern=matched_pattern,
                        )
                    backoff = min(
                        _SERVER_EXHAUSTED_BACKOFF_SECONDS * (2**server_exhausted_attempt),
                        _SERVER_EXHAUSTED_BACKOFF_CAP_SECONDS,
                    )
                    await asyncio.sleep(backoff)
                    server_exhausted_attempt += 1
                    continue

                if gateway_attempt >= _MAX_GATEWAY_RETRIES:
                    raise PayloadTooHeavyError(
                        f"NVIDIA returned {response.status_code} on {gateway_attempt + 1} "
                        "consecutive attempts with an identical payload — treating this as a "
                        "systematic gateway timeout on this request's size, not a transient "
                        f"one. Response body: {response.text}"
                    )
                await asyncio.sleep(_GATEWAY_BACKOFF_SECONDS * (2**gateway_attempt))
                gateway_attempt += 1
                continue

            return response

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
            "messages": self._to_nvidia_messages(messages, system),
        }
        if tools:
            payload["tools"] = self._to_nvidia_tools(tools)

        response = await self._post_with_retry(payload)
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
            "messages": self._to_nvidia_messages(messages, system),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = self._to_nvidia_tools(tools)

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
            "messages": self._to_nvidia_messages(messages, system),
            "tools": self._to_nvidia_tools([forced_tool]),
            "tool_choice": {"type": "function", "function": {"name": _STRUCTURED_TOOL_NAME}},
        }
        response = await self._post_with_retry(payload)
        _raise_for_status_with_body(response)
        result = self._parse_response(response.json())
        if not result.tool_calls:
            return result

        structured_json = json.dumps(result.tool_calls[0].arguments)
        return result.model_copy(update={"content": structured_json})
