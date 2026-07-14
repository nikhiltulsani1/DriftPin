from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from driftpin.providers.base import CompletionResult, LLMProvider, Message, ToolDefinition
from driftpin.providers.structured import StructuredOutputError, complete_structured


class _Answer(BaseModel):
    value: str


class _MessageCapturingProvider(LLMProvider):
    """Records the `messages` list passed to each `complete_structured` call,
    so a test can assert on the exact retry-correction text sent back to the
    model — not just that a retry happened."""

    name = "mock"
    model = "mock-model"

    def __init__(self, responses: list[CompletionResult]) -> None:
        self._responses = list(responses)
        self.messages_per_call: list[list[Message]] = []

    async def validate(self) -> None:
        return None

    async def complete(
        self, messages: list[Message], system: str, tools: list[ToolDefinition] | None = None
    ) -> CompletionResult:
        return self._next_response(messages)

    async def stream(self, messages: list[Message], system: str, tools: list[ToolDefinition] | None = None):
        result = self._next_response(messages)
        yield result  # pragma: no cover - unused by complete_structured path

    async def complete_structured(
        self, messages: list[Message], system: str, json_schema: dict[str, Any]
    ) -> CompletionResult:
        return self._next_response(messages)

    def _next_response(self, messages: list[Message]) -> CompletionResult:
        self.messages_per_call.append(list(messages))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_succeeds_on_first_valid_response(mock_provider_factory) -> None:
    provider = mock_provider_factory(
        [CompletionResult(content='{"value": "ok"}', tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    parsed, attempts = await complete_structured(
        provider, [Message(role="user", content="go")], system="sys", response_model=_Answer
    )

    assert parsed.value == "ok"
    assert attempts == 1


@pytest.mark.asyncio
async def test_recovers_after_one_invalid_response(mock_provider_factory) -> None:
    provider = mock_provider_factory(
        [
            CompletionResult(content="not json", tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content='{"value": "recovered"}', tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )

    parsed, attempts = await complete_structured(
        provider, [Message(role="user", content="go")], system="sys", response_model=_Answer
    )

    assert parsed.value == "recovered"
    assert attempts == 2


@pytest.mark.asyncio
async def test_raises_after_exhausting_retries(mock_provider_factory) -> None:
    provider = mock_provider_factory(
        [
            CompletionResult(content="bad-1", tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content="bad-2", tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content="bad-3", tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        ]
    )

    with pytest.raises(StructuredOutputError) as excinfo:
        await complete_structured(
            provider,
            [Message(role="user", content="go")],
            system="sys",
            response_model=_Answer,
            max_retries=2,
        )

    assert excinfo.value.attempts == 3
    assert provider.call_count == 3


@pytest.mark.asyncio
async def test_length_truncation_gets_a_distinct_retry_message_not_generic_validation_text() -> None:
    provider = _MessageCapturingProvider(
        [
            CompletionResult(content='{"value": "cut off', tokens_in=1, tokens_out=1, stop_reason="length"),
            CompletionResult(content='{"value": "ok"}', tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        ]
    )

    parsed, attempts = await complete_structured(
        provider, [Message(role="user", content="go")], system="sys", response_model=_Answer
    )

    assert parsed.value == "ok"
    assert attempts == 2

    retry_message = provider.messages_per_call[1][-1].content
    assert "cut off" in retry_message.lower() or "length" in retry_message.lower()
    assert "more concisely" in retry_message
    assert "schema validation" not in retry_message.lower()
