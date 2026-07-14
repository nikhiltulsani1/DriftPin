from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftpin.cli.actions import (
    DocumentNotFoundError,
    EmptyRegistryError,
    GenerationAbortedError,
    artifact_filename,
    derive_source_slug,
    open_registry,
    run_generate_cases,
    run_generate_strategy,
    run_ingest,
)
from driftpin.config.settings import driftpin_dir
from driftpin.providers.base import CompletionResult
from driftpin.schemas.requirements import AcceptanceCriterion, RegistryFile, Requirement, RiskTier


def _write_registry(project_root: Path, requirements: list[Requirement]) -> None:
    registry_dir = driftpin_dir(project_root)
    registry_dir.mkdir(parents=True, exist_ok=True)
    registry_file = RegistryFile(requirements=requirements)
    (registry_dir / "requirements.json").write_text(
        registry_file.model_dump_json(indent=2), encoding="utf-8"
    )


def _requirement(req_id: str = "R-1", source_doc_path: str = "prd.md") -> Requirement:
    return Requirement(
        requirement_id=req_id,
        title="A requirement",
        description="Description.",
        source_span="Some verbatim span.",
        source_doc_path=source_doc_path,
        source_doc_hash="hash-a",
        risk_tier=RiskTier.HIGH,
    )


def test_artifact_filename_capitalizes_prefix_and_includes_timestamp() -> None:
    filename = artifact_filename("cases", "xlsx")

    assert filename.startswith("Cases_")
    assert filename.endswith(".xlsx")
    # "Cases_20260705-113906.xlsx" -> timestamp segment is 15 chars (YYYYMMDD-HHMMSS)
    timestamp_segment = filename.removeprefix("Cases_").removesuffix(".xlsx")
    assert len(timestamp_segment) == 15
    assert timestamp_segment[8] == "-"


def test_artifact_filename_differs_by_prefix_and_extension() -> None:
    excel_name = artifact_filename("cases", "xlsx")
    markdown_name = artifact_filename("cases", "md")
    strategy_name = artifact_filename("strategy", "json")

    assert excel_name != markdown_name
    assert excel_name.startswith("Cases_")
    assert strategy_name.startswith("Strategy_")


def test_artifact_filename_includes_source_slug_when_given() -> None:
    filename = artifact_filename("cases", "xlsx", source_slug="prd-1-voice-assistant")

    assert filename.startswith("Cases_prd-1-voice-assistant_")
    assert filename.endswith(".xlsx")


def test_derive_source_slug_returns_none_for_no_requirements() -> None:
    assert derive_source_slug([]) is None


def test_derive_source_slug_uses_single_source_doc_stem() -> None:
    requirements = [
        _requirement("R-1", source_doc_path="evals/golden/prd-1-voice-assistant-fab.md"),
        _requirement("R-2", source_doc_path="evals/golden/prd-1-voice-assistant-fab.md"),
    ]

    assert derive_source_slug(requirements) == "prd-1-voice-assistant-fab"


def test_derive_source_slug_joins_up_to_two_distinct_sources() -> None:
    requirements = [
        _requirement("R-1", source_doc_path="prd-a.md"),
        _requirement("R-2", source_doc_path="prd-b.md"),
    ]

    assert derive_source_slug(requirements) == "prd-a-prd-b"


def test_derive_source_slug_falls_back_for_more_than_two_sources() -> None:
    requirements = [
        _requirement("R-1", source_doc_path="prd-a.md"),
        _requirement("R-2", source_doc_path="prd-b.md"),
        _requirement("R-3", source_doc_path="prd-c.md"),
    ]

    assert derive_source_slug(requirements) == "multi-source"


def test_derive_source_slug_sanitizes_unsafe_filename_characters() -> None:
    requirements = [_requirement("R-1", source_doc_path="My PRD (v2)!.md")]

    slug = derive_source_slug(requirements)

    assert slug is not None
    assert " " not in slug
    assert "(" not in slug
    assert "!" not in slug


@pytest.mark.asyncio
async def test_run_ingest_raises_for_missing_document(tmp_path: Path, mock_provider_factory) -> None:
    provider = mock_provider_factory([])
    missing = tmp_path / "does-not-exist.md"

    with pytest.raises(DocumentNotFoundError):
        await run_ingest(provider, tmp_path, [missing])


@pytest.mark.asyncio
async def test_run_ingest_adds_requirements_and_saves_registry(
    tmp_path: Path, mock_provider_factory
) -> None:
    doc_path = tmp_path / "prd.md"
    doc_path.write_text("Users must be able to reset their password via email.\n", encoding="utf-8")

    payload = {
        "candidate_requirements": [
            {
                "title": "Password reset",
                "description": "Users can reset passwords via email.",
                "source_span": "Users must be able to reset their password via email.",
                "risk_tier": "high",
            }
        ],
        "ambiguities": [],
    }
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps(payload), tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    outcome = await run_ingest(provider, tmp_path, [doc_path])

    assert len(outcome.outcomes) == 1
    assert outcome.outcomes[0].added_count == 1
    registry = open_registry(tmp_path)
    assert len(registry.requirements) == 1


@pytest.mark.asyncio
async def test_run_generate_strategy_raises_on_empty_registry(tmp_path: Path, mock_provider_factory) -> None:
    provider = mock_provider_factory([])
    with pytest.raises(EmptyRegistryError):
        await run_generate_strategy(provider, tmp_path, tmp_path / "out")


@pytest.mark.asyncio
async def test_run_generate_strategy_reports_stage_progress(tmp_path: Path, mock_provider_factory) -> None:
    _write_registry(tmp_path, [_requirement()])
    payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [
            {
                "scenario_id": "S-1",
                "title": "Scenario",
                "requirement_ids": ["R-1"],
                "owning_agent": "functional-tester",
                "risk_tier": "high",
                "execution_recommendation": "manual",
                "recommendation_justification": "Low frequency, high setup cost.",
            }
        ],
        "coverage_notes": "",
    }
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps(payload), tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    stages: list[str] = []
    outcome = await run_generate_strategy(provider, tmp_path, tmp_path / "out", on_stage=stages.append)

    assert stages == [
        "consistency-checker",
        "test-architect",
        "test-architect (requirement coverage check)",
    ]
    assert len(outcome.strategy.scenarios) == 1


@pytest.mark.asyncio
async def test_run_generate_strategy_aborts_when_consistency_report_declined(
    tmp_path: Path, mock_provider_factory
) -> None:
    """Declining the spec-consistency report must stop the pipeline before
    test-architect ever runs -- an empty provider queue makes that
    ordering self-verifying: if the architect call fired anyway, the mock
    provider would raise its own queue-exhausted assertion instead."""
    requirement = Requirement(
        requirement_id="R-1",
        title="A requirement",
        description="A user may not set a budget of zero.",
        source_span="A user may not set a budget of zero.",
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
        risk_tier=RiskTier.HIGH,
        acceptance_criteria=[
            AcceptanceCriterion(ac_id="AC-1", text="Budget entry field rejects values below 1.")
        ],
    )
    _write_registry(tmp_path, [requirement])
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps({"verdict": "threshold_mismatch", "explanation": "Zero vs below 1."}), tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    with pytest.raises(GenerationAbortedError):
        await run_generate_strategy(
            provider, tmp_path, tmp_path / "out", on_consistency_report=lambda _report: False
        )


@pytest.mark.asyncio
async def test_run_generate_strategy_writes_json_and_markdown(tmp_path: Path, mock_provider_factory) -> None:
    _write_registry(tmp_path, [_requirement()])
    payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [
            {
                "scenario_id": "S-1",
                "title": "Scenario",
                "requirement_ids": ["R-1"],
                "owning_agent": "functional-tester",
                "risk_tier": "high",
                "execution_recommendation": "manual",
                "recommendation_justification": "Low frequency, high setup cost.",
            }
        ],
        "coverage_notes": "",
    }
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps(payload), tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    out_dir = tmp_path / "out"
    outcome = await run_generate_strategy(provider, tmp_path, out_dir)

    assert outcome.strategy_path.exists()
    assert outcome.strategy_path.suffix == ".json"
    assert outcome.markdown_path.exists()
    assert outcome.markdown_path.suffix == ".md"
    markdown_text = outcome.markdown_path.read_text(encoding="utf-8")
    assert "Req-1" in markdown_text
    assert "R-1" not in markdown_text


@pytest.mark.asyncio
async def test_run_generate_cases_raises_on_empty_registry(tmp_path: Path, mock_provider_factory) -> None:
    provider = mock_provider_factory([])
    with pytest.raises(EmptyRegistryError):
        await run_generate_cases(provider, tmp_path, tmp_path / "out")


@pytest.mark.asyncio
async def test_run_generate_cases_writes_excel_and_markdown(tmp_path: Path, mock_provider_factory) -> None:
    _write_registry(tmp_path, [_requirement()])

    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [
            {
                "scenario_id": "S-1",
                "title": "Scenario",
                "requirement_ids": ["R-1"],
                "owning_agent": "functional-tester",
                "risk_tier": "high",
                "execution_recommendation": "manual",
                "recommendation_justification": "Low frequency, high setup cost.",
            }
        ],
        "coverage_notes": "",
    }
    fill_payload = {
        "cases": [
            {
                "case_id": "TC-placeholder",
                "scenario_id": "S-1",
                "requirement_ids": ["R-1"],
                "title": "Case",
                "preconditions": "",
                "steps": [{"step_number": 1, "action": "do", "expected_result": "ok"}],
                "owning_agent": "functional-tester",
                "execution_recommendation": "manual",
            }
        ],
    }
    review_payload = {"findings": []}

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        ]
    )

    stages: list[str] = []
    out_dir = tmp_path / "out"
    outcome = await run_generate_cases(provider, tmp_path, out_dir, on_stage=stages.append)

    assert stages == [
        "consistency-checker",
        "test-architect",
        "test-architect (requirement coverage check)",
        "functional-tester (1/1: S-1)",
        "reviewer (structural)",
        "reviewer (semantic groups)",
        "reviewer (fallback)",
    ]
    assert outcome.excel_path.exists()
    assert outcome.markdown_path.exists()
