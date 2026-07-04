from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftpin.agents.orchestrator import build_traceability_matrix, run_pipeline
from driftpin.ledger.ledger import RunLedger
from driftpin.providers.base import CompletionResult
from driftpin.schemas.requirements import Requirement, RiskTier
from driftpin.schemas.test_cases import TestCase as CaseModel
from driftpin.schemas.test_cases import TestStep as StepModel


def _requirement(req_id: str, title: str = "A requirement", risk: RiskTier = RiskTier.HIGH) -> Requirement:
    return Requirement(
        requirement_id=req_id,
        title=title,
        description="Description.",
        source_span="Some verbatim span.",
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
        risk_tier=risk,
    )


def _case(case_id: str, scenario_id: str, requirement_ids: list[str]) -> CaseModel:
    return CaseModel(
        case_id=case_id,
        scenario_id=scenario_id,
        requirement_ids=requirement_ids,
        title="A test case",
        steps=[StepModel(step_number=1, action="Do something", expected_result="Something happens")],
        owning_agent="functional-tester",
        execution_recommendation="manual",
    )


def test_build_traceability_matrix_counts_coverage() -> None:
    requirements = [_requirement("R-1"), _requirement("R-2")]
    cases = [_case("TC-1", "S-1", ["R-1"]), _case("TC-2", "S-1", ["R-1", "R-2"])]

    rows = build_traceability_matrix(requirements, cases)

    by_id = {r.requirement_id: r for r in rows}
    assert by_id["R-1"].coverage_count == 2
    assert by_id["R-2"].coverage_count == 1
    assert by_id["R-2"].case_ids == ["TC-2"]


def test_build_traceability_matrix_flags_zero_coverage() -> None:
    requirements = [_requirement("R-1"), _requirement("R-2")]
    cases = [_case("TC-1", "S-1", ["R-1"])]

    rows = build_traceability_matrix(requirements, cases)

    by_id = {r.requirement_id: r for r in rows}
    assert by_id["R-2"].coverage_count == 0
    assert by_id["R-2"].case_ids == []


@pytest.mark.asyncio
async def test_run_pipeline_drops_hallucinated_requirement_reference(
    mock_provider_factory, tmp_path: Path
) -> None:
    requirements = [_requirement("R-1")]

    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [
            {
                "scenario_id": "S-1",
                "title": "Valid scenario",
                "requirement_ids": ["R-1"],
                "owning_agent": "functional-tester",
                "risk_tier": "high",
                "execution_recommendation": "manual",
                "recommendation_justification": "Low run frequency, high setup cost.",
            },
            {
                "scenario_id": "S-2",
                "title": "Hallucinated scenario",
                "requirement_ids": ["R-999"],
                "owning_agent": "functional-tester",
                "risk_tier": "high",
                "execution_recommendation": "manual",
                "recommendation_justification": "Low run frequency, high setup cost.",
            },
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
                "title": "Covers R-1",
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
        "summary": "All good.",
    }

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(suite_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(provider, requirements, run_id="run1", ledger=ledger)

    assert len(result.strategy.scenarios) == 1
    assert result.strategy.scenarios[0].scenario_id == "S-1"
    assert len(result.traceability) == 1
    assert result.traceability[0].coverage_count == 1

    assumptions = ledger.assumptions_path.read_text(encoding="utf-8")
    assert "S-2" in assumptions
    assert "R-999" in assumptions


@pytest.mark.asyncio
async def test_run_pipeline_raises_on_empty_requirements(mock_provider_factory) -> None:
    provider = mock_provider_factory([])
    with pytest.raises(ValueError, match="empty"):
        await run_pipeline(provider, [], run_id="run1")
