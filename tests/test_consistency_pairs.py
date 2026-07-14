from __future__ import annotations

from driftpin.consistency.pairs import (
    enumerate_consistency_pairs,
    enumerate_req_vs_ac_pairs,
    enumerate_req_vs_nfr_pairs,
    enumerate_req_vs_peer_pairs,
    extract_modal,
    find_silence_gap_candidates,
)
from driftpin.schemas.consistency import PairType
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
    nfr_ids: list[str] | None = None,
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
        nfr_ids=nfr_ids or [],
    )


def test_extract_modal_ranks_mandatory_over_recommended_over_optional() -> None:
    assert extract_modal("The system must reject invalid input.") == "mandatory"
    assert extract_modal("The system should remember overrides.") == "recommended"
    assert extract_modal("Users may export their data.") == "optional"
    assert extract_modal("Transactions are categorised automatically.") is None


def test_enumerate_req_vs_ac_pairs_pairs_every_ac_with_its_requirement() -> None:
    r1 = _requirement(
        "R-1",
        "A user may not set a budget of zero.",
        acceptance_criteria=[AcceptanceCriterion(ac_id="AC-1", text="Budget entry field rejects values below 1.")],
    )
    r2 = _requirement("R-2", "No ACs here.")

    pairs = enumerate_req_vs_ac_pairs([r1, r2])

    assert len(pairs) == 1
    assert pairs[0].pair_type == PairType.REQ_VS_AC
    assert pairs[0].req_id_1 == "R-1"
    assert pairs[0].req_id_2_or_ac_id_or_nfr_id == "AC-1"
    assert pairs[0].text_2 == "Budget entry field rejects values below 1."


def test_enumerate_req_vs_nfr_pairs_includes_global_nfrs_for_every_requirement() -> None:
    r1 = _requirement("R-1", "Users can export transaction history.")
    r2 = _requirement("R-2", "Users can link bank accounts.", nfr_ids=["NFR-scoped"])
    global_nfr = NonFunctionalRequirement(
        nfr_id="NFR-global",
        text="All financial data encrypted at rest.",
        scope=NfrScope.GLOBAL,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
    )
    scoped_nfr = NonFunctionalRequirement(
        nfr_id="NFR-scoped",
        text="Linking requires OAuth authentication.",
        scope=NfrScope.SCOPED,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
    )

    pairs = enumerate_req_vs_nfr_pairs([r1, r2], {"NFR-global": global_nfr, "NFR-scoped": scoped_nfr})

    r1_pairs = [p for p in pairs if p.req_id_1 == "R-1"]
    r2_pairs = [p for p in pairs if p.req_id_1 == "R-2"]
    assert {p.req_id_2_or_ac_id_or_nfr_id for p in r1_pairs} == {"NFR-global"}
    assert {p.req_id_2_or_ac_id_or_nfr_id for p in r2_pairs} == {"NFR-global", "NFR-scoped"}


def test_enumerate_req_vs_peer_pairs_requires_shared_distinctive_tokens() -> None:
    r1 = _requirement("R-1", "Users can export their transaction history as a CSV file.")
    r2 = _requirement("R-2", "Users can view their transaction history in the Dashboard.")
    r3 = _requirement("R-3", "The assistant answers questions about spending patterns.")

    pairs = enumerate_req_vs_peer_pairs([r1, r2, r3])

    pair_ids = {frozenset((p.req_id_1, p.req_id_2_or_ac_id_or_nfr_id)) for p in pairs}
    assert frozenset(("R-1", "R-2")) in pair_ids
    assert frozenset(("R-1", "R-3")) not in pair_ids
    assert frozenset(("R-2", "R-3")) not in pair_ids


def test_enumerate_req_vs_peer_pairs_caps_neighbors_per_requirement() -> None:
    """A hub requirement sharing vocabulary with many peers keeps only its
    top-N highest-overlap neighbors, not every peer above the threshold --
    the mechanism the budget guard depends on to stay sub-quadratic."""
    hub = _requirement("R-hub", "transaction transaction transaction category budget alert export")
    peers = [
        _requirement(f"R-{i}", "transaction transaction category budget alert export currency")
        for i in range(10)
    ]

    pairs = enumerate_req_vs_peer_pairs([hub, *peers])

    hub_pairs = [p for p in pairs if "R-hub" in (p.req_id_1, p.req_id_2_or_ac_id_or_nfr_id)]
    assert len(hub_pairs) <= 5


def test_find_silence_gap_candidates_flags_action_with_no_failure_handling() -> None:
    silent = _requirement(
        "R-1",
        "At the end of each month, the app generates a spending summary and sends it to the user.",
    )
    handled = _requirement(
        "R-2",
        "The app must sync transactions automatically; sync failure retries up to 3 times before "
        "surfacing an error to the user.",
    )
    no_action = _requirement("R-3", "Young professionals aged 22-35 want visibility into spending.")

    candidates = find_silence_gap_candidates([silent, handled, no_action], {})

    assert {c.requirement.requirement_id for c in candidates} == {"R-1"}
    assert candidates[0].candidate_global_nfrs == []


def test_find_silence_gap_candidates_resolves_immediately_via_scoped_nfr() -> None:
    """An explicit SCOPED NFR link is a specific, human-curated association
    with this requirement -- unlike a global NFR, it needs no applicability
    judgment, so a requirement covered by one is never even a candidate."""
    r = _requirement(
        "R-1", "The app must sync transactions from linked accounts every 24 hours.", nfr_ids=["NFR-scoped"]
    )
    scoped_nfr = NonFunctionalRequirement(
        nfr_id="NFR-scoped",
        text="Sync failure: retry up to 3 times before surfacing error to user.",
        scope=NfrScope.SCOPED,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
    )

    candidates = find_silence_gap_candidates([r], {"NFR-scoped": scoped_nfr})

    assert candidates == []


def test_find_silence_gap_candidates_defers_global_nfr_credit_to_applicability_check() -> None:
    """Regression test for the live PocketBudget bug this fix replaces: a
    global NFR's failure keyword must NOT silently resolve a candidate by
    keyword match alone. The requirement still surfaces as a candidate,
    carrying the NFR that needs an applicability judgment -- `checker.py`
    decides the rest, not this (deterministic, zero-LLM) function."""
    r = _requirement("R-1", "The app must sync transactions from linked accounts every 24 hours.")
    global_nfr = NonFunctionalRequirement(
        nfr_id="NFR-1",
        text="Sync failure: retry up to 3 times before surfacing error to user.",
        scope=NfrScope.GLOBAL,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
    )

    candidates = find_silence_gap_candidates([r], {"NFR-1": global_nfr})

    assert len(candidates) == 1
    assert candidates[0].requirement.requirement_id == "R-1"
    assert candidates[0].candidate_global_nfrs == [global_nfr]


def test_find_silence_gap_candidates_reproduces_pocketbudget_zero_candidates_bug_scenario() -> None:
    """A summary-generation requirement sharing the document's one global
    sync-retry NFR with a sync-related requirement must ALSO surface as a
    candidate needing its own applicability judgment -- the exact scenario
    that previously vanished from `req_vs_silence` entirely, because the
    keyword-only check credited every requirement in the registry from a
    single unrelated global NFR."""
    sync_requirement = _requirement("R-1", "The app must sync transactions from linked accounts every 24 hours.")
    summary_requirement = _requirement(
        "R-9",
        "At the end of each month, the app generates a spending summary: total spent, breakdown by "
        "category, biggest single transaction, and month-over-month comparison.",
    )
    global_nfr = NonFunctionalRequirement(
        nfr_id="NFR-1",
        text="Sync failure: retry up to 3 times before surfacing error to user.",
        scope=NfrScope.GLOBAL,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
    )

    candidates = find_silence_gap_candidates([sync_requirement, summary_requirement], {"NFR-1": global_nfr})

    candidates_by_id = {c.requirement.requirement_id: c for c in candidates}
    assert set(candidates_by_id) == {"R-1", "R-9"}
    assert candidates_by_id["R-1"].candidate_global_nfrs == [global_nfr]
    assert candidates_by_id["R-9"].candidate_global_nfrs == [global_nfr]


def test_enumerate_consistency_pairs_combines_three_deterministic_pair_types() -> None:
    r1 = _requirement(
        "R-1",
        "Users can export transaction history as a CSV file.",
        acceptance_criteria=[AcceptanceCriterion(ac_id="AC-1", text="Export completes within 10 seconds.")],
    )
    r2 = _requirement("R-2", "Users can view transaction history in the Dashboard.")
    nfr = NonFunctionalRequirement(
        nfr_id="NFR-1",
        text="All financial data encrypted at rest.",
        scope=NfrScope.GLOBAL,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
    )

    pairs = enumerate_consistency_pairs([r1, r2], [nfr])

    types_present = {p.pair_type for p in pairs}
    assert PairType.REQ_VS_AC in types_present
    assert PairType.REQ_VS_NFR in types_present
    assert PairType.REQ_VS_PEER in types_present
