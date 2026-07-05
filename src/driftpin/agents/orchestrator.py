"""Deterministic orchestration: routes the test-architect -> functional-tester ->
reviewer pipeline and merges their output against the requirement registry.

Conflict resolution here is deterministic guard-rail logic over tagged
schemas — dropping a hallucinated reference, flagging a coverage gap — never
multi-model debate, judge models, or free-form agent negotiation. That
machinery is explicitly out of scope for this system.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from driftpin.agents.loader import load_agent_definition
from driftpin.agents.runtime import run_agent
from driftpin.ledger.ledger import RunLedger
from driftpin.providers.base import LLMProvider
from driftpin.schemas.requirements import Requirement
from driftpin.schemas.review import ReviewReport
from driftpin.schemas.strategy import Scenario, TestStrategy
from driftpin.schemas.test_cases import TestCase, TestSuite, TraceabilityRow

OnStage = Callable[[str], None]


class PipelineResult(BaseModel):
    strategy: TestStrategy
    suite: TestSuite
    review: ReviewReport
    traceability: list[TraceabilityRow]


def _filter_scenarios_to_known_requirements(
    strategy: TestStrategy, requirements: list[Requirement], ledger: RunLedger | None
) -> TestStrategy:
    """Drops any scenario referencing a requirement ID the registry doesn't
    know about, rather than trusting a hallucinated reference downstream."""
    known_ids = {r.requirement_id for r in requirements}
    kept: list[Scenario] = []

    for scenario in strategy.scenarios:
        unknown = [rid for rid in scenario.requirement_ids if rid not in known_ids]
        if unknown:
            if ledger is not None:
                ledger.record_assumption(
                    heading=f"Scenario {scenario.scenario_id} dropped: unknown requirement IDs",
                    detail=f"Referenced IDs not in the registry: {unknown}",
                )
            continue
        kept.append(scenario)

    return strategy.model_copy(update={"scenarios": kept})


def _filter_cases_to_known_scenarios(
    suite: TestSuite, strategy: TestStrategy, ledger: RunLedger | None
) -> TestSuite:
    """Drops any test case whose requirement_ids aren't a subset of its source
    scenario's, or whose scenario_id doesn't exist — the functional-tester
    does not get to expand scope beyond what the architect scoped."""
    scenarios_by_id = {s.scenario_id: s for s in strategy.scenarios}
    kept: list[TestCase] = []

    for case in suite.cases:
        scenario = scenarios_by_id.get(case.scenario_id)
        if scenario is None:
            if ledger is not None:
                ledger.record_assumption(
                    heading=f"Test case {case.case_id} dropped: unknown scenario",
                    detail=f"scenario_id '{case.scenario_id}' does not exist in this strategy.",
                )
            continue

        out_of_scope = [rid for rid in case.requirement_ids if rid not in scenario.requirement_ids]
        if out_of_scope:
            if ledger is not None:
                ledger.record_assumption(
                    heading=f"Test case {case.case_id} dropped: requirement scope violation",
                    detail=(
                        f"Requirement IDs {out_of_scope} are not part of scenario "
                        f"'{case.scenario_id}'s requirement_ids."
                    ),
                )
            continue

        kept.append(case)

    return suite.model_copy(update={"cases": kept})


def build_traceability_matrix(
    requirements: list[Requirement], cases: list[TestCase]
) -> list[TraceabilityRow]:
    """Deterministic aggregation: which cases cover which requirement, and how many."""
    rows: list[TraceabilityRow] = []
    for requirement in requirements:
        covering = [c.case_id for c in cases if requirement.requirement_id in c.requirement_ids]
        rows.append(
            TraceabilityRow(
                requirement_id=requirement.requirement_id,
                requirement_title=requirement.title,
                risk_tier=requirement.risk_tier.value,
                case_ids=covering,
                coverage_count=len(covering),
            )
        )
    return rows


async def generate_strategy_only(
    provider: LLMProvider,
    requirements: list[Requirement],
    run_id: str,
    ledger: RunLedger | None = None,
    on_stage: OnStage | None = None,
) -> TestStrategy:
    if not requirements:
        raise ValueError("Cannot run the generation pipeline against an empty requirement set.")

    if on_stage is not None:
        on_stage("test-architect")

    strategy_id = f"strategy-{run_id}"
    architect_def = load_agent_definition("test-architect")
    raw_strategy = await run_agent(
        architect_def,
        provider,
        context={"requirements": requirements, "strategy_id": strategy_id},
        ledger=ledger,
    )
    assert isinstance(raw_strategy, TestStrategy)
    return _filter_scenarios_to_known_requirements(raw_strategy, requirements, ledger)


async def run_pipeline(
    provider: LLMProvider,
    requirements: list[Requirement],
    run_id: str,
    ledger: RunLedger | None = None,
    on_stage: OnStage | None = None,
) -> PipelineResult:
    strategy_id = f"strategy-{run_id}"
    suite_id = f"suite-{run_id}"
    review_id = f"review-{run_id}"

    strategy = await generate_strategy_only(provider, requirements, run_id, ledger, on_stage)

    if on_stage is not None:
        on_stage("functional-tester")

    tester_def = load_agent_definition("functional-tester")
    referenced_ids = {rid for s in strategy.scenarios for rid in s.requirement_ids}
    raw_suite = await run_agent(
        tester_def,
        provider,
        context={
            "requirements": [r for r in requirements if r.requirement_id in referenced_ids],
            "scenarios": strategy.scenarios,
            "suite_id": suite_id,
            "strategy_id": strategy_id,
        },
        ledger=ledger,
    )
    assert isinstance(raw_suite, TestSuite)
    suite = _filter_cases_to_known_scenarios(raw_suite, strategy, ledger)

    if on_stage is not None:
        on_stage("reviewer")

    reviewer_def = load_agent_definition("reviewer")
    raw_review = await run_agent(
        reviewer_def,
        provider,
        context={
            "requirements": requirements,
            "scenarios": strategy.scenarios,
            "cases": suite.cases,
            "review_id": review_id,
            "target_run_id": run_id,
        },
        ledger=ledger,
    )
    assert isinstance(raw_review, ReviewReport)

    traceability = build_traceability_matrix(requirements, suite.cases)

    return PipelineResult(strategy=strategy, suite=suite, review=raw_review, traceability=traceability)
