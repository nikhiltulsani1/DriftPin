"""Anthropic provider. Structured output uses forced tool-calling: the schema
is registered as a single tool and the model is required to call it, which is
far more reliable than asking for bare JSON in prose."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from driftpin.providers.base import (
    CompletionResult,
    LLMProvider,
    Message,
    ProviderValidationError,
    StreamChunk,
    ToolCall,
    ToolDefinition,
)

_STRUCTURED_TOOL_NAME = "emit_structured_output"
_DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        self._model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def validate(self) -> None:
        try:
            await self._client.messages.create(
                model=self._model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
        except anthropic.APIError as exc:
            raise ProviderValidationError(f"Anthropic validation failed: {exc}") from exc

    def _to_anthropic_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        return [{"role": m.role, "content": m.content} for m in messages]

    def _to_anthropic_tools(self, tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]

    async def complete(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> CompletionResult:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "system": system,
            "messages": self._to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)

        response = await self._client.messages.create(**kwargs)
        return self._to_completion_result(response)

    def _to_completion_result(self, response: Any) -> CompletionResult:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(tool_name=block.name, arguments=block.input, call_id=block.id)
                )
        return CompletionResult(
            content="".join(text_parts),
            tool_calls=tool_calls,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            stop_reason=response.stop_reason or "unknown",
        )

    async def stream(
        self,
        messages: list[Message],
        system: str,
        tools: list[ToolDefinition] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "system": system,
            "messages": self._to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)

        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield StreamChunk(text_delta=text)
            final_message = await stream.get_final_message()
            yield StreamChunk(is_final=True, final_result=self._to_completion_result(final_message))

    async def complete_structured(
        self,
        messages: list[Message],
        system: str,
        json_schema: dict[str, Any],
    ) -> CompletionResult:
        forced_tool = ToolDefinition(
            name=_STRUCTURED_TOOL_NAME,
            description="Emit the structured result conforming to the required schema.",
            input_schema=json_schema,
        )
        # The SDK's overloads pin `tools`/`tool_choice` to precise TypedDict unions;
        # our internal dict[str, Any] shape satisfies them at runtime but not
        # structurally under mypy, hence the targeted ignore.
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=_DEFAULT_MAX_TOKENS,
            system=system,
            messages=self._to_anthropic_messages(messages),
            tools=self._to_anthropic_tools([forced_tool]),
            tool_choice={"type": "tool", "name": _STRUCTURED_TOOL_NAME},
        )  # type: ignore[call-overload]
        result = self._to_completion_result(response)
        if not result.tool_calls:
            return result

        structured_json = json.dumps(result.tool_calls[0].arguments)
        return result.model_copy(update={"content": structured_json})
