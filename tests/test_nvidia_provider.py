"""NvidiaProvider tests use httpx.MockTransport so nothing touches a real network."""

from __future__ import annotations

import json

import httpx
import pytest

from driftpin.providers.base import (
    Message,
    PayloadTooHeavyError,
    ProviderValidationError,
    RequestTooLargeError,
    ServerExhaustedError,
    ToolDefinition,
)
from driftpin.providers.nvidia_provider import NvidiaProvider


async def _no_op_sleep(_seconds: float) -> None:
    return None


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
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")

    await provider.validate()  # should not raise


@pytest.mark.asyncio
async def test_validate_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    monkeypatch.setattr(
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = NvidiaProvider(api_key="bad-key", model="nvidia/nemotron-3-ultra-550b-a55b")

    with pytest.raises(ProviderValidationError):
        await provider.validate()


@pytest.mark.asyncio
async def test_complete_parses_plain_text_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(content="The answer is 42.")

    monkeypatch.setattr(
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")

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
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")
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
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")

    result = await provider.complete_structured(
        [Message(role="user", content="go")],
        system="sys",
        json_schema={"type": "object", "properties": {"value": {"type": "string"}}},
    )

    assert json.loads(result.content) == {"value": "ok"}
    assert captured_payloads[0]["tool_choice"]["function"]["name"] == "emit_structured_output"


@pytest.mark.asyncio
async def test_complete_structured_retries_on_capacity_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                503,
                json={"error": {"message": "ResourceExhausted: Worker local total request limit reached"}},
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
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    monkeypatch.setattr("driftpin.providers.nvidia_provider.asyncio.sleep", _no_op_sleep)
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")

    result = await provider.complete_structured(
        [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
    )

    assert call_count == 2
    assert json.loads(result.content) == {"value": "ok"}


@pytest.mark.asyncio
async def test_complete_structured_raises_payload_too_heavy_after_exhausting_gateway_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture J, second half (guard against over-broadening): a 503/504
    whose body does NOT match any server-exhaustion pattern still follows
    the existing path — capped at `_MAX_GATEWAY_RETRIES` (2), 3 total
    attempts, then `PayloadTooHeavyError` rather than blind-retrying a 4th
    time. Verified live: NVIDIA's reviewer call returned 504 three attempts
    in a row on the same payload shape with an unrelated/empty body, not a
    transient blip and not a capacity-exhaustion signal either."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(504, content=b"")

    monkeypatch.setattr(
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    monkeypatch.setattr("driftpin.providers.nvidia_provider.asyncio.sleep", _no_op_sleep)
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")

    with pytest.raises(PayloadTooHeavyError):
        await provider.complete_structured(
            [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
        )
    assert call_count == 3


@pytest.mark.asyncio
async def test_complete_structured_raises_server_exhausted_not_payload_too_heavy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixture J, first half: a 503 whose body names server-side capacity
    exhaustion — live evidence: "ResourceExhausted: Worker local total
    request limit reached (32/32)" — must classify as `ServerExhaustedError`,
    never `PayloadTooHeavyError`. The wrong classification would trigger
    splitting the request smaller and firing more calls into an already-
    exhausted pool, making the exhaustion worse, not better."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            503,
            json={"error": {"message": "ResourceExhausted: Worker local total request limit reached (32/32)"}},
        )

    monkeypatch.setattr(
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    monkeypatch.setattr("driftpin.providers.nvidia_provider.asyncio.sleep", _no_op_sleep)
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")

    with pytest.raises(ServerExhaustedError) as exc_info:
        await provider.complete_structured(
            [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
        )

    assert exc_info.value.matched_pattern in ("resourceexhausted", "worker", "request limit")
    # 1 initial attempt + 4 retries = 5 total, per _MAX_SERVER_EXHAUSTED_RETRIES.
    assert call_count == 5


@pytest.mark.asyncio
async def test_complete_structured_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "internal error"}})

    monkeypatch.setattr(
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")

    with pytest.raises(httpx.HTTPStatusError):
        await provider.complete_structured(
            [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
        )


@pytest.mark.asyncio
async def test_complete_structured_raises_request_too_large_on_413(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"error": {"message": "payload too large"}})

    monkeypatch.setattr(
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")

    with pytest.raises(RequestTooLargeError):
        await provider.complete_structured(
            [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
        )


@pytest.mark.asyncio
async def test_complete_structured_raises_request_too_large_on_context_length_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "This model's maximum context length is exceeded"}}
        )

    monkeypatch.setattr(
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")

    with pytest.raises(RequestTooLargeError):
        await provider.complete_structured(
            [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
        )


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
        "driftpin.providers.nvidia_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = NvidiaProvider(api_key="test-key", model="nvidia/nemotron-3-ultra-550b-a55b")

    chunks = [chunk async for chunk in provider.stream([Message(role="user", content="hi")], system="sys")]

    text_deltas = "".join(c.text_delta for c in chunks if not c.is_final)
    assert text_deltas == "Hello"

    final = next(c for c in chunks if c.is_final)
    assert final.final_result is not None
    assert final.final_result.content == "Hello"
    assert final.final_result.tokens_in == 3
    assert final.final_result.tokens_out == 2
    assert final.final_result.stop_reason == "stop"
