"""Structured-output conformance probe for local models.

Run once during `driftpin init` when the user selects a local (Ollama) model.
A model that fails schema conformance on 2 of 3 trivial probes is unfit for
this system's schema-first agents; the wizard must warn and require explicit
confirmation before proceeding rather than silently degrading.
"""

from __future__ import annotations

from pydantic import BaseModel

from driftpin.providers.base import LLMProvider, Message

REQUIRED_SUCCESSES = 2
TOTAL_PROBES = 3


class _ProbeSchema(BaseModel):
    answer: str
    confidence: float


_PROBE_PROMPTS = [
    "Respond with the word 'ok' as the answer and confidence 1.0.",
    "Respond with the capital of France as the answer and confidence 1.0.",
    "Respond with the sum of 2 and 2 as the answer (as text) and confidence 1.0.",
]


class ConformanceResult(BaseModel):
    successes: int
    total: int
    passed: bool


async def run_conformance_probe(provider: LLMProvider) -> ConformanceResult:
    schema = _ProbeSchema.model_json_schema()
    successes = 0
    for prompt in _PROBE_PROMPTS:
        try:
            result = await provider.complete_structured(
                messages=[Message(role="user", content=prompt)],
                system="You return only structured JSON matching the required schema.",
                json_schema=schema,
            )
            _ProbeSchema.model_validate_json(result.content)
            successes += 1
        except Exception:
            continue

    return ConformanceResult(
        successes=successes,
        total=TOTAL_PROBES,
        passed=successes >= REQUIRED_SUCCESSES,
    )
