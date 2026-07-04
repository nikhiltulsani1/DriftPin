from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftpin.ingestion.extractor import extract_requirements
from driftpin.ingestion.parsers import SourceBlock
from driftpin.ledger.ledger import LedgerEntryType, RunLedger
from driftpin.providers.base import CompletionResult


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
