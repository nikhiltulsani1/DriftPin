from __future__ import annotations

import pytest

from driftpin.providers.base import CompletionResult, ProviderValidationError
from driftpin.providers.conformance import run_conformance_probe


@pytest.mark.asyncio
async def test_validate_raises_when_provider_marked_invalid(mock_provider_factory) -> None:
    provider = mock_provider_factory([], valid=False)
    with pytest.raises(ProviderValidationError):
        await provider.validate()


@pytest.mark.asyncio
async def test_validate_succeeds_when_provider_marked_valid(mock_provider_factory) -> None:
    provider = mock_provider_factory([], valid=True)
    await provider.validate()  # should not raise


@pytest.mark.asyncio
async def test_conformance_probe_passes_when_all_responses_valid(mock_provider_factory) -> None:
    responses = [
        CompletionResult(
            content='{"answer": "ok", "confidence": 1.0}',
            tokens_in=1,
            tokens_out=1,
            stop_reason="end_turn",
        )
        for _ in range(3)
    ]
    provider = mock_provider_factory(responses)

    result = await run_conformance_probe(provider)

    assert result.successes == 3
    assert result.passed is True


@pytest.mark.asyncio
async def test_conformance_probe_fails_below_threshold(mock_provider_factory) -> None:
    responses = [
        CompletionResult(content="not valid json", tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        CompletionResult(content="also not valid", tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        CompletionResult(
            content='{"answer": "ok", "confidence": 1.0}',
            tokens_in=1,
            tokens_out=1,
            stop_reason="end_turn",
        ),
    ]
    provider = mock_provider_factory(responses)

    result = await run_conformance_probe(provider)

    assert result.successes == 1
    assert result.passed is False
