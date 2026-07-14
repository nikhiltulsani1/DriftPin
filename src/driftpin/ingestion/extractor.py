"""Requirement extraction: turns parsed document blocks into an ExtractionResult.

Every candidate's `source_span` is verified as a verbatim substring of the
source document after the LLM call returns. A candidate whose span cannot be
found is not trusted into the registry — it is demoted to an ambiguity for
human review instead, so a hallucinated quote can never become a requirement.

Acceptance criteria are NOT extracted by the same LLM call as requirement
bodies — asking one call to find every requirement AND correctly cross-
reference every acceptance criterion back to the right one was measured
live to silently drop most requirements under the combined load (see
DESIGN_DECISIONS.md's "Registry AC/NFR ingestion" section). Instead, a
deterministic parser (`ac_parser.py`) handles the common case — machine-
labeled ACs like "AC-01 (R-02): ..." — at zero LLM-call cost, and a small
per-requirement LLM fallback only fires when that parser finds an
Acceptance-Criteria-like section it couldn't parse any labels out of.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from driftpin.agents.loader import load_agent_definition
from driftpin.agents.runtime import run_agent
from driftpin.ingestion.ac_parser import extract_ac_section_text, parse_acceptance_criteria
from driftpin.ingestion.parsers import SourceBlock
from driftpin.ingestion.text_utils import normalize_whitespace
from driftpin.ledger.ledger import RunLedger
from driftpin.paths import find_repo_dir
from driftpin.providers.base import CompletionResult, LLMProvider, Message, ServerExhaustedError
from driftpin.providers.structured import complete_structured
from driftpin.schemas.requirements import (
    ACFillResult,
    CandidateAmbiguity,
    CandidateRequirement,
    ExtractionResult,
)

_AGENT_NAME = "requirement-extractor"
_AC_FALLBACK_AGENT_NAME = "ac_fallback"
_MAX_AC_FALLBACK_ATTEMPTS = 2  # one try + one retry per requirement

_SYSTEM_PROMPT = (
    "You are a precise requirements-extraction engine for a QA automation system. "
    "You extract only requirements explicitly stated or directly implied in the "
    "supplied document, and you never invent a source quote."
)


def _render_extraction_prompt(document_text: str) -> str:
    template_path = find_repo_dir("prompts", Path(__file__)) / "extraction.md.j2"
    template = jinja2.Template(template_path.read_text(encoding="utf-8"))
    return template.render(document_text=document_text)


def _verify_source_spans(extraction: ExtractionResult, blocks: list[SourceBlock]) -> ExtractionResult:
    combined = normalize_whitespace(" ".join(block.text for block in blocks))
    verified_candidates = []
    ambiguities = list(extraction.ambiguities)

    for candidate in extraction.candidate_requirements:
        if normalize_whitespace(candidate.source_span) in combined:
            verified_acs = [
                ac for ac in candidate.acceptance_criteria if normalize_whitespace(ac) in combined
            ]
            verified_candidates.append(candidate.model_copy(update={"acceptance_criteria": verified_acs}))
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

    verified_nfrs = [nfr for nfr in extraction.candidate_nfrs if normalize_whitespace(nfr.text) in combined]

    return ExtractionResult(
        candidate_requirements=verified_candidates,
        ambiguities=ambiguities,
        candidate_nfrs=verified_nfrs,
    )


async def _fill_acceptance_criteria_via_llm(
    provider: LLMProvider,
    candidates: list[CandidateRequirement],
    ac_section_text: str,
    ledger: RunLedger | None,
) -> tuple[list[CandidateRequirement], list[str]]:
    """Per-requirement AC fallback: one small call per requirement (that
    requirement's body + the AC section text), never one call for the
    whole document — the same enumerate-then-fill completeness pattern
    used for functional-tester and reviewer elsewhere in this project. A
    requirement whose call fails gets one retry; if that also fails, it's
    marked `ac_extraction_failed` rather than silently left indistinguishable
    from 'genuinely has zero ACs'. `ServerExhaustedError` is never retried
    here — retrying into an already-exhausted provider pool only makes
    things worse, so it propagates immediately instead."""
    ac_fallback_def = load_agent_definition(_AC_FALLBACK_AGENT_NAME)
    updated: list[CandidateRequirement] = []
    failed_titles: list[str] = []

    for candidate in candidates:
        result: ACFillResult | None = None
        for _attempt in range(_MAX_AC_FALLBACK_ATTEMPTS):
            try:
                raw = await run_agent(
                    ac_fallback_def,
                    provider,
                    context={"requirement": candidate, "ac_section_text": ac_section_text},
                    ledger=ledger,
                )
                assert isinstance(raw, ACFillResult)
                result = raw
                break
            except ServerExhaustedError:
                raise
            except Exception:
                result = None
                continue

        if result is None:
            failed_titles.append(candidate.title)
            updated.append(candidate.model_copy(update={"ac_extraction_failed": True}))
        else:
            updated.append(candidate.model_copy(update={"acceptance_criteria": result.acceptance_criteria}))

    return updated, failed_titles


async def _fill_acceptance_criteria(
    provider: LLMProvider,
    extraction: ExtractionResult,
    document_text: str,
    ledger: RunLedger | None,
) -> ExtractionResult:
    parsed = parse_acceptance_criteria(document_text, extraction.candidate_requirements)

    updated_candidates = [
        candidate.model_copy(
            update={"acceptance_criteria": parsed.acs_by_requirement_span.get(candidate.source_span, [])}
        )
        for candidate in extraction.candidate_requirements
    ]

    # LLM fallback fires only when there's real evidence ACs exist but
    # weren't machine-parseable — an AC-like heading section with zero
    # labels found. A document with no such heading at all is treated as
    # genuinely having no ACs (a valid, common case), not a parsing
    # failure — firing the fallback there would spend LLM calls on every
    # ordinary PRD that simply lacks acceptance criteria.
    if parsed.found_ac_section and parsed.labels_parsed == 0:
        ac_section_text = extract_ac_section_text(document_text) or ""
        updated_candidates, failed_titles = await _fill_acceptance_criteria_via_llm(
            provider, updated_candidates, ac_section_text, ledger
        )
        for title in failed_titles:
            if ledger is not None:
                ledger.record_assumption(
                    heading=f"Acceptance-criteria extraction failed for requirement '{title}'",
                    detail=(
                        "Deterministic parsing found an Acceptance Criteria section with no "
                        "parseable labels, and the per-requirement LLM fallback also failed "
                        "(after one retry). This requirement's acceptance_criteria could not "
                        "be populated — human attention required."
                    ),
                )

    return extraction.model_copy(
        update={"candidate_requirements": updated_candidates, "unassigned_acs": list(parsed.unassigned_acs)}
    )


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

    try:
        parsed, _attempts = await complete_structured(
            provider,
            messages=[Message(role="user", content=prompt)],
            system=_SYSTEM_PROMPT,
            response_model=ExtractionResult,
            on_attempt=_on_attempt,
        )
    except ServerExhaustedError as exc:
        if ledger is not None:
            ledger.record_assumption(
                heading=f"{_AGENT_NAME}: provider capacity exhausted (pattern '{exc.matched_pattern}')",
                detail=str(exc),
            )
        raise

    verified = _verify_source_spans(parsed, blocks)
    return await _fill_acceptance_criteria(provider, verified, document_text, ledger)
