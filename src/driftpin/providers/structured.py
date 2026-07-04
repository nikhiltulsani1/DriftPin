"""Schema-validated completion with a bounded repair loop.

This is the single chokepoint every agent uses to get a validated pydantic
object back from any provider. On a validation failure the raw errors are fed
back to the model verbatim so it can correct itself; after `max_retries`
failures the caller must halt rather than fall back to unvalidated output.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ValidationError

from driftpin.providers.base import LLMProvider, Message

_ModelT = TypeVar("_ModelT", bound=BaseModel)

DEFAULT_MAX_RETRIES = 2


class StructuredOutputError(Exception):
    """Raised when a provider fails to produce schema-valid output after all retries."""

    def __init__(self, attempts: int, last_error: str, raw_output: str) -> None:
        self.attempts = attempts
        self.last_error = last_error
        self.raw_output = raw_output
        super().__init__(
            f"Failed to obtain schema-valid output after {attempts} attempt(s): {last_error}"
        )


async def complete_structured(
    provider: LLMProvider,
    messages: list[Message],
    system: str,
    response_model: type[_ModelT],
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> tuple[_ModelT, int]:
    """Returns the validated model instance and the number of attempts used."""
    schema = response_model.model_json_schema()
    working_messages = list(messages)
    last_error = ""
    last_raw = ""

    for attempt in range(1, max_retries + 2):
        result = await provider.complete_structured(working_messages, system, schema)
        last_raw = result.content
        try:
            parsed = response_model.model_validate_json(result.content)
            return parsed, attempt
        except ValidationError as exc:
            last_error = str(exc)
            working_messages = [
                *working_messages,
                Message(role="assistant", content=result.content),
                Message(
                    role="user",
                    content=(
                        "That output failed schema validation with these errors:\n"
                        f"{last_error}\n\n"
                        "Return only a corrected JSON object satisfying the schema."
                    ),
                ),
            ]

    raise StructuredOutputError(attempts=max_retries + 1, last_error=last_error, raw_output=last_raw)
