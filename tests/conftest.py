"""Shared fixtures: a scripted mock provider that never calls a real network."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from driftpin.providers.base import (
    CompletionResult,
    LLMProvider,
    Message,
    ProviderValidationError,
    StreamChunk,
    ToolDefinition,
)


class MockProvider(LLMProvider):
    """Replays a queue of canned CompletionResults, one per call to
    complete_structured/complete. Raises if the queue runs dry, so tests fail
    loudly instead of silently reusing stale responses."""

    name = "mock"

    def __init__(self, responses: list[CompletionResult], valid: bool = True) -> None:
        self._responses = list(responses)
        self._valid = valid
        self.call_count = 0

    async def validate(self) -> None:
        if not self._valid:
            raise ProviderValidationError("mock provider configured to fail validation")

    async def complete(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> CompletionResult:
        return self._next_response()

    async def stream(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        result = self._next_response()
        yield StreamChunk(text_delta=result.content)
        yield StreamChunk(is_final=True, final_result=result)

    async def complete_structured(
        self,
        messages: list[Message],
        system: str,
        json_schema: dict[str, Any],
    ) -> CompletionResult:
        return self._next_response()

    def _next_response(self) -> CompletionResult:
        if not self._responses:
            raise AssertionError("MockProvider queue exhausted: test issued more calls than expected")
        self.call_count += 1
        return self._responses.pop(0)


@pytest.fixture
def mock_provider_factory():
    def _make(responses: list[CompletionResult], valid: bool = True) -> MockProvider:
        return MockProvider(responses, valid=valid)

    return _make
