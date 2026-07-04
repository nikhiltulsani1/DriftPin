from __future__ import annotations

import pytest
from pydantic import BaseModel

from driftpin.providers.base import CompletionResult, Message
from driftpin.providers.structured import StructuredOutputError, complete_structured


class _Answer(BaseModel):
    value: str


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
