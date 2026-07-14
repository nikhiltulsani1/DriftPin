"""Orchestrates the spec consistency pass: enumerate pairs, check each
with its own scoped call, aggregate into a report.

Runs after ingestion completes and before test-architect ever sees the
registry -- a wholly separate question ("is the spec consistent with
itself?") from every review-stage check ("is this generated test
grounded in the spec?"), so it lives in its own module rather than
folded into `agents/orchestrator.py`.

Most pair enumeration is zero-LLM (see `pairs.py`), but req_vs_silence
candidates whose only apparent failure-handling coverage comes from a
GLOBAL NFR need one extra judgment call each first: a live PocketBudget
run found `req_vs_silence` enumerating ZERO candidates across a whole
9-requirement registry, because the one global NFR mentioning failure
("Sync failure: retry up to 3 times...") keyword-matched and silently
"covered" every requirement's failure handling, including a summary-
generation requirement with nothing to do with syncing. `_resolve_silence_gap_pairs`
below asks, per candidate NFR, whether it actually governs that specific
requirement's action before crediting it -- scoped NFRs (an explicit,
human-curated link) still resolve for free, with no LLM call, since
there's no ambiguity to judge there.

req_vs_lifecycle pairs need a one-time entity-extraction call
(`_extract_lifecycle_entities`) before `pairs.enumerate_req_vs_lifecycle_pairs`
can run at all -- unlike req_vs_ac/nfr/peer, there's no registry field
recording which requirements reference which domain entities. This call
always runs, even if the pair-count budget guard is later declined: it's
a single, fixed, small cost (one call regardless of registry size), the
same way the pipeline's own upstream extraction call isn't itself gated
by any downstream guard. Everything AFTER it -- silence-gap resolution
and every verdict call -- is gated behind the guard as before.
"""

from __future__ import annotations

from collections.abc import Callable

from driftpin.agents.loader import AgentDefinition, load_agent_definition
from driftpin.agents.runtime import run_agent
from driftpin.consistency.pairs import (
    SilenceGapCandidate,
    build_silence_gap_pair,
    enumerate_consistency_pairs,
    enumerate_req_vs_lifecycle_pairs,
    find_silence_gap_candidates,
)
from driftpin.ledger.ledger import RunLedger
from driftpin.providers.base import LLMProvider
from driftpin.schemas.consistency import (
    ConsistencyCheckResult,
    ConsistencyFinding,
    ConsistencyPair,
    ConsistencyReport,
    ConsistencyVerdict,
    EntityRequirementLink,
    LifecycleEntityExtraction,
    NfrApplicabilityResult,
    PairType,
    severity_for_verdict,
)
from driftpin.schemas.requirements import NonFunctionalRequirement, Requirement

PAIR_COUNT_WARNING_THRESHOLD = 200


class ConsistencyCheckAbortedError(Exception):
    """Raised when the pair-count budget guard is declined. The guard
    exists for cost visibility, not as a hard limit -- declining is an
    explicit human choice, never an automatic failure."""

    def __init__(self, pair_count: int) -> None:
        self.pair_count = pair_count
        super().__init__(
            f"Declined to run the spec consistency check over {pair_count} enumerated pairs."
        )


def _requirement_ids_for(pair: ConsistencyPair) -> list[str]:
    ids = [pair.req_id_1]
    if pair.pair_type == PairType.REQ_VS_PEER and pair.req_id_2_or_ac_id_or_nfr_id:
        ids.append(pair.req_id_2_or_ac_id_or_nfr_id)
    return ids


async def _check_pair(
    provider: LLMProvider,
    checker_def: AgentDefinition,
    pair: ConsistencyPair,
    ledger: RunLedger | None,
) -> ConsistencyCheckResult:
    raw = await run_agent(checker_def, provider, context={"pair": pair}, ledger=ledger)
    assert isinstance(raw, ConsistencyCheckResult)
    return raw


async def _nfr_applies_to_requirement(
    provider: LLMProvider,
    applicability_def: AgentDefinition,
    requirement: Requirement,
    nfr: NonFunctionalRequirement,
    ledger: RunLedger | None,
) -> bool:
    raw = await run_agent(
        applicability_def, provider, context={"requirement": requirement, "nfr": nfr}, ledger=ledger
    )
    assert isinstance(raw, NfrApplicabilityResult)
    return raw.applicable


def _estimated_silence_gap_cost(candidates: list[SilenceGapCandidate]) -> int:
    """Worst-case call count for resolving every candidate: every
    applicability check comes back False (no short-circuit) plus one
    verdict call for the pair it then becomes. A candidate with no
    global NFRs to check still costs exactly 1 (the verdict call for its
    already-definite gap)."""
    return sum(len(candidate.candidate_global_nfrs) + 1 for candidate in candidates)


async def _resolve_silence_gap_pairs(
    provider: LLMProvider,
    candidates: list[SilenceGapCandidate],
    ledger: RunLedger | None,
) -> tuple[list[ConsistencyPair], int]:
    """Returns the resolved pairs and the number of applicability calls
    actually spent (short-circuits on the first applicable NFR found per
    candidate, so this is usually well under the worst-case estimate the
    budget guard uses)."""
    if not candidates:
        return [], 0
    applicability_def = load_agent_definition("nfr-applicability")
    pairs: list[ConsistencyPair] = []
    applicability_calls = 0
    for candidate in candidates:
        credited = False
        for nfr in candidate.candidate_global_nfrs:
            applicability_calls += 1
            if await _nfr_applies_to_requirement(provider, applicability_def, candidate.requirement, nfr, ledger):
                credited = True
                break
        if not credited:
            pairs.append(build_silence_gap_pair(candidate.requirement))
    return pairs, applicability_calls


async def _extract_lifecycle_entities(
    provider: LLMProvider,
    requirements: list[Requirement],
    ledger: RunLedger | None,
) -> list[EntityRequirementLink]:
    """One call, always. Filters `requirement_ids` against the real
    registry the same way every other extraction output in this project
    is filtered -- the extracting LLM never gets to assign or invent IDs
    that survive unchecked into pair enumeration."""
    if not requirements:
        return []
    extraction_def = load_agent_definition("lifecycle-entities")
    raw = await run_agent(extraction_def, provider, context={"requirements": requirements}, ledger=ledger)
    assert isinstance(raw, LifecycleEntityExtraction)
    known_ids = {r.requirement_id for r in requirements}
    return [
        EntityRequirementLink(
            entity=link.entity,
            requirement_ids=[rid for rid in link.requirement_ids if rid in known_ids],
        )
        for link in raw.entities
        if link.entity.strip()
    ]


async def run_consistency_check(
    provider: LLMProvider,
    requirements: list[Requirement],
    nfrs: list[NonFunctionalRequirement],
    ledger: RunLedger | None = None,
    on_pair_count_check: Callable[[int], bool] | None = None,
) -> ConsistencyReport:
    deterministic_pairs = enumerate_consistency_pairs(requirements, nfrs)
    nfrs_by_id = {nfr.nfr_id: nfr for nfr in nfrs}
    silence_candidates = find_silence_gap_candidates(requirements, nfrs_by_id)

    # The one fixed, always-spent call: entity extraction has no
    # deterministic substitute, so it runs regardless of the guard
    # decision below (see this module's docstring). Its result lets the
    # guard estimate include the EXACT req_vs_lifecycle pair count,
    # rather than a guess.
    entity_links = await _extract_lifecycle_entities(provider, requirements, ledger)
    requirements_by_id = {r.requirement_id: r for r in requirements}
    lifecycle_pairs = enumerate_req_vs_lifecycle_pairs(entity_links, requirements_by_id)

    # Guarded before any FURTHER LLM call -- the silence-gap applicability
    # checks and every verdict call, none of which the pair count alone
    # would capture, since those happen ahead of and separately from the
    # per-pair verdict loop below.
    estimated_total = (
        len(deterministic_pairs) + _estimated_silence_gap_cost(silence_candidates) + len(lifecycle_pairs)
    )
    over_budget = estimated_total > PAIR_COUNT_WARNING_THRESHOLD
    if over_budget and on_pair_count_check is not None and not on_pair_count_check(estimated_total):
        raise ConsistencyCheckAbortedError(estimated_total)

    silence_pairs, applicability_calls = await _resolve_silence_gap_pairs(provider, silence_candidates, ledger)
    pairs = [*deterministic_pairs, *silence_pairs, *lifecycle_pairs]

    pairs_by_type: dict[str, int] = {}
    for pair in pairs:
        pairs_by_type[pair.pair_type.value] = pairs_by_type.get(pair.pair_type.value, 0) + 1

    checker_def = load_agent_definition("consistency-checker")
    findings: list[ConsistencyFinding] = []
    for pair in pairs:
        result = await _check_pair(provider, checker_def, pair, ledger)
        if result.verdict == ConsistencyVerdict.CONSISTENT:
            continue
        findings.append(
            ConsistencyFinding(
                pair_type=pair.pair_type,
                verdict=result.verdict,
                severity=severity_for_verdict(result.verdict),
                requirement_ids=_requirement_ids_for(pair),
                description=result.explanation,
            )
        )

    report = ConsistencyReport(pairs_enumerated=len(pairs), pairs_by_type=pairs_by_type, findings=findings)

    if ledger is not None:
        ledger.record_agent_step(
            agent_name="consistency-checker",
            metadata={
                "pairs_enumerated": report.pairs_enumerated,
                "pairs_by_type": pairs_by_type,
                "pair_calls": len(pairs),
                "silence_gap_candidates": len(silence_candidates),
                "nfr_applicability_calls": applicability_calls,
                "lifecycle_entities_extracted": len(entity_links),
                "lifecycle_pairs": len(lifecycle_pairs),
                "contradictions": report.contradictions,
                "threshold_mismatches": report.threshold_mismatches,
                "silence_gaps": report.silence_gaps,
                "modal_ambiguities": report.modal_ambiguities,
            },
        )
        for finding in findings:
            ledger.record_assumption(
                heading=(
                    f"Specification consistency: {finding.verdict.value} "
                    f"({', '.join(finding.requirement_ids)})"
                ),
                detail=finding.description,
            )

    return report
