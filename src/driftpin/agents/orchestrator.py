"""Deterministic orchestration: routes the test-architect -> functional-tester ->
reviewer pipeline and merges their output against the requirement registry.

Conflict resolution here is deterministic guard-rail logic over tagged
schemas — dropping a hallucinated reference, flagging a coverage gap — never
multi-model debate, judge models, or free-form agent negotiation. That
machinery is explicitly out of scope for this system.

functional-tester runs as enumerate-then-fill, not single-call generation of
the full suite: live testing found single-call generation reliably stalls
around 67% scenario coverage regardless of token budget or prompt wording
(see DESIGN_DECISIONS.md and EVALS.md for the evidence). Stage 1's
"enumeration" is simply test-architect's own scenario list — it already has
every field a checklist needs, so re-deriving it with a second call would be
pure redundancy. Stage 2 fills each scenario with its own dedicated call, so
no single call ever has to pace itself through a long array; completeness is
then enforced by Python diffing filled scenarios against that checklist, not
assumed from model stamina.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from pydantic import BaseModel

from driftpin.agents.loader import AgentDefinition, load_agent_definition
from driftpin.agents.runtime import run_agent
from driftpin.ledger.ledger import RunLedger
from driftpin.providers.base import LLMProvider, PayloadTooHeavyError
from driftpin.schemas.requirements import NfrScope, NonFunctionalRequirement, Requirement
from driftpin.schemas.review import (
    FindingSeverity,
    GroupReviewResult,
    ReviewerFinding,
    ReviewReport,
)
from driftpin.schemas.strategy import Scenario, TestStrategy
from driftpin.schemas.test_cases import FillResult, TestCase, TestSuite, TraceabilityRow

OnStage = Callable[[str], None]

MAX_SCENARIOS_WITHOUT_CONFIRMATION = 100
DEFAULT_FILL_CALL_DELAY_SECONDS = 2.0
_MAX_SCENARIO_REFILL_ATTEMPTS = 2
_MAX_REQUIREMENT_REFILL_ATTEMPTS = 2
_REVIEW_GROUP_SIZE = 3
_FALLBACK_RULE_MARKERS = ("never", "must not", "should not", "cannot", "must never")


class TooManyScenariosError(Exception):
    """Raised when a strategy produces more scenarios than the fill stage's
    scale guard allows without explicit confirmation — enumerate-then-fill
    means one sequential call per scenario, and hundreds of scenarios means
    hundreds of sequential calls a human should knowingly sign up for."""

    def __init__(self, scenario_count: int) -> None:
        self.scenario_count = scenario_count
        super().__init__(
            f"This PRD produced {scenario_count} scenarios, above the "
            f"{MAX_SCENARIOS_WITHOUT_CONFIRMATION}-scenario guard — this PRD may be too large "
            "for a single run; consider splitting it by module, or explicitly confirm to proceed."
        )


class PipelineResult(BaseModel):
    strategy: TestStrategy
    suite: TestSuite
    review: ReviewReport
    traceability: list[TraceabilityRow]
    failed_scenario_ids: list[str] = []


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


def _renumber_scenario_ids(scenarios: list[Scenario]) -> list[Scenario]:
    """Python owns final scenario IDs, the same reason `_renumber_case_ids`
    owns final case IDs: each requirement-completeness refill round is its
    own isolated test-architect call, and the model numbers that round's
    scenarios starting from S-1 in isolation — with no visibility into the
    initial enumeration's own S-1..S-N. Left unrenumbered, a refill round's
    S-1 collides with the initial enumeration's real S-1 in
    `_filter_cases_to_requirement_scope`'s `scenario_id`-keyed dict, silently
    replacing it — which then fails EVERY case correctly filled for the
    original S-1 as a "scope violation" against the wrong (colliding)
    scenario, and does so invisibly until the zero-coverage alarm below
    catches the resulting empty requirement. Measured live: exactly this
    collision zeroed out 3 requirements' coverage on a real NVIDIA run."""
    return [scenario.model_copy(update={"scenario_id": f"S-{i}"}) for i, scenario in enumerate(scenarios, start=1)]


def _requirement_ids_with_scenarios(scenarios: list[Scenario]) -> set[str]:
    covered: set[str] = set()
    for scenario in scenarios:
        covered.update(scenario.requirement_ids)
    return covered


async def _refill_missing_requirement_scenarios(
    provider: LLMProvider,
    architect_def: AgentDefinition,
    strategy_id: str,
    all_requirements: list[Requirement],
    scenarios: list[Scenario],
    ledger: RunLedger | None,
) -> tuple[list[Scenario], list[str]]:
    """Enumerate-then-fill's completeness guarantee only ever checked that
    every ENUMERATED scenario gets filled with cases — it says nothing
    about whether every requirement got a scenario in the first place.
    test-architect is still a single call producing the whole enumeration
    at once, the same "one call, whole list, model stamina" shape that
    fill generation and review both already needed splitting to fix.
    Measured live: 3 of 9 requirements (a ~33% miss rate) got zero
    scenarios in one PocketBudget run, with nothing downstream noticing —
    `scenarios_failed` stayed 0 because every scenario that WAS enumerated
    filled successfully; the gap was upstream of that check entirely.

    This applies the identical fix at the requirement level: diff the
    enumeration against the full registry, and for any requirement with
    zero scenarios, ask test-architect again — scoped to only the missing
    requirements' body + ACs + NFRs, never the whole registry — up to
    `_MAX_REQUIREMENT_REFILL_ATTEMPTS` rounds. Returns (all scenarios
    including any successfully refilled, requirement_ids still uncovered
    after exhausting retries)."""
    requirements_by_id = {r.requirement_id: r for r in all_requirements}
    all_scenarios = list(scenarios)

    for attempt in range(_MAX_REQUIREMENT_REFILL_ATTEMPTS):
        covered = _requirement_ids_with_scenarios(all_scenarios)
        missing_ids = [r.requirement_id for r in all_requirements if r.requirement_id not in covered]
        if not missing_ids:
            break

        missing_requirements = [requirements_by_id[rid] for rid in missing_ids]
        raw = await run_agent(
            architect_def,
            provider,
            context={"requirements": missing_requirements, "strategy_id": strategy_id},
            ledger=ledger,
        )
        assert isinstance(raw, TestStrategy)
        filtered = _filter_scenarios_to_known_requirements(raw, missing_requirements, ledger)
        all_scenarios.extend(filtered.scenarios)

        if ledger is not None:
            newly_covered = _requirement_ids_with_scenarios(filtered.scenarios) & set(missing_ids)
            ledger.record_assumption(
                heading=f"Requirement-to-scenario refill attempt {attempt + 1}",
                detail=(
                    f"Requested scenarios for {len(missing_ids)} uncovered requirement(s): "
                    f"{', '.join(missing_ids)}. {len(newly_covered)} now covered."
                ),
            )

    covered = _requirement_ids_with_scenarios(all_scenarios)
    still_missing = [r.requirement_id for r in all_requirements if r.requirement_id not in covered]
    return all_scenarios, still_missing


def _filter_cases_to_requirement_scope(
    suite: TestSuite, strategy: TestStrategy, ledger: RunLedger | None
) -> TestSuite:
    """Drops any test case whose requirement_ids aren't a subset of its
    source scenario's — the functional-tester does not get to expand scope
    beyond what the architect scoped. `scenario_id` itself isn't re-checked
    here: every case's scenario_id is set deterministically by
    `_fill_scenario` below, so it's guaranteed to match a real scenario by
    construction, not by trusting the model to echo it back correctly."""
    scenarios_by_id = {s.scenario_id: s for s in strategy.scenarios}
    kept: list[TestCase] = []

    for case in suite.cases:
        scenario = scenarios_by_id[case.scenario_id]
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


def _renumber_case_ids(cases: list[TestCase]) -> list[TestCase]:
    """Python owns final case IDs. Every fill call numbers its own cases
    starting from TC-1 in isolation, so raw IDs collide across scenarios and
    must be reassigned sequentially at merge — the same reason requirement
    IDs are never trusted to the extracting LLM."""
    return [case.model_copy(update={"case_id": f"TC-{i}"}) for i, case in enumerate(cases, start=1)]


def _log_case_assumptions(cases: list[TestCase], ledger: RunLedger | None) -> None:
    """A case's `assumptions` field only reaches a human if it lands in
    ASSUMPTIONS.md, the same place every other flagged uncertainty in this
    system goes — not just as a column a reader might not scroll to."""
    if ledger is None:
        return
    for case in cases:
        for assumption in case.assumptions:
            ledger.record_assumption(
                heading=f"Test case {case.case_id} ({case.scenario_id}) carries an assumption",
                detail=assumption,
            )


def _run_structural_review(
    requirements: list[Requirement],
    strategy: TestStrategy,
    suite: TestSuite,
    traceability: list[TraceabilityRow],
) -> list[ReviewerFinding]:
    """Zero-LLM-call checks: hallucinated requirement IDs, unknown scenario
    IDs, duplicate case IDs, owning_agent consistency, and traceability
    coverage-count accounting. These are all facts Python can check exactly
    — spending tokens asking a model to verify them would be paying for
    work code already does for free. A blocker finding here means the suite
    is structurally broken and semantic review never runs: there's no point
    auditing content quality on a suite with a hallucinated ID or a
    duplicate case ID."""
    findings: list[ReviewerFinding] = []
    known_requirement_ids = {r.requirement_id for r in requirements}
    scenarios_by_id = {s.scenario_id: s for s in strategy.scenarios}

    for case in suite.cases:
        unknown_ids = [rid for rid in case.requirement_ids if rid not in known_requirement_ids]
        if unknown_ids:
            findings.append(
                ReviewerFinding(
                    severity=FindingSeverity.BLOCKER,
                    subject_id=case.case_id,
                    description=f"References requirement ID(s) not in the registry: {unknown_ids}",
                    requirement_ids=unknown_ids,
                )
            )
        if case.scenario_id not in scenarios_by_id:
            findings.append(
                ReviewerFinding(
                    severity=FindingSeverity.BLOCKER,
                    subject_id=case.case_id,
                    description=f"References scenario ID '{case.scenario_id}' that doesn't exist in the strategy.",
                )
            )

    seen_case_ids: dict[str, int] = {}
    for case in suite.cases:
        seen_case_ids[case.case_id] = seen_case_ids.get(case.case_id, 0) + 1
    for case_id, count in seen_case_ids.items():
        if count > 1:
            findings.append(
                ReviewerFinding(
                    severity=FindingSeverity.BLOCKER,
                    subject_id=case_id,
                    description=f"Case ID '{case_id}' appears {count} times — duplicate TC-IDs.",
                )
            )

    for case in suite.cases:
        scenario = scenarios_by_id.get(case.scenario_id)
        if scenario is not None and case.owning_agent != scenario.owning_agent:
            findings.append(
                ReviewerFinding(
                    severity=FindingSeverity.MINOR,
                    subject_id=case.case_id,
                    description=(
                        f"owning_agent '{case.owning_agent.value}' does not match its scenario "
                        f"'{scenario.scenario_id}'s owning_agent '{scenario.owning_agent.value}'."
                    ),
                )
            )

    recomputed_by_id = {row.requirement_id: row for row in build_traceability_matrix(requirements, suite.cases)}
    for row in traceability:
        recomputed = recomputed_by_id.get(row.requirement_id)
        if recomputed is not None and recomputed.coverage_count != row.coverage_count:
            findings.append(
                ReviewerFinding(
                    severity=FindingSeverity.MAJOR,
                    subject_id=row.requirement_id,
                    description=(
                        f"Traceability matrix reports coverage_count={row.coverage_count} for "
                        f"{row.requirement_id}, but recomputing from the final case list gives "
                        f"{recomputed.coverage_count}."
                    ),
                    requirement_ids=[row.requirement_id],
                )
            )

    return findings


def _group_scenarios(scenarios: list[Scenario], group_size: int) -> list[list[Scenario]]:
    return [scenarios[i : i + group_size] for i in range(0, len(scenarios), group_size)]


def _resolve_requirement_nfrs(
    requirement: Requirement, nfrs_by_id: dict[str, NonFunctionalRequirement]
) -> list[str]:
    """Global-scope NFRs apply to every requirement implicitly; scoped NFRs
    apply only where the requirement's own `nfr_ids` link to them. Resolved
    here, at review-input-build time, rather than duplicated into registry
    storage — a single global NFR edit shouldn't require rewriting every
    requirement in the registry file."""
    texts = [nfr.text for nfr in nfrs_by_id.values() if nfr.scope == NfrScope.GLOBAL]
    texts += [nfrs_by_id[nid].text for nid in requirement.nfr_ids if nid in nfrs_by_id]
    return texts


def _build_review_requirement(
    requirement: Requirement, nfrs_by_id: dict[str, NonFunctionalRequirement]
) -> dict[str, object]:
    """Flattens a Requirement into the view the reviewer prompts render:
    body text + acceptance criteria + applicable NFRs. 'Grounded' means
    grounded in any of the three, not the body text alone — a value stated
    only in an AC or only in an NFR is exactly as real as one stated in the
    requirement's own description."""
    return {
        "requirement_id": requirement.requirement_id,
        "risk_tier": requirement.risk_tier,
        "title": requirement.title,
        "description": requirement.description,
        "source_span": requirement.source_span,
        "acceptance_criteria": [ac.text for ac in requirement.acceptance_criteria],
        "nfrs": _resolve_requirement_nfrs(requirement, nfrs_by_id),
    }


async def _review_group_with_splitting(
    provider: LLMProvider,
    reviewer_def: AgentDefinition,
    group: list[Scenario],
    cases_by_scenario_id: dict[str, list[TestCase]],
    requirements_by_id: dict[str, Requirement],
    nfrs_by_id: dict[str, NonFunctionalRequirement],
    ledger: RunLedger | None,
) -> tuple[list[ReviewerFinding], int]:
    """Returns (findings, calls_made). A group call receives only that
    group's cases and the deduped full text (body + acceptance criteria +
    applicable NFRs) of only the requirements those cases link to — never
    the whole suite or registry. On `PayloadTooHeavyError` the group is
    split in half and each half retried; a group already down to one
    scenario that still fails is logged and skipped (an `info` finding, not
    a crashed run) rather than split further, since there's nothing smaller
    left to try."""
    group_cases = [c for s in group for c in cases_by_scenario_id.get(s.scenario_id, [])]
    if not group_cases:
        return [], 0

    group_requirements: dict[str, Requirement] = {}
    for scenario in group:
        for rid in scenario.requirement_ids:
            if rid in requirements_by_id and rid not in group_requirements:
                group_requirements[rid] = requirements_by_id[rid]

    try:
        raw = await run_agent(
            reviewer_def,
            provider,
            context={
                "cases": group_cases,
                "requirements": [
                    _build_review_requirement(r, nfrs_by_id) for r in group_requirements.values()
                ],
            },
            ledger=ledger,
        )
        assert isinstance(raw, GroupReviewResult)
        return raw.findings, 1
    except PayloadTooHeavyError as exc:
        if len(group) == 1:
            if ledger is not None:
                ledger.record_assumption(
                    heading=f"Semantic review failed for scenario {group[0].scenario_id}",
                    detail=(
                        f"{exc} — payload too heavy even at the smallest possible group size. "
                        "This scenario's cases were not semantically reviewed for contradiction "
                        "or grounding issues; human attention required."
                    ),
                )
            return [
                ReviewerFinding(
                    severity=FindingSeverity.INFO,
                    subject_id=group[0].scenario_id,
                    description="Semantic review could not complete for this scenario (payload too heavy).",
                )
            ], 1

        midpoint = len(group) // 2
        left_findings, left_calls = await _review_group_with_splitting(
            provider, reviewer_def, group[:midpoint], cases_by_scenario_id, requirements_by_id, nfrs_by_id, ledger
        )
        right_findings, right_calls = await _review_group_with_splitting(
            provider, reviewer_def, group[midpoint:], cases_by_scenario_id, requirements_by_id, nfrs_by_id, ledger
        )
        if ledger is not None:
            ledger.record_assumption(
                heading=f"Review group split after payload-too-heavy ({len(group)} scenarios)",
                detail=f"{exc} — retried as two smaller groups instead of the original size.",
            )
        return left_findings + right_findings, left_calls + right_calls


async def _run_semantic_group_review(
    provider: LLMProvider,
    reviewer_def: AgentDefinition,
    scenarios: list[Scenario],
    cases: list[TestCase],
    requirements: list[Requirement],
    nfrs: list[NonFunctionalRequirement],
    ledger: RunLedger | None,
) -> tuple[list[ReviewerFinding], int, int]:
    """Returns (findings, groups_reviewed, group_calls_made)."""
    requirements_by_id = {r.requirement_id: r for r in requirements}
    nfrs_by_id = {n.nfr_id: n for n in nfrs}
    cases_by_scenario_id: dict[str, list[TestCase]] = {}
    for case in cases:
        cases_by_scenario_id.setdefault(case.scenario_id, []).append(case)

    groups = _group_scenarios(scenarios, _REVIEW_GROUP_SIZE)
    all_findings: list[ReviewerFinding] = []
    total_calls = 0
    for group in groups:
        findings, calls = await _review_group_with_splitting(
            provider, reviewer_def, group, cases_by_scenario_id, requirements_by_id, nfrs_by_id, ledger
        )
        all_findings.extend(findings)
        total_calls += calls

    return all_findings, len(groups), total_calls


def _find_fallback_rule_requirements(requirements: list[Requirement]) -> list[Requirement]:
    """Heuristic scan for requirements that define a fallback/never-drop
    rule — the only ones the suite-wide fallback call needs, keeping that
    call's requirement input small by construction. A requirement whose
    text names no prohibition has nothing for a rejection case elsewhere in
    the suite to contradict. Scans acceptance criteria with equal weight to
    the body/source-span text — a rule stated only in an AC (never visible
    to a body-only scan) is exactly as real as one stated in the body."""
    matches = []
    for requirement in requirements:
        haystack = " ".join(
            [requirement.description, requirement.source_span]
            + [ac.text for ac in requirement.acceptance_criteria]
        ).lower()
        if any(marker in haystack for marker in _FALLBACK_RULE_MARKERS):
            matches.append(requirement)
    return matches


def _compact_case_summary(case: TestCase) -> dict[str, str]:
    outcome = "; ".join(step.expected_result for step in case.steps)
    return {"case_id": case.case_id, "title": case.title, "expected_outcome": outcome}


async def _run_fallback_review(
    provider: LLMProvider,
    fallback_def: AgentDefinition,
    requirements: list[Requirement],
    cases: list[TestCase],
    ledger: RunLedger | None,
) -> tuple[list[ReviewerFinding], int]:
    """Returns (findings, calls_made). Skipped entirely (0 calls) when no
    requirement in the registry defines a fallback/never-drop rule — there's
    nothing this check could ever find. Unlike group review, a
    `PayloadTooHeavyError` here isn't split further: the input is already
    the minimal filtered requirement set plus one-line case summaries, so a
    failure is logged and surfaced rather than retried smaller."""
    fallback_requirements = _find_fallback_rule_requirements(requirements)
    if not fallback_requirements:
        return [], 0

    compact_cases = [_compact_case_summary(c) for c in cases]
    review_requirements = [_build_review_requirement(r, {}) for r in fallback_requirements]
    try:
        raw = await run_agent(
            fallback_def,
            provider,
            context={"requirements": review_requirements, "cases": compact_cases},
            ledger=ledger,
        )
        assert isinstance(raw, GroupReviewResult)
        return raw.findings, 1
    except PayloadTooHeavyError as exc:
        if ledger is not None:
            ledger.record_assumption(
                heading="Fallback-rule review failed (payload too heavy)",
                detail=(
                    f"{exc} — the suite was not checked for fallback-rule violations; "
                    "human attention required."
                ),
            )
        return [
            ReviewerFinding(
                severity=FindingSeverity.INFO,
                subject_id="fallback-review",
                description="Suite-wide fallback-rule review could not complete (payload too heavy).",
            )
        ], 1


_ZERO_COVERAGE_MARKER = "has zero test coverage in the final suite."


def _check_zero_coverage_requirements(traceability: list[TraceabilityRow]) -> list[ReviewerFinding]:
    """The final safety net, independent of the requirement-to-scenario
    refill in `generate_strategy_only`: that refill closes the gap at
    enumeration time, but this check catches ANY requirement that still
    has zero test cases in the FINAL suite, for any reason — the refill
    itself exhausting retries, a scenario's fill call failing all retries,
    cases getting filtered out downstream, or any other currently-unknown
    failure mode. Runs unconditionally, in Python, on the already-computed
    traceability matrix — never delegated to the LLM reviewer's own
    judgment, and never skipped even when a structural blocker skips
    semantic review, since it costs zero LLM calls either way."""
    return [
        ReviewerFinding(
            severity=FindingSeverity.BLOCKER,
            subject_id=row.requirement_id,
            description=f"{row.requirement_id} {_ZERO_COVERAGE_MARKER}",
            requirement_ids=[row.requirement_id],
        )
        for row in traceability
        if row.coverage_count == 0
    ]


def _compose_review_report(
    review_id: str,
    target_run_id: str,
    structural_findings: list[ReviewerFinding],
    semantic_findings: list[ReviewerFinding],
    fallback_findings: list[ReviewerFinding],
    coverage_findings: list[ReviewerFinding] | None = None,
) -> ReviewReport:
    """The final review is Python-assembled from four sources, never
    returned directly by a single LLM call — `passed` is computed here from
    the merged findings, not trusted to any model's own bookkeeping. A
    zero-coverage finding is blocker-severity like any other, so it forces
    `passed=False` through the same mechanism as every other blocker —
    no special-cased override needed."""
    coverage_findings = coverage_findings or []
    all_findings = structural_findings + semantic_findings + fallback_findings + coverage_findings
    passed = not any(f.severity == FindingSeverity.BLOCKER for f in all_findings)
    summary = (
        f"{len(structural_findings)} structural finding(s), {len(semantic_findings)} semantic "
        f"finding(s), {len(fallback_findings)} fallback-rule finding(s), "
        f"{len(coverage_findings)} coverage finding(s). "
        f"{'PASSED' if passed else 'FAILED'} — "
        f"{'no' if passed else 'at least one'} blocker-severity finding."
    )
    return ReviewReport(
        review_id=review_id,
        target_run_id=target_run_id,
        findings=all_findings,
        passed=passed,
        summary=summary,
    )


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
    filtered_strategy = _filter_scenarios_to_known_requirements(raw_strategy, requirements, ledger)

    if on_stage is not None:
        on_stage("test-architect (requirement coverage check)")
    initially_missing = [
        r.requirement_id
        for r in requirements
        if r.requirement_id not in _requirement_ids_with_scenarios(filtered_strategy.scenarios)
    ]
    all_scenarios, still_missing = await _refill_missing_requirement_scenarios(
        provider, architect_def, strategy_id, requirements, filtered_strategy.scenarios, ledger
    )

    if ledger is not None:
        ledger.record_agent_step(
            agent_name="test-architect",
            metadata={
                "requirements_total": len(requirements),
                "requirements_with_scenarios": len(requirements) - len(still_missing),
                "requirements_refilled": len(initially_missing) - len(still_missing),
                "requirements_failed": len(still_missing),
            },
        )
        for requirement_id in still_missing:
            ledger.record_assumption(
                heading=f"{requirement_id} has no test scenarios",
                detail=f"{requirement_id} has no test scenarios — human attention required.",
            )

    return filtered_strategy.model_copy(update={"scenarios": _renumber_scenario_ids(all_scenarios)})


async def _fill_scenario(
    provider: LLMProvider,
    tester_def: AgentDefinition,
    scenario: Scenario,
    requirements_by_id: dict[str, Requirement],
    ledger: RunLedger | None,
) -> list[TestCase]:
    """One call, one scenario. Every case that comes back has its
    scenario_id/owning_agent/execution_recommendation forced to the
    scenario's own values — these are never trusted to the model, since
    Python already knows them with certainty."""
    linked_requirements = [
        requirements_by_id[rid] for rid in scenario.requirement_ids if rid in requirements_by_id
    ]
    raw = await run_agent(
        tester_def,
        provider,
        context={"scenario": scenario, "requirements": linked_requirements},
        ledger=ledger,
    )
    assert isinstance(raw, FillResult)
    return [
        case.model_copy(
            update={
                "scenario_id": scenario.scenario_id,
                "owning_agent": scenario.owning_agent,
                "execution_recommendation": scenario.execution_recommendation,
            }
        )
        for case in raw.cases
    ]


async def _fill_all_scenarios(
    provider: LLMProvider,
    tester_def: AgentDefinition,
    scenarios: list[Scenario],
    requirements: list[Requirement],
    ledger: RunLedger | None,
    on_stage: OnStage | None,
    fill_call_delay_seconds: float,
) -> tuple[list[TestCase], list[str]]:
    """Fills every scenario with its own dedicated call — the actual
    coverage guarantee enumerate-then-fill exists for. A scenario that comes
    back empty is retried up to `_MAX_SCENARIO_REFILL_ATTEMPTS` times; still
    empty after that, it's recorded as a generation failure (surfaced in
    `PipelineResult.failed_scenario_ids` and `ASSUMPTIONS.md`) rather than
    silently missing with no trace."""
    requirements_by_id = {r.requirement_id: r for r in requirements}
    all_cases: list[TestCase] = []
    failed_scenario_ids: list[str] = []
    refilled_count = 0
    is_first_call = True

    for index, scenario in enumerate(scenarios):
        if on_stage is not None:
            on_stage(f"functional-tester ({index + 1}/{len(scenarios)}: {scenario.scenario_id})")

        cases: list[TestCase] = []
        for attempt in range(_MAX_SCENARIO_REFILL_ATTEMPTS + 1):
            if not is_first_call:
                await asyncio.sleep(fill_call_delay_seconds)
            is_first_call = False

            cases = await _fill_scenario(provider, tester_def, scenario, requirements_by_id, ledger)
            if cases:
                if attempt > 0:
                    refilled_count += 1
                break

        if cases:
            all_cases.extend(cases)
        else:
            failed_scenario_ids.append(scenario.scenario_id)
            if ledger is not None:
                ledger.record_assumption(
                    heading=f"Scenario {scenario.scenario_id} GENERATION_FAILED",
                    detail=(
                        f"No test cases could be generated for '{scenario.title}' after "
                        f"{_MAX_SCENARIO_REFILL_ATTEMPTS + 1} attempts — human attention required."
                    ),
                )

    if ledger is not None:
        ledger.record_agent_step(
            agent_name="functional-tester",
            metadata={
                "scenarios_enumerated": len(scenarios),
                "scenarios_filled": len(scenarios) - len(failed_scenario_ids),
                "scenarios_refilled": refilled_count,
                "scenarios_failed": len(failed_scenario_ids),
            },
        )

    return all_cases, failed_scenario_ids


async def run_pipeline(
    provider: LLMProvider,
    requirements: list[Requirement],
    run_id: str,
    ledger: RunLedger | None = None,
    on_stage: OnStage | None = None,
    fill_call_delay_seconds: float = DEFAULT_FILL_CALL_DELAY_SECONDS,
    allow_large_scenario_count: bool = False,
    on_scenario_count_check: Callable[[int], bool] | None = None,
    nfrs: list[NonFunctionalRequirement] | None = None,
) -> PipelineResult:
    """`on_scenario_count_check`, if given, is called with the scenario count
    once the strategy is ready and only if it exceeds the scale guard — a
    synchronous confirmation hook (e.g. a CLI `Confirm.ask`) that lets a
    caller decide inline rather than losing the already-generated strategy
    to a raised exception and having to re-run test-architect on retry.
    `nfrs` is the registry's non-functional-requirement list, fed to the
    semantic review stage so grounding checks recognize values grounded in
    an NFR, not just a requirement's own body text."""
    nfrs = nfrs or []
    strategy_id = f"strategy-{run_id}"
    suite_id = f"suite-{run_id}"
    review_id = f"review-{run_id}"

    strategy = await generate_strategy_only(provider, requirements, run_id, ledger, on_stage)

    scenario_count = len(strategy.scenarios)
    over_guard = scenario_count > MAX_SCENARIOS_WITHOUT_CONFIRMATION and not allow_large_scenario_count
    if over_guard and (on_scenario_count_check is None or not on_scenario_count_check(scenario_count)):
        raise TooManyScenariosError(scenario_count)

    tester_def = load_agent_definition("functional-tester")
    raw_cases, failed_scenario_ids = await _fill_all_scenarios(
        provider, tester_def, strategy.scenarios, requirements, ledger, on_stage, fill_call_delay_seconds
    )
    placeholder_suite = TestSuite(suite_id=suite_id, strategy_id=strategy_id, cases=raw_cases)
    filtered_suite = _filter_cases_to_requirement_scope(placeholder_suite, strategy, ledger)
    suite = filtered_suite.model_copy(update={"cases": _renumber_case_ids(filtered_suite.cases)})
    _log_case_assumptions(suite.cases, ledger)

    traceability = build_traceability_matrix(requirements, suite.cases)

    if on_stage is not None:
        on_stage("reviewer (structural)")
    structural_findings = _run_structural_review(requirements, strategy, suite, traceability)
    has_structural_blocker = any(f.severity == FindingSeverity.BLOCKER for f in structural_findings)

    semantic_findings: list[ReviewerFinding] = []
    fallback_findings: list[ReviewerFinding] = []
    groups_reviewed = 0
    group_calls = 0
    fallback_calls = 0

    if has_structural_blocker:
        if ledger is not None:
            ledger.record_assumption(
                heading="Semantic review skipped: structural review found a blocker",
                detail=(
                    "The suite is structurally broken (see structural findings) — no tokens "
                    "spent reviewing content quality on a suite that's already known-broken."
                ),
            )
    else:
        if on_stage is not None:
            on_stage("reviewer (semantic groups)")
        reviewer_def = load_agent_definition("reviewer")
        semantic_findings, groups_reviewed, group_calls = await _run_semantic_group_review(
            provider, reviewer_def, strategy.scenarios, suite.cases, requirements, nfrs, ledger
        )

        if on_stage is not None:
            on_stage("reviewer (fallback)")
        fallback_def = load_agent_definition("reviewer_fallback")
        fallback_findings, fallback_calls = await _run_fallback_review(
            provider, fallback_def, requirements, suite.cases, ledger
        )

    coverage_findings = _check_zero_coverage_requirements(traceability)

    review = _compose_review_report(
        review_id,
        target_run_id=run_id,
        structural_findings=structural_findings,
        semantic_findings=semantic_findings,
        fallback_findings=fallback_findings,
        coverage_findings=coverage_findings,
    )

    if ledger is not None:
        ledger.record_agent_step(
            agent_name="reviewer",
            metadata={
                "groups_reviewed": groups_reviewed,
                "group_calls": group_calls,
                "fallback_calls": fallback_calls,
                "structural_findings": len(structural_findings),
                "semantic_findings": len(semantic_findings),
                "fallback_findings": len(fallback_findings),
                "coverage_findings": len(coverage_findings),
            },
        )

    return PipelineResult(
        strategy=strategy,
        suite=suite,
        review=review,
        traceability=traceability,
        failed_scenario_ids=failed_scenario_ids,
    )
