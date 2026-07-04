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


class LLMProvider(ABC):
    """Every provider implements streaming, non-streaming, tool calling, and
    structured JSON output over the same message shape."""

    name: str

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
