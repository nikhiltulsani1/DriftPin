"""Schemas for the registry-level spec consistency pass.

This check compares the requirement set against ITSELF -- a requirement
against its own acceptance criteria, its applicable NFRs, its peer
requirements, and its own silence on failure handling -- which is a
different question from every other check in the pipeline (all of which
compare a generated test case against the spec). See
`driftpin/consistency/pairs.py` for how comparison pairs are enumerated
and `driftpin/consistency/checker.py` for how each pair is judged.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class PairType(StrEnum):
    REQ_VS_AC = "req_vs_ac"
    REQ_VS_NFR = "req_vs_nfr"
    REQ_VS_PEER = "req_vs_peer"
    REQ_VS_SILENCE = "req_vs_silence"
    REQ_VS_LIFECYCLE = "req_vs_lifecycle"


class ConsistencyPair(BaseModel):
    """One comparison unit sent to the checker call. `text_2` is empty for
    `req_vs_silence` and `req_vs_lifecycle` pairs, both of which ask the
    model to judge `text_1` alone -- against its own silence on failure
    handling, or against its own silence on one specific lifecycle state
    of an entity it references -- not against a second text. `entity` and
    `lifecycle_state` are only set for `req_vs_lifecycle` pairs."""

    pair_type: PairType
    req_id_1: str
    req_id_2_or_ac_id_or_nfr_id: str | None = None
    text_1: str = Field(min_length=1)
    text_2: str = ""
    entity: str | None = None
    lifecycle_state: str | None = None


class ConsistencyVerdict(StrEnum):
    CONSISTENT = "consistent"
    CONTRADICTION = "contradiction"
    THRESHOLD_MISMATCH = "threshold_mismatch"
    SILENCE_GAP = "silence_gap"
    MODAL_AMBIGUITY = "modal_ambiguity"
    # Never returned by a single checker call -- the prompt doesn't mention
    # it and the model is never asked to produce it. `checker.py` assigns
    # this itself when self-consistency mode (N>1 independent verdict calls
    # on the same pair) finds disagreement: taking a silent majority would
    # hide exactly the instability self-consistency exists to surface, so
    # an ambiguous pair is flagged for a human instead of resolved by vote.
    FLAGGED_FOR_REVIEW = "flagged_for_review"


class ConsistencyCheckResult(BaseModel):
    """Output schema for a single per-pair checker call."""

    verdict: ConsistencyVerdict
    explanation: str = Field(
        default="",
        description="One-sentence statement of the tension. Empty when verdict is 'consistent'.",
    )


class EntityRequirementLink(BaseModel):
    """One domain entity (e.g. "budget", "override") and every requirement
    that references it, as identified by the one-time `lifecycle-entities`
    extraction call `checker.py` runs before enumerating req_vs_lifecycle
    pairs. `requirement_ids` is filtered against the real registry the
    same way every other extraction output in this project is -- an
    extracting LLM never gets to assign or invent IDs that survive
    unchecked."""

    entity: str = Field(min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)


class LifecycleEntityExtraction(BaseModel):
    """Output schema for the one-time entity-extraction call. An empty
    `entities` list is the correct output when no requirement describes a
    persistent, stateful resource -- not every PRD has one."""

    entities: list[EntityRequirementLink] = Field(default_factory=list)


class NfrApplicabilityResult(BaseModel):
    """Output of the per-(requirement, global-NFR) applicability check
    `checker.py` runs to resolve a `SilenceGapCandidate` (see
    `driftpin.consistency.pairs`). Keyword presence in a global NFR's text
    is not sufficient to credit it as covering a specific requirement's
    failure handling -- this call asks whether it actually does."""

    applicable: bool
    reason: str = ""


class ConsistencyFindingSeverity(StrEnum):
    BLOCKER = "blocker"
    ASSUMPTION = "assumption"


_BLOCKER_VERDICTS = frozenset({ConsistencyVerdict.CONTRADICTION, ConsistencyVerdict.THRESHOLD_MISMATCH})


def severity_for_verdict(verdict: ConsistencyVerdict) -> ConsistencyFindingSeverity:
    if verdict in _BLOCKER_VERDICTS:
        return ConsistencyFindingSeverity.BLOCKER
    return ConsistencyFindingSeverity.ASSUMPTION


class ConsistencyFinding(BaseModel):
    pair_type: PairType
    verdict: ConsistencyVerdict
    severity: ConsistencyFindingSeverity
    requirement_ids: list[str]
    description: str


class ConsistencyReport(BaseModel):
    pairs_enumerated: int
    pairs_by_type: dict[str, int] = Field(default_factory=dict)
    findings: list[ConsistencyFinding] = Field(default_factory=list)

    @property
    def contradictions(self) -> int:
        return sum(1 for f in self.findings if f.verdict == ConsistencyVerdict.CONTRADICTION)

    @property
    def threshold_mismatches(self) -> int:
        return sum(1 for f in self.findings if f.verdict == ConsistencyVerdict.THRESHOLD_MISMATCH)

    @property
    def silence_gaps(self) -> int:
        return sum(1 for f in self.findings if f.verdict == ConsistencyVerdict.SILENCE_GAP)

    @property
    def modal_ambiguities(self) -> int:
        return sum(1 for f in self.findings if f.verdict == ConsistencyVerdict.MODAL_AMBIGUITY)

    @property
    def flagged_for_review(self) -> int:
        return sum(1 for f in self.findings if f.verdict == ConsistencyVerdict.FLAGGED_FOR_REVIEW)
