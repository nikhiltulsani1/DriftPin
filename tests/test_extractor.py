from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from driftpin.ingestion.extractor import extract_requirements
from driftpin.ingestion.parsers import SourceBlock
from driftpin.ledger.ledger import LedgerEntryType, RunLedger
from driftpin.providers.base import CompletionResult, LLMProvider, Message, ToolDefinition


def _blocks() -> list[SourceBlock]:
    return [
        SourceBlock(
            text="Users must be able to reset their password via an emailed link.",
            anchor="paragraph 1",
            source_doc_path="prd.md",
        ),
        SourceBlock(
            text="Sessions expire after 30 minutes of inactivity.",
            anchor="paragraph 2",
            source_doc_path="prd.md",
        ),
    ]


@pytest.mark.asyncio
async def test_extract_requirements_returns_empty_for_no_blocks(mock_provider_factory) -> None:
    provider = mock_provider_factory([])
    result = await extract_requirements(provider, [])
    assert result.candidate_requirements == []
    assert result.ambiguities == []
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_extract_requirements_keeps_verified_spans(mock_provider_factory) -> None:
    payload = {
        "candidate_requirements": [
            {
                "title": "Password reset",
                "description": "Users can reset passwords via email.",
                "source_span": "Users must be able to reset their password via an emailed link.",
                "risk_tier": "high",
            }
        ],
        "ambiguities": [],
    }
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps(payload), tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    result = await extract_requirements(provider, _blocks())

    assert len(result.candidate_requirements) == 1
    assert result.ambiguities == []


@pytest.mark.asyncio
async def test_extract_requirements_demotes_unverifiable_span_to_ambiguity(mock_provider_factory) -> None:
    payload = {
        "candidate_requirements": [
            {
                "title": "Hallucinated requirement",
                "description": "This was never in the document.",
                "source_span": "The system must support quantum encryption for all sessions.",
                "risk_tier": "critical",
            }
        ],
        "ambiguities": [],
    }
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps(payload), tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    result = await extract_requirements(provider, _blocks())

    assert result.candidate_requirements == []
    assert len(result.ambiguities) == 1
    assert "could not be verified" in result.ambiguities[0].description


@pytest.mark.asyncio
async def test_extract_requirements_ignores_llm_supplied_acs_and_defers_to_parser(mock_provider_factory) -> None:
    """The extraction LLM is instructed not to populate `acceptance_criteria`
    at all — ACs come from the deterministic parser (primary) or its
    per-requirement LLM fallback (secondary), never from the same call that
    finds requirement bodies. Even if a model ignores that instruction and
    returns some anyway, they're overwritten by the parser's result (empty
    here, since these test blocks have no Acceptance Criteria section at
    all) rather than trusted. This is the actual fix for the live
    extraction-breadth regression: one call is never asked to do both jobs."""
    payload = {
        "candidate_requirements": [
            {
                "title": "Password reset",
                "description": "Users can reset passwords via email.",
                "source_span": "Users must be able to reset their password via an emailed link.",
                "risk_tier": "high",
                "acceptance_criteria": ["A model-supplied AC that should be discarded."],
            }
        ],
        "ambiguities": [],
    }
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps(payload), tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    result = await extract_requirements(provider, _blocks())

    assert len(result.candidate_requirements) == 1
    assert result.candidate_requirements[0].acceptance_criteria == []
    assert provider.call_count == 1  # no AC-fallback call fired -- no AC section exists to trigger it


@pytest.mark.asyncio
async def test_extract_requirements_drops_unverifiable_nfr(mock_provider_factory) -> None:
    payload = {
        "candidate_requirements": [
            {
                "title": "Password reset",
                "description": "Users can reset passwords via email.",
                "source_span": "Users must be able to reset their password via an emailed link.",
                "risk_tier": "high",
            }
        ],
        "ambiguities": [],
        "candidate_nfrs": [
            {"text": "Sessions expire after 30 minutes of inactivity.", "scope": "global"},
            {"text": "The system guarantees 99.999% uptime across all regions.", "scope": "global"},
        ],
    }
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps(payload), tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    result = await extract_requirements(provider, _blocks())

    assert len(result.candidate_nfrs) == 1
    assert result.candidate_nfrs[0].text == "Sessions expire after 30 minutes of inactivity."


@pytest.mark.asyncio
async def test_extract_requirements_records_llm_call_on_ledger(
    mock_provider_factory, tmp_path: Path
) -> None:
    payload = {"candidate_requirements": [], "ambiguities": []}
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps(payload), tokens_in=42, tokens_out=7, stop_reason="end_turn")]
    )
    ledger = RunLedger(tmp_path, run_id="run-1")

    await extract_requirements(provider, _blocks(), ledger=ledger)

    entries = ledger.read_all()
    assert len(entries) == 1
    assert entries[0].entry_type == LedgerEntryType.LLM_CALL
    assert entries[0].agent_name == "requirement-extractor"
    assert entries[0].tokens_in == 42
    assert entries[0].tokens_out == 7


def _labeled_ac_blocks() -> list[SourceBlock]:
    return [
        SourceBlock(
            text=(
                "## Requirements\n\n"
                "R-01: Users must be able to reset their password via an emailed link.\n\n"
                "## Acceptance Criteria\n\n"
                "AC-01 (R-01): A reset email is sent within one minute of the request."
            ),
            anchor="body",
            source_doc_path="prd.md",
        )
    ]


@pytest.mark.asyncio
async def test_extract_requirements_fixture_k_deterministic_parser_needs_zero_extra_llm_calls(
    mock_provider_factory,
) -> None:
    """Fixture K: a machine-labeled Acceptance Criteria section is fully
    handled by the deterministic parser — the only LLM call in the ledger
    is the one main requirement-extraction call; no per-requirement AC
    fallback call fires at all."""
    payload = {
        "candidate_requirements": [
            {
                "title": "Password reset",
                "description": "Users can reset passwords via email.",
                "source_span": "Users must be able to reset their password via an emailed link.",
                "risk_tier": "high",
            }
        ],
        "ambiguities": [],
    }
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps(payload), tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    result = await extract_requirements(provider, _labeled_ac_blocks())

    assert provider.call_count == 1
    assert result.candidate_requirements[0].acceptance_criteria == [
        "A reset email is sent within one minute of the request."
    ]
    assert result.unassigned_acs == []


class _FailsOnCallProvider(LLMProvider):
    """Raises on specific 1-indexed call numbers, returns queued responses
    otherwise — simulates the per-requirement AC-fallback call failing
    (and its retry also failing) for one specific requirement, without
    needing to replicate provider-level HTTP retry mechanics already
    covered by the provider-layer tests."""

    name = "mock"
    model = "mock-model"

    def __init__(self, responses: list[CompletionResult], raise_on_call_numbers: set[int]) -> None:
        self._responses = list(responses)
        self._raise_on_call_numbers = raise_on_call_numbers
        self._call_count = 0

    async def validate(self) -> None:
        return None

    async def complete(
        self, messages: list[Message], system: str, tools: list[ToolDefinition] | None = None
    ) -> CompletionResult:
        return self._next()

    async def stream(self, messages: list[Message], system: str, tools: list[ToolDefinition] | None = None):
        result = self._next()
        yield result  # pragma: no cover - unused by complete_structured path

    async def complete_structured(
        self, messages: list[Message], system: str, json_schema: dict[str, Any]
    ) -> CompletionResult:
        return self._next()

    def _next(self) -> CompletionResult:
        self._call_count += 1
        if self._call_count in self._raise_on_call_numbers:
            raise RuntimeError("simulated AC-fallback call failure")
        return self._responses.pop(0)


def _unlabeled_ac_blocks() -> list[SourceBlock]:
    return [
        SourceBlock(
            text=(
                "## Requirements\n\n"
                "Users must be able to reset their password via an emailed link.\n"
                "Sessions expire after 30 minutes of inactivity.\n\n"
                "## Acceptance Criteria\n\n"
                "When a user requests a reset, an email with a link is sent within one minute. "
                "When a session has been idle for 30 minutes, the user is signed out automatically."
            ),
            anchor="body",
            source_doc_path="prd.md",
        )
    ]


@pytest.mark.asyncio
async def test_extract_requirements_fixture_l_unlabeled_ac_section_triggers_llm_fallback(
    tmp_path: Path,
) -> None:
    """Fixture L: an Acceptance Criteria section exists but has no
    parseable labels — the deterministic parser correctly finds zero, and
    the per-requirement LLM fallback fires once per requirement. The first
    requirement's fallback call fails, is retried once, fails again, and is
    recorded as `ac_extraction_failed` and listed in ASSUMPTIONS.md rather
    than silently left indistinguishable from 'genuinely has no ACs'. The
    second requirement's call succeeds normally."""
    main_payload = {
        "candidate_requirements": [
            {
                "title": "Password reset",
                "description": "Users can reset passwords via email.",
                "source_span": "Users must be able to reset their password via an emailed link.",
                "risk_tier": "high",
            },
            {
                "title": "Session expiry",
                "description": "Idle sessions expire.",
                "source_span": "Sessions expire after 30 minutes of inactivity.",
                "risk_tier": "medium",
            },
        ],
        "ambiguities": [],
    }
    second_requirement_ac_payload = {
        "acceptance_criteria": [
            "When a session has been idle for 30 minutes, the user is signed out automatically."
        ]
    }

    responses = [
        CompletionResult(content=json.dumps(main_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        # Call 2 and 3 (requirement #1's attempt + retry) raise via raise_on_call_numbers.
        CompletionResult(
            content=json.dumps(second_requirement_ac_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
        ),
    ]
    provider = _FailsOnCallProvider(responses, raise_on_call_numbers={2, 3})
    ledger = RunLedger(tmp_path, run_id="run-1")

    result = await extract_requirements(provider, _unlabeled_ac_blocks(), ledger=ledger)

    password_reset = next(c for c in result.candidate_requirements if c.title == "Password reset")
    session_expiry = next(c for c in result.candidate_requirements if c.title == "Session expiry")

    assert password_reset.ac_extraction_failed is True
    assert password_reset.acceptance_criteria == []
    assert session_expiry.ac_extraction_failed is False
    assert session_expiry.acceptance_criteria == [
        "When a session has been idle for 30 minutes, the user is signed out automatically."
    ]

    assumptions = ledger.assumptions_path.read_text(encoding="utf-8")
    assert "Password reset" in assumptions
    assert "acceptance-criteria extraction failed" in assumptions.lower()
