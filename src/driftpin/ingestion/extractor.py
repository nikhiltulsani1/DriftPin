"""Requirement extraction: turns parsed document blocks into an ExtractionResult.

Every candidate's `source_span` is verified as a verbatim substring of the
source document after the LLM call returns. A candidate whose span cannot be
found is not trusted into the registry — it is demoted to an ambiguity for
human review instead, so a hallucinated quote can never become a requirement.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from driftpin.ingestion.parsers import SourceBlock
from driftpin.ingestion.text_utils import normalize_whitespace
from driftpin.ledger.ledger import RunLedger
from driftpin.providers.base import CompletionResult, LLMProvider, Message
from driftpin.providers.structured import complete_structured
from driftpin.schemas.requirements import CandidateAmbiguity, ExtractionResult

_AGENT_NAME = "requirement-extractor"

_SYSTEM_PROMPT = (
    "You are a precise requirements-extraction engine for a QA automation system. "
    "You extract only requirements explicitly stated or directly implied in the "
    "supplied document, and you never invent a source quote."
)


def _prompts_dir() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "prompts"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("Could not locate the prompts/ directory from ingestion/extractor.py")


def _render_extraction_prompt(document_text: str) -> str:
    template_path = _prompts_dir() / "extraction.md.j2"
    template = jinja2.Template(template_path.read_text(encoding="utf-8"))
    return template.render(document_text=document_text)


def _verify_source_spans(extraction: ExtractionResult, blocks: list[SourceBlock]) -> ExtractionResult:
    combined = normalize_whitespace(" ".join(block.text for block in blocks))
    verified_candidates = []
    ambiguities = list(extraction.ambiguities)

    for candidate in extraction.candidate_requirements:
        if normalize_whitespace(candidate.source_span) in combined:
            verified_candidates.append(candidate)
        else:
            ambiguities.append(
                CandidateAmbiguity(
                    description=(
                        f"Candidate requirement '{candidate.title}' cited a source span "
                        "that could not be verified verbatim in the source document; "
                        "discarded rather than trusted."
                    ),
                    source_span=candidate.source_span,
                )
            )

    return ExtractionResult(candidate_requirements=verified_candidates, ambiguities=ambiguities)


async def extract_requirements(
    provider: LLMProvider,
    blocks: list[SourceBlock],
    ledger: RunLedger | None = None,
) -> ExtractionResult:
    if not blocks:
        return ExtractionResult(candidate_requirements=[], ambiguities=[])

    document_text = "\n\n".join(f"[{block.anchor}] {block.text}" for block in blocks)
    prompt = _render_extraction_prompt(document_text)

    def _on_attempt(result: CompletionResult, attempt: int) -> None:
        if ledger is not None:
            ledger.record_llm_call(
                agent_name=_AGENT_NAME,
                provider=provider.name,
                model=provider.model,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=0.0,
                metadata={"attempt": attempt, "stop_reason": result.stop_reason},
            )

    parsed, _attempts = await complete_structured(
        provider,
        messages=[Message(role="user", content=prompt)],
        system=_SYSTEM_PROMPT,
        response_model=ExtractionResult,
        on_attempt=_on_attempt,
    )
    return _verify_source_spans(parsed, blocks)
