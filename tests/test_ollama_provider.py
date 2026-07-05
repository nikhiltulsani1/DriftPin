"""OllamaProvider tests use httpx.MockTransport so nothing touches a real local server."""

from __future__ import annotations

import json

import httpx
import pytest

from driftpin.providers.base import Message, ProviderValidationError, ToolDefinition
from driftpin.providers.ollama_provider import OllamaProvider


def _client_with_transport(handler):
    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return _MockAsyncClient


def _chat_response(
    content: str = "",
    tool_calls: list[dict] | None = None,
    done: bool = True,
    prompt_eval_count: int = 10,
    eval_count: int = 5,
) -> httpx.Response:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "message": message,
            "done": done,
            "prompt_eval_count": prompt_eval_count,
            "eval_count": eval_count,
        },
    )


@pytest.mark.asyncio
async def test_validate_succeeds_when_model_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "llama3.2:3b"}]})

    monkeypatch.setattr(
        "driftpin.providers.ollama_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2:3b")

    await provider.validate()  # should not raise


@pytest.mark.asyncio
async def test_validate_raises_when_model_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "mistral:latest"}]})

    monkeypatch.setattr(
        "driftpin.providers.ollama_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2:3b")

    with pytest.raises(ProviderValidationError, match="not installed"):
        await provider.validate()


@pytest.mark.asyncio
async def test_validate_raises_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        "driftpin.providers.ollama_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2:3b")

    with pytest.raises(ProviderValidationError, match="unreachable"):
        await provider.validate()


@pytest.mark.asyncio
async def test_complete_parses_plain_text_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(content="The answer is 42.")

    monkeypatch.setattr(
        "driftpin.providers.ollama_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2:3b")

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
        "driftpin.providers.ollama_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2:3b")
    tools = [ToolDefinition(name="lookup", description="Looks something up.", input_schema={"type": "object"})]

    await provider.complete([Message(role="user", content="go")], system="sys", tools=tools)

    assert captured_payloads[0]["tools"][0]["function"]["name"] == "lookup"


@pytest.mark.asyncio
async def test_complete_structured_sends_format_schema_and_parses_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return _chat_response(
            tool_calls=[{"function": {"name": "emit_structured_output", "arguments": {"value": "ok"}}}]
        )

    monkeypatch.setattr(
        "driftpin.providers.ollama_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2:3b")

    result = await provider.complete_structured(
        [Message(role="user", content="go")],
        system="sys",
        json_schema={"type": "object", "properties": {"value": {"type": "string"}}},
    )

    assert result.tool_calls[0].arguments == {"value": "ok"}
    assert captured_payloads[0]["format"] == {"type": "object", "properties": {"value": {"type": "string"}}}


@pytest.mark.asyncio
async def test_complete_raises_with_response_body_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "model requires more system memory than is available"})

    monkeypatch.setattr(
        "driftpin.providers.ollama_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2:3b")

    with pytest.raises(httpx.HTTPStatusError, match="model requires more system memory"):
        await provider.complete([Message(role="user", content="go")], system="sys")


@pytest.mark.asyncio
async def test_complete_structured_raises_with_response_body_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid format schema"})

    monkeypatch.setattr(
        "driftpin.providers.ollama_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2:3b")

    with pytest.raises(httpx.HTTPStatusError, match="invalid format schema"):
        await provider.complete_structured(
            [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
        )


@pytest.mark.asyncio
async def test_stream_yields_text_deltas_then_final_result(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        json.dumps({"message": {"content": "Hel"}, "done": False}),
        json.dumps({"message": {"content": "lo"}, "done": False}),
        json.dumps(
            {"message": {"content": ""}, "done": True, "prompt_eval_count": 3, "eval_count": 2}
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = "\n".join(lines) + "\n"
        return httpx.Response(200, content=body)

    monkeypatch.setattr(
        "driftpin.providers.ollama_provider.httpx.AsyncClient", _client_with_transport(handler)
    )
    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.2:3b")

    chunks = [chunk async for chunk in provider.stream([Message(role="user", content="hi")], system="sys")]

    text_deltas = "".join(c.text_delta for c in chunks if not c.is_final)
    assert text_deltas == "Hello"

    final = next(c for c in chunks if c.is_final)
    assert final.final_result is not None
    assert final.final_result.content == "Hello"
    assert final.final_result.tokens_in == 3
    assert final.final_result.tokens_out == 2
