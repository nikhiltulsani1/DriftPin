from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftpin.cli.actions import (
    DocumentNotFoundError,
    EmptyRegistryError,
    artifact_filename,
    open_registry,
    run_generate_cases,
    run_generate_strategy,
    run_ingest,
)
from driftpin.config.settings import driftpin_dir
from driftpin.providers.base import CompletionResult
from driftpin.schemas.requirements import RegistryFile, Requirement, RiskTier


def _write_registry(project_root: Path, requirements: list[Requirement]) -> None:
    registry_dir = driftpin_dir(project_root)
    registry_dir.mkdir(parents=True, exist_ok=True)
    registry_file = RegistryFile(requirements=requirements)
    (registry_dir / "requirements.json").write_text(
        registry_file.model_dump_json(indent=2), encoding="utf-8"
    )


def _requirement(req_id: str = "R-1") -> Requirement:
    return Requirement(
        requirement_id=req_id,
        title="A requirement",
        description="Description.",
        source_span="Some verbatim span.",
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
        risk_tier=RiskTier.HIGH,
    )


def test_artifact_filename_includes_prefix_timestamp_and_run_id() -> None:
    filename = artifact_filename("cases", "abc123def456", "xlsx")

    assert filename.startswith("cases_")
    assert filename.endswith("_abc123def456.xlsx")
    # "cases_20260705-113906_abc123def456.xlsx" -> timestamp segment is 15 chars (YYYYMMDD-HHMMSS)
    timestamp_segment = filename.removeprefix("cases_").removesuffix("_abc123def456.xlsx")
    assert len(timestamp_segment) == 15
    assert timestamp_segment[8] == "-"


def test_artifact_filename_differs_by_prefix_and_extension() -> None:
    excel_name = artifact_filename("cases", "run1", "xlsx")
    markdown_name = artifact_filename("cases", "run1", "md")
    strategy_name = artifact_filename("strategy", "run1", "json")

    assert excel_name != markdown_name
    assert excel_name.startswith("cases_")
    assert strategy_name.startswith("strategy_")


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
        await run_generate_strategy(provider, tmp_path)


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
    outcome = await run_generate_strategy(provider, tmp_path, on_stage=stages.append)

    assert stages == ["test-architect"]
    assert len(outcome.strategy.scenarios) == 1


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
    suite_payload = {
        "suite_id": "suite-run1",
        "strategy_id": "strategy-run1",
        "cases": [
            {
                "case_id": "TC-1",
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
    review_payload = {
        "review_id": "review-run1",
        "target_run_id": "run1",
        "findings": [],
        "passed": True,
        "summary": "ok",
    }

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(suite_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        ]
    )

    stages: list[str] = []
    out_dir = tmp_path / "out"
    outcome = await run_generate_cases(provider, tmp_path, out_dir, on_stage=stages.append)

    assert stages == ["test-architect", "functional-tester", "reviewer"]
    assert outcome.excel_path.exists()
    assert outcome.markdown_path.exists()
