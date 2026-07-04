"""Generic agent execution: render the system prompt, call the provider, validate, log.

Every content-generating agent (test-architect, functional-tester, reviewer)
runs through this single function. Agent-specific behavior lives entirely in
the YAML definition and its jinja2 template — there is no per-agent Python
branching here, by design.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
from pydantic import BaseModel

from driftpin.agents.loader import AgentDefinition, resolve_output_schema
from driftpin.ledger.ledger import RunLedger
from driftpin.paths import find_repo_dir
from driftpin.providers.base import CompletionResult, LLMProvider, Message
from driftpin.providers.structured import complete_structured

_TRIGGER_MESSAGE = "Produce the structured output now, following the schema and instructions above."


def _render_system_prompt(template_filename: str, context: dict[str, Any]) -> str:
    template_path = find_repo_dir("prompts", Path(__file__)) / template_filename
    template = jinja2.Template(template_path.read_text(encoding="utf-8"))
    return template.render(**context)


async def run_agent(
    definition: AgentDefinition,
    provider: LLMProvider,
    context: dict[str, Any],
    ledger: RunLedger | None = None,
) -> BaseModel:
    system_prompt = _render_system_prompt(definition.system_prompt, context)
    response_model = resolve_output_schema(definition.output_schema)

    def _on_attempt(result: CompletionResult, attempt: int) -> None:
        if ledger is not None:
            ledger.record_llm_call(
                agent_name=definition.name,
                provider=provider.name,
                model=provider.model,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=0.0,
                metadata={"attempt": attempt, "stop_reason": result.stop_reason},
            )

    parsed, _attempts = await complete_structured(
        provider,
        messages=[Message(role="user", content=_TRIGGER_MESSAGE)],
        system=system_prompt,
        response_model=response_model,
        on_attempt=_on_attempt,
    )
    return parsed
