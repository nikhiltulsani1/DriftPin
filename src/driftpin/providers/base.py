"""Provider-agnostic interface. A new backend is one new file implementing this ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    tool_name: str
    arguments: dict[str, Any]
    call_id: str


class CompletionResult(BaseModel):
    content: str
    tool_calls: list[ToolCall] = []
    tokens_in: int
    tokens_out: int
    stop_reason: str


class StreamChunk(BaseModel):
    text_delta: str = ""
    tool_call: ToolCall | None = None
    is_final: bool = False
    final_result: CompletionResult | None = None


class ProviderValidationError(Exception):
    """Raised when a provider fails startup validation (bad key, unreachable host)."""


class RequestTooLargeError(Exception):
    """Raised when a provider rejects a request as exceeding its context or
    payload size limit (HTTP 413, or a 400 whose message names a context/
    length ceiling). Distinct from a generic HTTP error on purpose: retrying
    an oversized request unchanged can never succeed — waiting doesn't make
    a request smaller — so callers must not fold this into the same
    blind-retry path used for transient errors like rate limits."""


class PayloadTooHeavyError(Exception):
    """Raised after a bounded number of consecutive 502/503/504 gateway
    errors on an identical payload — evidence gathered live (three NVIDIA
    reviewer-call failures on the same request shape) that some gateway
    timeouts are systematic, not transient, and blind-retrying the same
    payload indefinitely will never succeed. Distinct from `RequestTooLargeError`
    (an explicit size-limit rejection) and from ordinary rate-limit backoff
    (429, which stays on its own retry path since waiting genuinely helps
    there). A caller that receives this and can reduce the payload (e.g. a
    smaller review group) should retry with that smaller payload; a caller
    that can't split further should surface the failure rather than retry
    the exact same request a third, fourth, fifth time."""


class ServerExhaustedError(Exception):
    """Raised after exhausting a long-backoff retry budget on a gateway
    error (502/503/504) whose response body names server-side capacity
    exhaustion — a shared worker pool or request-limit queue being full —
    rather than our own request being too large. Distinct from
    `PayloadTooHeavyError` on purpose: the fix for capacity exhaustion is
    patience (and eventually giving up), never splitting the payload
    smaller. Splitting fires *more* requests into an already-exhausted
    pool, making the exhaustion worse, not better. Live evidence: NVIDIA
    returning 503 with body "ResourceExhausted: Worker local total request
    limit reached (32/32)" — misclassified as `PayloadTooHeavyError` before
    this distinction existed, which then triggered exactly the wrong
    remedy (splitting a review group in half and firing two more requests
    at the same exhausted pool)."""

    def __init__(self, message: str, matched_pattern: str) -> None:
        self.matched_pattern = matched_pattern
        super().__init__(message)


class LLMProvider(ABC):
    """Every provider implements streaming, non-streaming, tool calling, and
    structured JSON output over the same message shape."""

    name: str
    model: str

    @abstractmethod
    async def validate(self) -> None:
        """Raises ProviderValidationError if the provider cannot be reached/authenticated."""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> CompletionResult:
        ...

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Implementations are async generator functions (`async def` + `yield`);
        declared without `async` here so the return type is the iterator itself,
        not a coroutine that resolves to one."""

    @abstractmethod
    async def complete_structured(
        self,
        messages: list[Message],
        system: str,
        json_schema: dict[str, Any],
    ) -> CompletionResult:
        """Returns a CompletionResult whose `content` is a JSON string conforming
        to `json_schema`. Validation against a pydantic model happens one layer
        up, in providers.structured, so retry/repair logic is provider-agnostic."""
