from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from driftpin.agents.orchestrator import (
    MAX_SCENARIOS_WITHOUT_CONFIRMATION,
    TooManyScenariosError,
    _build_review_requirement,
    _compose_review_report,
    _find_fallback_rule_requirements,
    _resolve_requirement_nfrs,
    _run_structural_review,
    build_traceability_matrix,
    generate_strategy_only,
    run_pipeline,
)
from driftpin.ledger.ledger import LedgerEntryType, RunLedger
from driftpin.providers.base import (
    CompletionResult,
    LLMProvider,
    Message,
    PayloadTooHeavyError,
    ToolDefinition,
)
from driftpin.schemas.requirements import (
    AcceptanceCriterion,
    NfrScope,
    NonFunctionalRequirement,
    Requirement,
    RiskTier,
)
from driftpin.schemas.review import FindingSeverity, ReviewerFinding
from driftpin.schemas.strategy import ExecutionRecommendation, OwningAgent, Scenario
from driftpin.schemas.strategy import TestStrategy as StrategyModel
from driftpin.schemas.test_cases import TestCase as CaseModel
from driftpin.schemas.test_cases import TestStep as StepModel
from driftpin.schemas.test_cases import TestSuite as SuiteModel


def _requirement(
    req_id: str,
    title: str = "A requirement",
    risk: RiskTier = RiskTier.HIGH,
    description: str = "Description.",
    source_span: str = "Some verbatim span.",
    acceptance_criteria: list[str] | None = None,
    nfr_ids: list[str] | None = None,
) -> Requirement:
    return Requirement(
        requirement_id=req_id,
        title=title,
        description=description,
        source_span=source_span,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
        risk_tier=risk,
        acceptance_criteria=[
            AcceptanceCriterion(ac_id=f"AC-{req_id}-{i}", text=text)
            for i, text in enumerate(acceptance_criteria or [])
        ],
        nfr_ids=nfr_ids or [],
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


def _group_review_payload(findings: list[dict] | None = None) -> dict:
    """A `GroupReviewResult`-shaped mock response — what one per-group
    semantic review call (or the fallback call) actually returns now, not
    the old single `ReviewReport` shape a whole-suite call used to."""
    return {"findings": findings or []}


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
    fill_payload = {
        "cases": [
            {
                "case_id": "TC-placeholder",
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
    review_payload = _group_review_payload()

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

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


class _SystemPromptCapturingProvider(LLMProvider):
    """Records every rendered system prompt it's called with, so a test can
    assert on what the functional-tester template actually produced — not
    just on the canned response it was fed back."""

    name = "mock"
    model = "mock-model"

    def __init__(self, responses: list[CompletionResult]) -> None:
        self._responses = list(responses)
        self.system_prompts: list[str] = []

    async def validate(self) -> None:
        return None

    async def complete(
        self, messages: list[Message], system: str, tools: list[ToolDefinition] | None = None
    ) -> CompletionResult:
        return self._next_response(system)

    async def stream(self, messages: list[Message], system: str, tools: list[ToolDefinition] | None = None):
        result = self._next_response(system)
        yield result  # pragma: no cover - unused by complete_structured path

    async def complete_structured(
        self, messages: list[Message], system: str, json_schema: dict[str, Any]
    ) -> CompletionResult:
        return self._next_response(system)

    def _next_response(self, system: str) -> CompletionResult:
        self.system_prompts.append(system)
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_functional_tester_prompt_only_includes_scenario_referenced_requirements(
    tmp_path: Path,
) -> None:
    requirements = [
        _requirement("R-1", title="Password Reset Requirement"),
        _requirement("R-2", title="Unrelated Reporting Requirement"),
    ]

    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [
            {
                "scenario_id": "S-1",
                "title": "Password reset scenario",
                "requirement_ids": ["R-1"],
                "owning_agent": "functional-tester",
                "risk_tier": "high",
                "execution_recommendation": "manual",
                "recommendation_justification": "Low run frequency, high setup cost.",
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
                "title": "Covers R-1",
                "preconditions": "",
                "steps": [{"step_number": 1, "action": "do", "expected_result": "ok"}],
                "owning_agent": "functional-tester",
                "execution_recommendation": "manual",
            }
        ],
    }
    review_payload = _group_review_payload()
    # R-2 is deliberately never referenced by any scenario in this fixture
    # (that's the point being tested), which means the requirement-to-
    # scenario completeness check legitimately finds it missing and fires
    # its own scoped refill call -- exhausted here via 2 empty responses,
    # unrelated to what this test actually asserts on.
    empty_refill_payload = {"strategy_id": "strategy-run1", "scenarios": [], "coverage_notes": ""}

    provider = _SystemPromptCapturingProvider(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content=json.dumps(empty_refill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(empty_refill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(content=json.dumps(fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    await run_pipeline(provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0)

    # system_prompts: [0] initial architect call, [1]-[2] the 2 requirement-
    # completeness refill attempts for the never-referenced R-2, [3] the
    # functional-tester fill call this test actually cares about.
    functional_tester_prompt = provider.system_prompts[3]
    assert "Password Reset Requirement" in functional_tester_prompt
    assert "Unrelated Reporting Requirement" not in functional_tester_prompt


def _scenario_payload(scenario_id: str, requirement_ids: list[str]) -> dict:
    return {
        "scenario_id": scenario_id,
        "title": f"Scenario {scenario_id}",
        "requirement_ids": requirement_ids,
        "owning_agent": "functional-tester",
        "risk_tier": "high",
        "execution_recommendation": "manual",
        "recommendation_justification": "Low run frequency, high setup cost.",
    }


def _fill_payload(
    scenario_id: str, requirement_ids: list[str], owning_agent: str = "functional-tester"
) -> dict:
    return {
        "cases": [
            {
                "case_id": "TC-placeholder",
                "scenario_id": scenario_id,
                "requirement_ids": requirement_ids,
                "title": f"Case for {scenario_id}",
                "preconditions": "",
                "steps": [{"step_number": 1, "action": "do", "expected_result": "ok"}],
                "owning_agent": owning_agent,
                "execution_recommendation": "manual",
            }
        ],
    }


_REVIEW_PAYLOAD = _group_review_payload()


@pytest.mark.asyncio
async def test_fill_stage_renumbers_cases_and_forces_scenario_fields(
    mock_provider_factory, tmp_path: Path
) -> None:
    """Two scenarios, each fill call returning a wrong scenario_id/owning_agent
    on purpose — Python must override both deterministically, and case IDs
    must be renumbered sequentially across scenarios, not left as two
    colliding "TC-placeholder" values."""
    requirements = [_requirement("R-1"), _requirement("R-2")]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [
            _scenario_payload("S-1", ["R-1"]),
            _scenario_payload("S-2", ["R-2"]),
        ],
        "coverage_notes": "",
    }
    # Both fill responses claim the wrong scenario_id/owning_agent on purpose.
    fill_payload_1 = _fill_payload("S-2", ["R-1"], owning_agent="api-backend-tester")
    fill_payload_2 = _fill_payload("S-1", ["R-2"], owning_agent="api-backend-tester")

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(fill_payload_1), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(fill_payload_2), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(_REVIEW_PAYLOAD), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert [c.case_id for c in result.suite.cases] == ["TC-1", "TC-2"]
    case_by_scenario = {c.scenario_id: c for c in result.suite.cases}
    assert case_by_scenario["S-1"].owning_agent.value == "functional-tester"
    assert case_by_scenario["S-2"].owning_agent.value == "functional-tester"


@pytest.mark.asyncio
async def test_fill_stage_refills_scenario_that_returns_empty_then_succeeds(
    mock_provider_factory, tmp_path: Path
) -> None:
    requirements = [_requirement("R-1")]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    empty_fill_payload = {"cases": []}
    real_fill_payload = _fill_payload("S-1", ["R-1"])

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(empty_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(real_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(_REVIEW_PAYLOAD), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert len(result.suite.cases) == 1
    assert result.failed_scenario_ids == []

    agent_steps = [e for e in ledger.read_all() if e.entry_type == LedgerEntryType.AGENT_STEP]
    fill_step = next(e for e in agent_steps if e.agent_name == "functional-tester")
    assert fill_step.metadata["scenarios_refilled"] == 1
    assert fill_step.metadata["scenarios_failed"] == 0


@pytest.mark.asyncio
async def test_fill_stage_marks_generation_failed_after_exhausting_refills(
    mock_provider_factory, tmp_path: Path
) -> None:
    requirements = [_requirement("R-1")]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    empty_fill_payload = {"cases": []}

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(empty_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(empty_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(empty_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            # No review response needed: a scenario with zero cases makes its
            # review group empty too, so `_review_group_with_splitting` skips
            # the LLM call entirely rather than reviewing nothing.
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert result.failed_scenario_ids == ["S-1"]
    assert result.suite.cases == []

    assumptions = ledger.assumptions_path.read_text(encoding="utf-8")
    assert "GENERATION_FAILED" in assumptions
    assert "S-1" in assumptions

    agent_steps = [e for e in ledger.read_all() if e.entry_type == LedgerEntryType.AGENT_STEP]
    fill_step = next(e for e in agent_steps if e.agent_name == "functional-tester")
    assert fill_step.metadata["scenarios_failed"] == 1


@pytest.mark.asyncio
async def test_run_pipeline_raises_too_many_scenarios_without_confirmation(
    mock_provider_factory,
) -> None:
    requirements = [_requirement("R-1")]
    scenario_count = MAX_SCENARIOS_WITHOUT_CONFIRMATION + 1
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload(f"S-{i}", ["R-1"]) for i in range(1, scenario_count + 1)],
        "coverage_notes": "",
    }
    provider = mock_provider_factory(
        [CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn")]
    )

    with pytest.raises(TooManyScenariosError) as exc_info:
        await run_pipeline(provider, requirements, run_id="run1", fill_call_delay_seconds=0)

    assert exc_info.value.scenario_count == scenario_count


@pytest.mark.asyncio
async def test_run_pipeline_proceeds_past_scale_guard_when_confirmed(
    mock_provider_factory,
) -> None:
    requirements = [_requirement("R-1")]
    scenario_count = MAX_SCENARIOS_WITHOUT_CONFIRMATION + 1
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload(f"S-{i}", ["R-1"]) for i in range(1, scenario_count + 1)],
        "coverage_notes": "",
    }
    responses = [
        CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn")
    ]
    for i in range(1, scenario_count + 1):
        responses.append(
            CompletionResult(
                content=json.dumps(_fill_payload(f"S-{i}", ["R-1"])),
                tokens_in=1,
                tokens_out=1,
                stop_reason="end_turn",
            )
        )
    group_count = -(-scenario_count // 3)  # ceiling division, matches _REVIEW_GROUP_SIZE = 3
    for _ in range(group_count):
        responses.append(
            CompletionResult(content=json.dumps(_REVIEW_PAYLOAD), tokens_in=1, tokens_out=1, stop_reason="end_turn")
        )
    provider = mock_provider_factory(responses)

    result = await run_pipeline(
        provider,
        requirements,
        run_id="run1",
        fill_call_delay_seconds=0,
        on_scenario_count_check=lambda count: True,
    )

    assert len(result.suite.cases) == scenario_count


def test_compose_review_report_fails_when_any_blocker_present() -> None:
    review = _compose_review_report(
        "review-1",
        "run1",
        structural_findings=[],
        semantic_findings=[ReviewerFinding(severity=FindingSeverity.BLOCKER, subject_id="TC-1", description="Bad.")],
        fallback_findings=[],
    )

    assert review.passed is False


def test_compose_review_report_passes_when_no_blocker_anywhere() -> None:
    review = _compose_review_report(
        "review-1",
        "run1",
        structural_findings=[
            ReviewerFinding(severity=FindingSeverity.MINOR, subject_id="TC-1", description="Minor thing.")
        ],
        semantic_findings=[],
        fallback_findings=[],
    )

    assert review.passed is True


def test_compose_review_report_does_not_fail_for_assumption_findings_only() -> None:
    review = _compose_review_report(
        "review-1",
        "run1",
        structural_findings=[],
        semantic_findings=[
            ReviewerFinding(
                severity=FindingSeverity.ASSUMPTION,
                subject_id="TC-1",
                description="Invented an endpoint not in the requirement.",
            )
        ],
        fallback_findings=[],
    )

    assert review.passed is True


def test_compose_review_report_fails_on_fallback_blocker_alone() -> None:
    review = _compose_review_report(
        "review-1",
        "run1",
        structural_findings=[],
        semantic_findings=[],
        fallback_findings=[
            ReviewerFinding(severity=FindingSeverity.BLOCKER, subject_id="TC-1", description="Rejects a never-drop input.")
        ],
    )

    assert review.passed is False
    assert len(review.findings) == 1


@pytest.mark.asyncio
async def test_fixture_a_mutually_exclusive_outcome_forces_blocker_and_failed_review(
    mock_provider_factory, tmp_path: Path
) -> None:
    """Replicates the Groq TC-3 class: a single step asserts two outcomes
    that can't both be true (text lands in the quick-note composer AND is
    routed to a specific different service). The per-group semantic review
    call is mocked as having correctly caught this (a live-model concern,
    not something a mocked-provider test can verify) — what this test
    verifies is that Python's merge correctly fails the overall review the
    moment any group's findings include a blocker."""
    requirements = [_requirement("R-1", title="Unrecognised input handling")]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    contradictory_fill_payload = {
        "cases": [
            {
                "case_id": "TC-placeholder",
                "scenario_id": "S-1",
                "requirement_ids": ["R-1"],
                "title": "Unrecognised input handling",
                "preconditions": "",
                "steps": [
                    {
                        "step_number": 1,
                        "action": "Speak an unrecognised phrase",
                        "expected_result": (
                            "Text appears in the quick-note composer AND the input is routed "
                            "to the workout logging service"
                        ),
                    }
                ],
                "owning_agent": "functional-tester",
                "execution_recommendation": "manual",
            }
        ],
    }
    group_review_payload = _group_review_payload(
        [
            {
                "severity": "blocker",
                "subject_id": "TC-1",
                "requirement_ids": ["R-1"],
                "description": "Step asserts two mutually exclusive outcomes at once.",
                "requirement_quote": "routes to quick note",
            }
        ]
    )

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content=json.dumps(contradictory_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(group_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert result.review.passed is False
    assert any(f.severity == FindingSeverity.BLOCKER for f in result.review.findings)


@pytest.mark.asyncio
async def test_fixture_b2_title_announced_contradiction_caught_by_fallback_call(
    mock_provider_factory, tmp_path: Path
) -> None:
    """Verbatim from the live 3B run: a case titled "Unrecognised Input
    Silently Dropped" asserting "the transcribed text does NOT appear in
    the quick-note composer", linked to a never-drop requirement. The
    per-group semantic call is mocked as missing it (exactly what happened
    live — the local model's own reviewer didn't catch it either); the
    dedicated suite-wide fallback call is what's actually responsible for
    catching this class, per the redesign, and must emit a blocker."""
    requirements = [
        _requirement(
            "R-1",
            title="Unrecognised input handling",
            description="Unrecognised input routes to quick note — never silently dropped.",
            source_span="Unrecognised input routes to quick note — never silently dropped.",
        )
    ]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    fill_payload = {
        "cases": [
            {
                "case_id": "TC-placeholder",
                "scenario_id": "S-1",
                "requirement_ids": ["R-1"],
                "title": "Unrecognised Input Silently Dropped",
                "preconditions": "",
                "steps": [
                    {
                        "step_number": 1,
                        "action": "Enter an unrecognised phrase into the voice input field",
                        "expected_result": "The transcribed text does not appear in the quick-note composer",
                    }
                ],
                "owning_agent": "functional-tester",
                "execution_recommendation": "manual",
            }
        ],
    }
    # Per-group semantic review misses it, same as the live run.
    group_review_payload = _group_review_payload([])
    fallback_review_payload = _group_review_payload(
        [
            {
                "severity": "blocker",
                "subject_id": "TC-1",
                "requirement_ids": ["R-1"],
                "description": (
                    "Case title and expected result both describe unrecognised input being "
                    "silently dropped — the requirement it's linked to explicitly forbids this."
                ),
                "requirement_quote": "never silently dropped",
            }
        ]
    )

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content=json.dumps(group_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(fallback_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert result.review.passed is False
    assert any(f.severity == FindingSeverity.BLOCKER for f in result.review.findings)


@pytest.mark.asyncio
async def test_fixture_c_invented_specifics_are_an_assumption_not_a_blocker(
    mock_provider_factory, tmp_path: Path
) -> None:
    """Replicates the NVIDIA TC-25/34 class: a case names an endpoint and
    error code nowhere in the requirement text. This should be flagged as
    an assumption (transparency gap), not fail the review outright."""
    requirements = [_requirement("R-1", title="Meal log calorie estimation")]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    invented_specifics_fill_payload = {
        "cases": [
            {
                "case_id": "TC-placeholder",
                "scenario_id": "S-1",
                "requirement_ids": ["R-1"],
                "title": "Meal log via API",
                "preconditions": "",
                "steps": [
                    {
                        "step_number": 1,
                        "action": "POST /api/v1/meal-logs with a food description",
                        "expected_result": "Returns HTTP 201 with error code MEAL_LOG_CREATED",
                    }
                ],
                "owning_agent": "functional-tester",
                "execution_recommendation": "manual",
            }
        ],
    }
    group_review_payload = _group_review_payload(
        [
            {
                "severity": "assumption",
                "subject_id": "TC-1",
                "requirement_ids": ["R-1"],
                "description": "Case names an endpoint path and error code not present in the requirement text.",
                "requirement_quote": "",
            }
        ]
    )

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content=json.dumps(invented_specifics_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(group_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert result.review.passed is True
    assert any(f.severity == FindingSeverity.ASSUMPTION for f in result.review.findings)
    assert not any(f.severity == FindingSeverity.BLOCKER for f in result.review.findings)


@pytest.mark.asyncio
async def test_fixture_d_fully_grounded_case_reviews_clean_with_zero_findings(
    mock_provider_factory, tmp_path: Path
) -> None:
    """Guards against over-flagging: a case that only asserts behavior the
    requirement text actually supports must review as passed with zero
    findings, not get spuriously flagged by the new semantic checks."""
    requirements = [_requirement("R-1", title="Voice input capture")]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    clean_fill_payload = _fill_payload("S-1", ["R-1"])
    group_review_payload = _group_review_payload([])

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(clean_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content=json.dumps(group_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert result.review.passed is True
    assert result.review.findings == []


def test_fixture_e_structural_review_catches_hallucinated_id_and_duplicate_tc_id() -> None:
    """A deliberately broken suite — one case references a requirement ID
    that doesn't exist in the registry, and two cases share the same
    case_id — is caught by `_run_structural_review` directly, with zero
    LLM calls involved. In real `run_pipeline` operation neither condition
    can actually occur (the fill-scope filter and sequential renumbering
    prevent both by construction), so this exercises the function as the
    safety net it's meant to be, not a scenario `run_pipeline` itself can
    reach."""
    requirements = [_requirement("R-1")]
    scenario = Scenario(
        scenario_id="S-1",
        title="A scenario",
        requirement_ids=["R-1"],
        owning_agent=OwningAgent.FUNCTIONAL_TESTER,
        risk_tier=RiskTier.HIGH,
        execution_recommendation=ExecutionRecommendation.MANUAL,
        recommendation_justification="Low run frequency, high setup cost.",
    )
    strategy = StrategyModel(strategy_id="strategy-1", scenarios=[scenario])
    broken_cases = [
        _case("TC-1", "S-1", ["R-999"]),  # hallucinated requirement ID
        _case("TC-2", "S-1", ["R-1"]),
        _case("TC-2", "S-1", ["R-1"]),  # duplicate case_id
    ]
    suite = SuiteModel(suite_id="suite-1", strategy_id="strategy-1", cases=broken_cases)
    traceability = build_traceability_matrix(requirements, broken_cases)

    findings = _run_structural_review(requirements, strategy, suite, traceability)

    blocker_descriptions = " ".join(f.description for f in findings if f.severity == FindingSeverity.BLOCKER)
    assert "R-999" in blocker_descriptions
    assert "TC-2" in blocker_descriptions and "duplicate" in blocker_descriptions.lower()


@pytest.mark.asyncio
async def test_semantic_review_skipped_when_structural_review_finds_a_blocker(
    monkeypatch: pytest.MonkeyPatch, mock_provider_factory, tmp_path: Path
) -> None:
    """Forces `_run_structural_review` to report a blocker (monkeypatched,
    since the real pipeline's own filters prevent this from ever happening
    naturally) and verifies no group or fallback review call is attempted
    afterward — the mock provider's response queue only has enough
    responses for strategy + fill, so a group/fallback call would raise
    "queue exhausted" and fail this test if the skip didn't actually
    happen."""
    import driftpin.agents.orchestrator as orchestrator_module

    def _fake_structural_review(requirements, strategy, suite, traceability):
        return [
            ReviewerFinding(severity=FindingSeverity.BLOCKER, subject_id="TC-1", description="Forced for test.")
        ]

    monkeypatch.setattr(orchestrator_module, "_run_structural_review", _fake_structural_review)

    requirements = [_requirement("R-1")]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    fill_payload = _fill_payload("S-1", ["R-1"])

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert result.review.passed is False
    assumptions = ledger.assumptions_path.read_text(encoding="utf-8")
    assert "Semantic review skipped" in assumptions


class _RaisesThenSucceedsProvider(LLMProvider):
    """Raises `PayloadTooHeavyError` on specific 1-indexed call numbers,
    returns queued responses otherwise — simulates a provider that decided
    a specific payload (e.g. the original, unsplit review group) is too
    heavy, without needing to replicate the provider-level HTTP retry
    mechanics that are already covered by the provider-layer tests."""

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
            raise PayloadTooHeavyError("simulated systematic gateway timeout")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_fixture_f_group_split_after_payload_too_heavy_then_succeeds(tmp_path: Path) -> None:
    """A group of 2 scenarios raises `PayloadTooHeavyError` on its first
    (unsplit) review call; the orchestrator must split it into two groups
    of 1 and retry each — both succeed, and the ledger records the split."""
    requirements = [_requirement("R-1"), _requirement("R-2")]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"]), _scenario_payload("S-2", ["R-2"])],
        "coverage_notes": "",
    }
    fill_payload_1 = _fill_payload("S-1", ["R-1"])
    fill_payload_2 = _fill_payload("S-2", ["R-2"])
    split_review_1 = _group_review_payload([])
    split_review_2 = _group_review_payload([])

    responses = [
        CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        CompletionResult(content=json.dumps(fill_payload_1), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        CompletionResult(content=json.dumps(fill_payload_2), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        CompletionResult(content=json.dumps(split_review_1), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
        CompletionResult(content=json.dumps(split_review_2), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
    ]
    # Call #4 is the first (unsplit, 2-scenario) group review attempt — raise there.
    provider = _RaisesThenSucceedsProvider(responses, raise_on_call_numbers={4})
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert result.review.passed is True

    agent_steps = [e for e in ledger.read_all() if e.entry_type == LedgerEntryType.AGENT_STEP]
    review_step = next(e for e in agent_steps if e.agent_name == "reviewer")
    assert review_step.metadata["groups_reviewed"] == 1
    assert review_step.metadata["group_calls"] == 2  # the two post-split calls, not the failed attempt

    assumptions = ledger.assumptions_path.read_text(encoding="utf-8")
    assert "split" in assumptions.lower()
    assert "payload-too-heavy" in assumptions.lower() or "payload too heavy" in assumptions.lower()


def test_fixture_g_fallback_rule_detected_from_acceptance_criteria_not_body() -> None:
    """Problem #1 from the AC/NFR fix prompt: a body-text-only scan misses a
    fallback rule stated only in an acceptance criterion, so the requirement
    never even reaches the fallback call. `_find_fallback_rule_requirements`
    must scan acceptance criteria with equal weight to the body/source-span
    text — this requirement's body has no marker word, only its AC does."""
    requirement = _requirement(
        "R-1",
        title="Schedule block matching",
        description="AI matches spoken activity to an active or recent schedule block.",
        source_span="AI matches spoken activity to an active or recent schedule block.",
        acceptance_criteria=[
            "If no matching block is found, the input must not be rejected — it saves as a note instead."
        ],
    )
    unrelated = _requirement("R-2", title="Unrelated requirement with no fallback rule anywhere")

    matches = _find_fallback_rule_requirements([requirement, unrelated])

    assert matches == [requirement]


@pytest.mark.asyncio
async def test_fixture_g_end_to_end_ac_only_rule_reaches_fallback_call_and_blocks(
    mock_provider_factory, tmp_path: Path
) -> None:
    """End-to-end version of Fixture G: a case asserting a rejection for a
    no-match input, against a rule stated only in an AC. The fallback call
    must actually run (not be skipped as 'no fallback rule found') and its
    rendered prompt must contain the AC text, so a live model could catch
    it — a body-text-only fallback check would skip this requirement
    entirely and never send it to the LLM at all."""
    requirements = [
        _requirement(
            "R-1",
            title="Schedule block matching",
            description="AI matches spoken activity to an active or recent schedule block.",
            source_span="AI matches spoken activity to an active or recent schedule block.",
            acceptance_criteria=[
                "If no matching block is found, the input must not be rejected — it saves as a note instead."
            ],
        )
    ]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    fill_payload = {
        "cases": [
            {
                "case_id": "TC-placeholder",
                "scenario_id": "S-1",
                "requirement_ids": ["R-1"],
                "title": "No-match input rejected",
                "preconditions": "",
                "steps": [
                    {
                        "step_number": 1,
                        "action": "Speak an activity with no matching schedule block",
                        "expected_result": "The system returns a 400 rejection and no note is saved",
                    }
                ],
                "owning_agent": "functional-tester",
                "execution_recommendation": "manual",
            }
        ],
    }
    group_review_payload = _group_review_payload([])
    fallback_review_payload = _group_review_payload(
        [
            {
                "severity": "blocker",
                "subject_id": "TC-1",
                "requirement_ids": ["R-1"],
                "description": "Case rejects a no-match input; the AC guarantees it saves as a note instead.",
                "requirement_quote": "it saves as a note instead",
            }
        ]
    )

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content=json.dumps(group_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(fallback_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert result.review.passed is False
    assert any(f.severity == FindingSeverity.BLOCKER for f in result.review.findings)
    # The fallback call's rendered prompt must actually contain the AC text —
    # proving a live model would have it in front of it, not just the body.
    fallback_prompt = provider.captured_systems[-1]
    assert "saves as a note instead" in fallback_prompt


@pytest.mark.asyncio
async def test_fixture_h_ambiguous_boundary_downgrades_to_assumption_not_blocker(
    mock_provider_factory, tmp_path: Path
) -> None:
    """Encodes the TC-18/TC-26 instability as a regression test: the same
    behavior class (empty input creates no entry) passed review on one live
    run and was blocked on another, because the fallback rule was written
    for 'unrecognised input' and the case concerned an adjacent, unnamed
    class ('empty input'). Per the adjudication rule, this must always
    downgrade to `assumption` with an ambiguity note — never a blocker —
    so the same input class can no longer flip the review's pass/fail
    verdict depending on which model happened to run it."""
    requirements = [
        _requirement(
            "R-1",
            title="Unrecognised input handling",
            description="Unrecognised input routes to quick note — never silently dropped.",
            source_span="Unrecognised input routes to quick note — never silently dropped.",
        )
    ]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    fill_payload = {
        "cases": [
            {
                "case_id": "TC-placeholder",
                "scenario_id": "S-1",
                "requirement_ids": ["R-1"],
                "title": "Empty input creates no entry",
                "preconditions": "",
                "steps": [
                    {
                        "step_number": 1,
                        "action": "Submit empty voice input",
                        "expected_result": "No note is created and no error is shown",
                    }
                ],
                "owning_agent": "functional-tester",
                "execution_recommendation": "manual",
            }
        ],
    }
    group_review_payload = _group_review_payload([])
    # Per the adjudication rule, the fallback call emits assumption, not
    # blocker, for this ambiguous-boundary case.
    fallback_review_payload = _group_review_payload(
        [
            {
                "severity": "assumption",
                "subject_id": "TC-1",
                "requirement_ids": ["R-1"],
                "description": (
                    "Req-1's never-drop rule is written for 'unrecognised input'; this case concerns "
                    "empty input, which the rule's text does not explicitly address — flagged as an "
                    "open boundary question, not a confirmed violation."
                ),
                "requirement_quote": "never silently dropped",
            }
        ]
    )

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(content=json.dumps(fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content=json.dumps(group_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(fallback_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    assert result.review.passed is True
    assert not any(f.severity == FindingSeverity.BLOCKER for f in result.review.findings)
    assert any(
        f.severity == FindingSeverity.ASSUMPTION and "boundary" in f.description.lower()
        for f in result.review.findings
    )


def test_fixture_i_nfr_grounds_timing_value_when_present_and_flags_when_absent() -> None:
    """Problem #2 from the AC/NFR fix prompt, asserted in both directions:
    a timing value stated only in a global NFR is resolved into the
    requirement's review view when the NFR exists in the registry, and is
    simply absent when it doesn't — the grounding check's prompt instructs
    the model to treat NFR-resolved text as grounded, but that only works
    if the NFR text actually reaches the prompt in the first place, which
    is what this test verifies at the Python layer."""
    requirement = _requirement("R-1", title="Voice transcription display")
    global_nfr = NonFunctionalRequirement(
        nfr_id="NFR-1",
        text="All voice-to-text responses complete within 3 seconds end-to-end.",
        scope=NfrScope.GLOBAL,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
    )

    with_nfr = _build_review_requirement(requirement, {"NFR-1": global_nfr})
    assert with_nfr["nfrs"] == ["All voice-to-text responses complete within 3 seconds end-to-end."]

    without_nfr = _build_review_requirement(requirement, {})
    assert without_nfr["nfrs"] == []


def test_resolve_requirement_nfrs_scoped_nfr_only_applies_to_linked_requirement() -> None:
    """A scoped NFR must not leak onto a requirement it wasn't linked to —
    only global NFRs are implicitly universal."""
    linked = _requirement("R-1", nfr_ids=["NFR-scoped"])
    unlinked = _requirement("R-2")
    scoped_nfr = NonFunctionalRequirement(
        nfr_id="NFR-scoped",
        text="R-1's endpoint specifically must respond within 500ms.",
        scope=NfrScope.SCOPED,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
    )
    nfrs_by_id = {"NFR-scoped": scoped_nfr}

    assert _resolve_requirement_nfrs(linked, nfrs_by_id) == [scoped_nfr.text]
    assert _resolve_requirement_nfrs(unlinked, nfrs_by_id) == []


@pytest.mark.asyncio
async def test_fixture_i_end_to_end_nfr_text_reaches_group_review_prompt(
    mock_provider_factory, tmp_path: Path
) -> None:
    """End-to-end companion to Fixture I: confirms the NFR text actually
    lands in the per-group semantic review's rendered prompt, not just in
    the Python-level resolver — a live model can only treat a timing value
    as grounded if the NFR text is genuinely in front of it."""
    requirements = [_requirement("R-1", title="Voice transcription display")]
    nfrs = [
        NonFunctionalRequirement(
            nfr_id="NFR-1",
            text="All voice-to-text responses complete within 3 seconds end-to-end.",
            scope=NfrScope.GLOBAL,
            source_doc_path="prd.md",
            source_doc_hash="hash-a",
        )
    ]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    clean_fill_payload = _fill_payload("S-1", ["R-1"])
    group_review_payload = _group_review_payload([])

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content=json.dumps(clean_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(group_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0, nfrs=nfrs
    )

    assert result.review.passed is True
    group_review_prompt = provider.captured_systems[-1]
    assert "within 3 seconds end-to-end" in group_review_prompt


@pytest.mark.asyncio
async def test_fixture_m_refill_covers_all_missing_requirements_in_one_attempt(
    mock_provider_factory, tmp_path: Path
) -> None:
    """Fixture M: the initial architect call enumerates scenarios for 4 of 6
    requirements. The requirement-to-scenario refill fires once, scoped to
    exactly the 2 missing requirements, and both get covered on the first
    attempt -- so the loop breaks before spending a second attempt."""
    requirements = [_requirement(f"R-{i}") for i in range(1, 7)]
    initial_strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload(f"S-{i}", [f"R-{i}"]) for i in range(1, 5)],
        "coverage_notes": "",
    }
    refill_strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [
            _scenario_payload("S-5", ["R-5"]),
            _scenario_payload("S-6", ["R-6"]),
        ],
        "coverage_notes": "",
    }

    provider = mock_provider_factory(
        [
            CompletionResult(
                content=json.dumps(initial_strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(refill_strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    strategy = await generate_strategy_only(provider, requirements, run_id="run1", ledger=ledger)

    assert {s.scenario_id for s in strategy.scenarios} == {"S-1", "S-2", "S-3", "S-4", "S-5", "S-6"}
    assert provider.call_count == 2

    agent_steps = [e for e in ledger.read_all() if e.entry_type == LedgerEntryType.AGENT_STEP]
    architect_step = next(e for e in agent_steps if e.agent_name == "test-architect")
    assert architect_step.metadata["requirements_total"] == 6
    assert architect_step.metadata["requirements_with_scenarios"] == 6
    assert architect_step.metadata["requirements_refilled"] == 2
    assert architect_step.metadata["requirements_failed"] == 0


@pytest.mark.asyncio
async def test_fixture_n_refill_exhausts_retries_and_reports_failure(
    mock_provider_factory, tmp_path: Path
) -> None:
    """Fixture N: R-2 never gets a scenario, not on the initial call nor
    either of the 2 refill attempts. After exhausting
    `_MAX_REQUIREMENT_REFILL_ATTEMPTS`, it must be reported as failed --
    both in the ledger's agent-step metadata and in ASSUMPTIONS.md -- never
    silently dropped."""
    requirements = [_requirement("R-1"), _requirement("R-2")]
    initial_strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    empty_refill_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [],
        "coverage_notes": "",
    }

    provider = mock_provider_factory(
        [
            CompletionResult(
                content=json.dumps(initial_strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(empty_refill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(empty_refill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    strategy = await generate_strategy_only(provider, requirements, run_id="run1", ledger=ledger)

    assert {s.scenario_id for s in strategy.scenarios} == {"S-1"}
    assert provider.call_count == 3

    agent_steps = [e for e in ledger.read_all() if e.entry_type == LedgerEntryType.AGENT_STEP]
    architect_step = next(e for e in agent_steps if e.agent_name == "test-architect")
    assert architect_step.metadata["requirements_refilled"] == 0
    assert architect_step.metadata["requirements_failed"] == 1

    assumptions = ledger.assumptions_path.read_text(encoding="utf-8")
    assert "R-2 has no test scenarios" in assumptions


@pytest.mark.asyncio
async def test_fixture_o_zero_coverage_alarm_forces_review_failure(
    mock_provider_factory, tmp_path: Path
) -> None:
    """Fixture O: R-2's scenario exhausts every fill retry and ends up with
    zero test cases -- a gap that survives the enumeration-level refill
    (Change 1) untouched, since R-2 DID get a scenario, the scenario just
    never got filled. The independent, Python-enforced zero-coverage alarm
    (Change 2) must catch this as the last step before rendering and force
    `passed=False` in code, even though no LLM reviewer call ever ran
    against R-2 to say so."""
    requirements = [_requirement("R-1"), _requirement("R-2")]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"]), _scenario_payload("S-2", ["R-2"])],
        "coverage_notes": "",
    }
    clean_fill_payload = _fill_payload("S-1", ["R-1"])
    empty_fill_payload = {"cases": []}
    group_review_payload = _group_review_payload([])

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content=json.dumps(clean_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(empty_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(empty_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(empty_fill_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(group_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    by_id = {row.requirement_id: row for row in result.traceability}
    assert by_id["R-2"].coverage_count == 0

    assert result.review.passed is False
    coverage_findings = [f for f in result.review.findings if "R-2" in f.requirement_ids]
    assert len(coverage_findings) == 1
    assert coverage_findings[0].severity == FindingSeverity.BLOCKER
    assert "zero test coverage in the final suite" in coverage_findings[0].description


@pytest.mark.asyncio
async def test_refill_scenario_id_collision_does_not_drop_original_scenarios_cases(
    mock_provider_factory, tmp_path: Path
) -> None:
    """Regression test for a real bug found live on NVIDIA: each refill
    round is its own isolated test-architect call that numbers its own
    scenarios starting from S-1, with no visibility into IDs the initial
    enumeration already used. Here the refill call reuses "S-1" -- the same
    ID as the initial enumeration's own scenario, which covers a DIFFERENT
    requirement. Before the fix, `_filter_cases_to_requirement_scope`'s
    `scenario_id`-keyed dict let the refill's "S-1" silently overwrite the
    original, so every case correctly filled for the original "S-1" failed
    as a "scope violation" against the wrong scenario -- live evidence
    zeroed out 3 real requirements' coverage this exact way. The fix
    renumbers every merged scenario ID before `generate_strategy_only`
    returns, so no downstream `scenario_id`-keyed lookup can ever collide."""
    requirements = [_requirement("R-1"), _requirement("R-2")]
    strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-1"])],
        "coverage_notes": "",
    }
    # The refill call's own local numbering restarts at "S-1" -- colliding
    # with the initial enumeration's real "S-1", which covers R-1, not R-2.
    refill_strategy_payload = {
        "strategy_id": "strategy-run1",
        "scenarios": [_scenario_payload("S-1", ["R-2"])],
        "coverage_notes": "",
    }
    fill_payload_r1 = _fill_payload("placeholder", ["R-1"])
    fill_payload_r2 = _fill_payload("placeholder", ["R-2"])
    group_review_payload = _group_review_payload([])

    provider = mock_provider_factory(
        [
            CompletionResult(content=json.dumps(strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"),
            CompletionResult(
                content=json.dumps(refill_strategy_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(fill_payload_r1), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(fill_payload_r2), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
            CompletionResult(
                content=json.dumps(group_review_payload), tokens_in=1, tokens_out=1, stop_reason="end_turn"
            ),
        ]
    )
    ledger = RunLedger(tmp_path, run_id="run1")

    result = await run_pipeline(
        provider, requirements, run_id="run1", ledger=ledger, fill_call_delay_seconds=0
    )

    scenario_ids = [s.scenario_id for s in result.strategy.scenarios]
    assert len(scenario_ids) == len(set(scenario_ids)), "scenario IDs must be unique after merging refill rounds"

    by_id = {row.requirement_id: row for row in result.traceability}
    assert by_id["R-1"].coverage_count == 1
    assert by_id["R-2"].coverage_count == 1

    assumptions_text = (
        ledger.assumptions_path.read_text(encoding="utf-8") if ledger.assumptions_path.exists() else ""
    )
    assert "scope violation" not in assumptions_text
