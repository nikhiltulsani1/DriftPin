"""AnthropicProvider tests mock the `anthropic` SDK client directly — no real
network call, and no need to reimplement httpx transport mocking since the
SDK owns its own HTTP layer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest

from driftpin.providers.anthropic_provider import AnthropicProvider
from driftpin.providers.base import Message, ProviderValidationError, ToolDefinition


def _text_response(text: str, input_tokens: int = 10, output_tokens: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason="end_turn",
    )


def _tool_use_response(
    tool_name: str, arguments: dict, input_tokens: int = 10, output_tokens: int = 5
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name=tool_name, input=arguments, id="call_1")],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason="tool_use",
    )


def _api_status_error(status_code: int, message: str) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request, json={"error": {"message": message}})
    return anthropic.APIStatusError(message, response=response, body={"error": {"message": message}})


@pytest.mark.asyncio
async def test_validate_succeeds_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-5")
    provider._client.messages.create = AsyncMock(return_value=_text_response("hi"))

    await provider.validate()  # should not raise


@pytest.mark.asyncio
async def test_validate_raises_on_api_error() -> None:
    provider = AnthropicProvider(api_key="bad-key", model="claude-sonnet-5")
    provider._client.messages.create = AsyncMock(
        side_effect=_api_status_error(401, "invalid x-api-key")
    )

    with pytest.raises(ProviderValidationError, match="invalid x-api-key"):
        await provider.validate()


@pytest.mark.asyncio
async def test_complete_parses_plain_text_response() -> None:
    provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-5")
    provider._client.messages.create = AsyncMock(return_value=_text_response("The answer is 42."))

    result = await provider.complete([Message(role="user", content="what is the answer?")], system="sys")

    assert result.content == "The answer is 42."
    assert result.tokens_in == 10
    assert result.tokens_out == 5
    assert result.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_complete_with_tools_sends_tool_definitions() -> None:
    provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-5")
    create_mock = AsyncMock(return_value=_text_response("ok"))
    provider._client.messages.create = create_mock
    tools = [ToolDefinition(name="lookup", description="Looks something up.", input_schema={"type": "object"})]

    await provider.complete([Message(role="user", content="go")], system="sys", tools=tools)

    _, kwargs = create_mock.call_args
    assert kwargs["tools"][0]["name"] == "lookup"


@pytest.mark.asyncio
async def test_complete_structured_forces_tool_choice_and_parses_arguments() -> None:
    provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-5")
    create_mock = AsyncMock(
        return_value=_tool_use_response("emit_structured_output", {"value": "ok"})
    )
    provider._client.messages.create = create_mock

    result = await provider.complete_structured(
        [Message(role="user", content="go")],
        system="sys",
        json_schema={"type": "object", "properties": {"value": {"type": "string"}}},
    )

    import json

    assert json.loads(result.content) == {"value": "ok"}
    _, kwargs = create_mock.call_args
    assert kwargs["tool_choice"] == {"type": "tool", "name": "emit_structured_output"}


@pytest.mark.asyncio
async def test_complete_structured_raises_informative_error_on_api_error() -> None:
    provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-5")
    provider._client.messages.create = AsyncMock(
        side_effect=_api_status_error(429, "rate limit exceeded, retry later")
    )

    with pytest.raises(anthropic.APIStatusError, match="rate limit exceeded"):
        await provider.complete_structured(
            [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
        )


@pytest.mark.asyncio
async def test_complete_structured_returns_text_result_when_no_tool_call_made() -> None:
    provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-5")
    provider._client.messages.create = AsyncMock(return_value=_text_response("I can't do that."))

    result = await provider.complete_structured(
        [Message(role="user", content="go")], system="sys", json_schema={"type": "object"}
    )

    assert result.content == "I can't do that."
    assert result.tool_calls == []
