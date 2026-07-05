"""GroqProvider tests use httpx.MockTransport so nothing touches a real network."""

from __future__ import annotations

import json

import httpx
import pytest

from driftpin.providers.base import Message, ProviderValidationError, ToolDefinition
from driftpin.providers.groq_provider import GroqProvider, _parse_retry_after_seconds


def _client_with_transport(handler):
    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return _MockAsyncClient


def _chat_response(
    content: str = "",
    tool_calls: list[dict] | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> httpx.Response:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


@pytest.mark.asyncio
async def test_validate_succeeds_on_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(content="hi")

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = GroqProvider(api_key="test-key", model="llama-3.3-70b-versatile")

    await provider.validate()  # should not raise


@pytest.mark.asyncio
async def test_validate_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = GroqProvider(api_key="bad-key", model="llama-3.3-70b-versatile")

    with pytest.raises(ProviderValidationError):
        await provider.validate()


@pytest.mark.asyncio
async def test_complete_parses_plain_text_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(content="The answer is 42.")

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = GroqProvider(api_key="test-key", model="llama-3.3-70b-versatile")

    result = await provider.complete([Message(role="user", content="what is the answer?")], system="sys")

    assert result.content == "The answer is 42."
    assert result.tokens_in == 10
    assert result.tokens_out == 5
    assert result.stop_reason == "stop"


@pytest.mark.asyncio
async def test_complete_with_tools_sends_tool_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return _chat_response(content="ok")

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = GroqProvider(api_key="test-key", model="llama-3.3-70b-versatile")
    tools = [ToolDefinition(name="lookup", description="Looks something up.", input_schema={"type": "object"})]

    await provider.complete([Message(role="user", content="go")], system="sys", tools=tools)

    assert captured_payloads[0]["tools"][0]["function"]["name"] == "lookup"


@pytest.mark.asyncio
async def test_complete_structured_forces_tool_choice_and_parses_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return _chat_response(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "emit_structured_output",
                        "arguments": json.dumps({"value": "ok"}),
                    },
                }
            ]
        )

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = GroqProvider(api_key="test-key", model="llama-3.3-70b-versatile")

    result = await provider.complete_structured(
        [Message(role="user", content="go")],
        system="sys",
        json_schema={"type": "object", "properties": {"value": {"type": "string"}}},
    )

    assert json.loads(result.content) == {"value": "ok"}
    assert captured_payloads[0]["tool_choice"]["function"]["name"] == "emit_structured_output"


def test_parse_retry_after_seconds_extracts_value_from_message() -> None:
    assert _parse_retry_after_seconds("Please try again in 21.46s.") == 21.46


def test_parse_retry_after_seconds_falls_back_when_unparseable() -> None:
    assert _parse_retry_after_seconds("no timing info here") == 10.0


@pytest.mark.asyncio
async def test_complete_structured_returns_failed_generation_on_tool_use_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Failed to call a function. Please adjust your prompt.",
                    "type": "invalid_request_error",
                    "code": "tool_use_failed",
                    "failed_generation": '<function=emit_structured_output>{"value": "broken"',
                }
            },
        )

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = GroqProvider(api_key="test-key", model="llama-3.1-8b-instant")

    result = await provider.complete_structured(
        [Message(role="user", content="go")],
        system="sys",
        json_schema={"type": "object"},
    )

    assert result.stop_reason == "tool_use_failed"
    assert "emit_structured_output" in result.content


@pytest.mark.asyncio
async def test_complete_structured_retries_on_rate_limit_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                429,
                json={"error": {"message": "Rate limit reached. Please try again in 0.01s."}},
            )
        return _chat_response(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "emit_structured_output",
                        "arguments": json.dumps({"value": "ok"}),
                    },
                }
            ]
        )

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = GroqProvider(api_key="test-key", model="llama-3.1-8b-instant")

    result = await provider.complete_structured(
        [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
    )

    assert call_count == 2
    assert json.loads(result.content) == {"value": "ok"}


@pytest.mark.asyncio
async def test_complete_structured_raises_after_exhausting_rate_limit_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            429,
            json={"error": {"message": "Rate limit reached. Please try again in 0.01s."}},
        )

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = GroqProvider(api_key="test-key", model="llama-3.1-8b-instant")

    with pytest.raises(httpx.HTTPStatusError):
        await provider.complete_structured(
            [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
        )

    assert call_count == 4  # initial attempt + 3 retries


@pytest.mark.asyncio
async def test_stream_yields_text_deltas_then_final_result(monkeypatch: pytest.MonkeyPatch) -> None:
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":3,"completion_tokens":2}}',
        "data: [DONE]",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = "\n\n".join(sse_lines) + "\n\n"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = GroqProvider(api_key="test-key", model="llama-3.3-70b-versatile")

    chunks = [chunk async for chunk in provider.stream([Message(role="user", content="hi")], system="sys")]

    text_deltas = "".join(c.text_delta for c in chunks if not c.is_final)
    assert text_deltas == "Hello"

    final = next(c for c in chunks if c.is_final)
    assert final.final_result is not None
    assert final.final_result.content == "Hello"
    assert final.final_result.tokens_in == 3
    assert final.final_result.tokens_out == 2
    assert final.final_result.stop_reason == "stop"
