from __future__ import annotations

import json

import pytest

from driftpin.consistency.checker import (
    PAIR_COUNT_WARNING_THRESHOLD,
    ConsistencyCheckAbortedError,
    run_consistency_check,
)
from driftpin.providers.base import CompletionResult
from driftpin.schemas.consistency import ConsistencyFindingSeverity, ConsistencyVerdict, PairType
from driftpin.schemas.requirements import (
    AcceptanceCriterion,
    NfrScope,
    NonFunctionalRequirement,
    Requirement,
    RiskTier,
)


def _requirement(
    req_id: str,
    description: str,
    acceptance_criteria: list[AcceptanceCriterion] | None = None,
) -> Requirement:
    return Requirement(
        requirement_id=req_id,
        title="Title",
        description=description,
        source_span=description,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
        risk_tier=RiskTier.MEDIUM,
        acceptance_criteria=acceptance_criteria or [],
    )


def _response(verdict: str, explanation: str = "") -> CompletionResult:
    return CompletionResult(
        content=json.dumps({"verdict": verdict, "explanation": explanation}),
        tokens_in=1,
        tokens_out=1,
        stop_reason="end_turn",
    )


def _applicability_response(applicable: bool, reason: str = "") -> CompletionResult:
    return CompletionResult(
        content=json.dumps({"applicable": applicable, "reason": reason}),
        tokens_in=1,
        tokens_out=1,
        stop_reason="end_turn",
    )


_SYNC_RETRY_NFR = NonFunctionalRequirement(
    nfr_id="NFR-1",
    text="Sync failure: retry up to 3 times before surfacing error to user.",
    scope=NfrScope.GLOBAL,
    source_doc_path="prd.md",
    source_doc_hash="hash-a",
)


@pytest.mark.asyncio
async def test_fixture_p_contradictory_peer_requirements_flagged_as_blocker(mock_provider_factory) -> None:
    """Fixture P: two requirements make mutually exclusive mandates about
    the same entity (permanent deletion vs 7-year retention) -- caught via
    the req_vs_peer pair type, since neither requirement references the
    other directly."""
    r1 = _requirement(
        "R-1", "User account data must not be recoverable after 24 hours following an erasure request."
    )
    r2 = _requirement("R-2", "User account data must be retained for 7 years for compliance purposes.")

    provider = mock_provider_factory(
        [_response("contradiction", "R-1 mandates permanent erasure while R-2 mandates 7-year retention.")]
    )

    report = await run_consistency_check(provider, [r1, r2], [])

    assert report.pairs_enumerated == 1
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.verdict == ConsistencyVerdict.CONTRADICTION
    assert finding.severity == ConsistencyFindingSeverity.BLOCKER
    assert set(finding.requirement_ids) == {"R-1", "R-2"}


@pytest.mark.asyncio
async def test_fixture_q_requirement_vs_own_ac_threshold_mismatch(mock_provider_factory) -> None:
    """Fixture Q: R-03 says "zero" while its own AC says "below 1" --
    caught via the req_vs_ac pair type."""
    r1 = _requirement(
        "R-1",
        "A user may not set a budget of zero.",
        acceptance_criteria=[AcceptanceCriterion(ac_id="AC-1", text="Budget entry field rejects values below 1.")],
    )

    provider = mock_provider_factory(
        [_response("threshold_mismatch", "R-1 prohibits zero while AC-1 rejects anything below 1.")]
    )

    report = await run_consistency_check(provider, [r1], [])

    assert report.pairs_enumerated == 1
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.verdict == ConsistencyVerdict.THRESHOLD_MISMATCH
    assert finding.severity == ConsistencyFindingSeverity.BLOCKER


@pytest.mark.asyncio
async def test_fixture_r_action_with_no_failure_path_is_silence_gap(mock_provider_factory) -> None:
    """Fixture R: a requirement describes a state-changing action
    (generates + sends a summary) with no failure/error handling
    specified anywhere -- caught via the req_vs_silence pair type, which
    has no second text to compare against."""
    r1 = _requirement(
        "R-1", "At the end of each month, the app generates a spending summary and sends it to the user."
    )

    provider = mock_provider_factory(
        [_response("silence_gap", "No failure path is specified if summary generation fails.")]
    )

    report = await run_consistency_check(provider, [r1], [])

    assert report.pairs_enumerated == 1
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.verdict == ConsistencyVerdict.SILENCE_GAP
    assert finding.severity == ConsistencyFindingSeverity.ASSUMPTION
    assert finding.requirement_ids == ["R-1"]


@pytest.mark.asyncio
async def test_fixture_s_modal_mismatch_between_requirement_and_its_ac(mock_provider_factory) -> None:
    """Fixture S: the requirement uses "should" while its own AC uses
    "must" for what reads as the same behavior -- caught via req_vs_ac."""
    r1 = _requirement(
        "R-1",
        "The system should remember user overrides and apply them to future transactions.",
        acceptance_criteria=[
            AcceptanceCriterion(
                ac_id="AC-1",
                text="The system must apply the remembered override to all future transactions from the same merchant.",
            )
        ],
    )

    provider = mock_provider_factory(
        [_response("modal_ambiguity", "R-1 says 'should' while AC-1 says 'must' for the same behavior.")]
    )

    report = await run_consistency_check(provider, [r1], [])

    assert report.pairs_enumerated == 1
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.verdict == ConsistencyVerdict.MODAL_AMBIGUITY
    assert finding.severity == ConsistencyFindingSeverity.ASSUMPTION


@pytest.mark.asyncio
async def test_fixture_t_consistent_peers_produce_zero_findings(mock_provider_factory) -> None:
    """Fixture T (over-flagging guard): two requirements share a domain
    entity ("dashboard", "view") but do not conflict at all -- the checker
    must return `consistent` and produce zero findings, not manufacture a
    tension because the pair was enumerated."""
    r1 = _requirement("R-1", "Users can view their transaction history in the Dashboard app.")
    r2 = _requirement("R-2", "Users can view their monthly budget totals in the Dashboard app.")

    provider = mock_provider_factory([_response("consistent")])

    report = await run_consistency_check(provider, [r1, r2], [])

    assert report.pairs_enumerated == 1
    assert report.findings == []


# 15 mutually distinct filler words: no shared token across any two of
# these requirements, and no substring overlap with the action/failure
# keyword lists pairs.py scans for -- so the only pairs these fixtures
# enumerate are the deliberate 15x15 req_vs_ac ones, keeping the pair
# count exactly predictable for the budget-guard assertions below.
_UNIQUE_WORDS = [
    "wordalpha", "wordbravo", "wordcharlie", "worddelta", "wordecho",
    "wordfoxtrot", "wordgolf", "wordhotel", "wordindia", "wordjuliet",
    "wordkilo", "wordlima", "wordmike", "wordnovember", "wordoscar",
]


def _requirement_with_many_acs(index: int) -> Requirement:
    word = _UNIQUE_WORDS[index]
    return _requirement(
        f"R-{index}",
        f"{word}.",
        acceptance_criteria=[AcceptanceCriterion(ac_id=f"AC-{index}-{j}", text=f"{word}.") for j in range(15)],
    )


@pytest.mark.asyncio
async def test_pair_count_budget_guard_aborts_before_any_llm_call_when_declined(mock_provider_factory) -> None:
    requirements = [_requirement_with_many_acs(i) for i in range(15)]
    assert len(requirements) * 15 > PAIR_COUNT_WARNING_THRESHOLD

    provider = mock_provider_factory([])  # any LLM call before the abort fails the test loudly

    with pytest.raises(ConsistencyCheckAbortedError) as exc_info:
        await run_consistency_check(provider, requirements, [], on_pair_count_check=lambda _count: False)

    assert exc_info.value.pair_count == 225
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_pair_count_budget_guard_proceeds_when_accepted(mock_provider_factory) -> None:
    requirements = [_requirement_with_many_acs(i) for i in range(15)]
    pair_count = len(requirements) * 15
    seen_counts: list[int] = []

    provider = mock_provider_factory([_response("consistent") for _ in range(pair_count)])

    report = await run_consistency_check(
        provider, requirements, [], on_pair_count_check=lambda count: (seen_counts.append(count), True)[1]
    )

    assert seen_counts == [pair_count]
    assert report.pairs_enumerated == pair_count
    assert report.findings == []


@pytest.mark.asyncio
async def test_silence_gap_credited_when_global_nfr_applicability_confirmed(mock_provider_factory) -> None:
    """A sync-related requirement covered by a global sync-retry NFR must
    be credited (no silence-gap pair) once the applicability check
    confirms the NFR actually governs its action -- the correct-crediting
    half of the fix, paired with the below test's correct-flagging half."""
    sync_requirement = _requirement("R-1", "The app must sync transactions from linked accounts every 24 hours.")

    provider = mock_provider_factory(
        [
            _applicability_response(True, "The NFR explicitly names sync failure."),
            _response("consistent"),  # verdict for the req_vs_nfr pair this NFR also creates
        ]
    )

    report = await run_consistency_check(provider, [sync_requirement], [_SYNC_RETRY_NFR])

    assert report.pairs_enumerated == 1  # only the req_vs_nfr pair -- no silence pair was ever built
    assert report.findings == []


@pytest.mark.asyncio
async def test_silence_gap_flagged_when_global_nfr_not_applicable(mock_provider_factory) -> None:
    """A summary-generation requirement sharing the same global sync-retry
    NFR must NOT be credited -- the applicability check correctly says the
    NFR doesn't govern this action, so the candidate becomes a real
    silence-gap pair and gets its own verdict call."""
    summary_requirement = _requirement(
        "R-9", "At the end of each month, the app generates a spending summary for the user."
    )

    provider = mock_provider_factory(
        [
            _applicability_response(False, "Sync retry policy has nothing to do with summary generation."),
            _response("consistent"),  # verdict for the req_vs_nfr pair this NFR also creates
            _response("silence_gap", "No failure path is specified if summary generation fails."),
        ]
    )

    report = await run_consistency_check(provider, [summary_requirement], [_SYNC_RETRY_NFR])

    assert report.pairs_enumerated == 2  # req_vs_nfr pair + the silence-gap pair that was NOT credited
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.pair_type == PairType.REQ_VS_SILENCE
    assert finding.verdict == ConsistencyVerdict.SILENCE_GAP
    assert finding.severity == ConsistencyFindingSeverity.ASSUMPTION
    assert finding.requirement_ids == ["R-9"]


@pytest.mark.asyncio
async def test_regression_pocketbudget_zero_candidates_bug_is_fixed(mock_provider_factory) -> None:
    """Direct regression test for the live-diagnosed bug: a registry with
    a sync-related requirement and an unrelated summary-generation
    requirement, both implicitly covered by the SAME single global
    sync-retry NFR. Before the fix, req_vs_silence enumerated ZERO
    candidates for either -- the NFR's keywords silently "covered" both.
    After the fix, the sync-related one is correctly credited and the
    summary-generation one is correctly flagged."""
    sync_requirement = _requirement("R-1", "The app must sync transactions from linked accounts every 24 hours.")
    summary_requirement = _requirement(
        "R-9",
        "At the end of each month, the app generates a spending summary: total spent, breakdown by "
        "category, biggest single transaction, and month-over-month comparison.",
    )

    provider = mock_provider_factory(
        [
            _applicability_response(True, "Governs sync's own failure handling."),
            _applicability_response(False, "Sync retry policy is unrelated to summary generation."),
            _response("consistent"),  # req_vs_nfr verdict for R-1
            _response("consistent"),  # req_vs_nfr verdict for R-9
            _response("silence_gap", "No failure path is specified if summary generation fails."),
        ]
    )

    report = await run_consistency_check(provider, [sync_requirement, summary_requirement], [_SYNC_RETRY_NFR])

    assert report.pairs_enumerated == 3  # 2 req_vs_nfr pairs + 1 silence-gap pair (R-9 only)
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.verdict == ConsistencyVerdict.SILENCE_GAP
    assert finding.requirement_ids == ["R-9"]
    assert "R-1" not in finding.requirement_ids
