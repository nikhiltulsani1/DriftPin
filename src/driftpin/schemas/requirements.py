"""Schemas for the requirement registry, the system's central data structure."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RiskTier(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AcceptanceCriterion(BaseModel):
    """One acceptance criterion linked to a requirement.

    `ac_id` is derived deterministically from the parent `requirement_id`
    and the criterion's own verbatim text by the registry, the same way
    `requirement_id` itself is derived — never assigned by the extracting
    LLM, so re-ingesting an unchanged document yields identical IDs.
    """

    ac_id: str
    text: str = Field(
        min_length=1,
        description="Verbatim quote from the source document stating this acceptance criterion.",
    )


class NfrScope(StrEnum):
    """Whether a non-functional requirement applies system-wide or only to
    specific requirements the document explicitly ties it to."""

    GLOBAL = "global"
    SCOPED = "scoped"


class NonFunctionalRequirement(BaseModel):
    """A performance, timing, reliability, security, or capacity constraint.

    Stored once in the registry regardless of how many requirements it
    applies to — a global NFR is not duplicated onto every requirement's
    own record. `nfr_id` is content-addressed the same way `requirement_id`
    and `ac_id` are.
    """

    nfr_id: str
    text: str = Field(
        min_length=1,
        description="Verbatim quote from the source document stating this NFR.",
    )
    scope: NfrScope
    source_doc_path: str
    source_doc_hash: str


class Requirement(BaseModel):
    """A single traceable requirement extracted from a source document.

    `requirement_id` is derived deterministically from `source_doc_hash` and
    `source_span` by the registry, never assigned by the extracting LLM, so
    re-ingesting an unchanged document yields identical IDs.
    """

    requirement_id: str
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_span: str = Field(
        min_length=1,
        description="Verbatim quote from the source document supporting this requirement.",
    )
    source_doc_path: str
    source_doc_hash: str
    risk_tier: RiskTier
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    nfr_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of scoped NFRs (RegistryFile.nfrs) that explicitly target this "
            "requirement. Global-scope NFRs are not listed here — they apply to "
            "every requirement implicitly and are resolved at review-input-build "
            "time, not duplicated into this list."
        ),
    )
    ac_extraction_failed: bool = Field(
        default=False,
        description=(
            "True when the acceptance-criteria section names criteria for this "
            "requirement that the deterministic parser couldn't label-match and "
            "the per-requirement LLM fallback then also failed (after one retry) "
            "to extract. Zero acceptance_criteria alone is not an error — a "
            "requirement can legitimately have none — this flag distinguishes "
            "'genuinely none' from 'extraction broke, human should check.'"
        ),
    )
    version: int = Field(default=1, ge=1)
    superseded_by: str | None = Field(
        default=None, description="ID of the requirement that replaced this one, if any."
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CandidateRequirement(BaseModel):
    """Raw LLM extraction output, before the registry assigns an ID and stamps
    the source document's path and hash — the extracting LLM knows neither."""

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_span: str = Field(
        min_length=1,
        description="Verbatim quote from the source document supporting this requirement.",
    )
    risk_tier: RiskTier
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description=(
            "Verbatim acceptance-criteria text linked to this requirement. Not "
            "populated by the extraction LLM call itself — filled in afterward by "
            "the deterministic AC parser (primary) or a per-requirement LLM "
            "fallback (secondary), never by asking one call to find every "
            "requirement AND every acceptance criterion at once. The registry "
            "assigns AC IDs, not the LLM."
        ),
    )
    ac_extraction_failed: bool = Field(default=False)


class ACFillResult(BaseModel):
    """Output of the per-requirement acceptance-criteria fallback call — the
    LLM's answer to 'which of these acceptance-criteria-section entries
    belong to this one requirement,' used only when the deterministic
    parser found an AC-like section but couldn't parse any labeled entries
    out of it."""

    acceptance_criteria: list[str] = Field(default_factory=list)


class CandidateNFR(BaseModel):
    """Raw LLM extraction output for one non-functional requirement."""

    text: str = Field(
        min_length=1,
        description="Verbatim quote from the source document stating this NFR.",
    )
    scope: NfrScope
    applies_to_source_spans: list[str] = Field(
        default_factory=list,
        description=(
            "Verbatim source_span text of each requirement this NFR applies to "
            "— must match a requirement's own source_span character-for-character. "
            "Empty for global scope."
        ),
    )


class CandidateAmbiguity(BaseModel):
    """A contradiction, gap, or unresolvable ambiguity found during extraction.

    Routed to ASSUMPTIONS.md instead of being silently resolved into invented
    requirement coverage.
    """

    description: str = Field(min_length=1)
    source_span: str = Field(min_length=1)


class ExtractionResult(BaseModel):
    """Raw output of the extraction agent, before registry ID assignment."""

    candidate_requirements: list[CandidateRequirement]
    ambiguities: list[CandidateAmbiguity] = Field(default_factory=list)
    candidate_nfrs: list[CandidateNFR] = Field(default_factory=list)
    unassigned_acs: list[str] = Field(
        default_factory=list,
        description=(
            "Acceptance-criteria text the AC parser/fallback found but could not "
            "link to any requirement — never silently dropped, surfaced for "
            "human review instead."
        ),
    )


class RegistryFile(BaseModel):
    """Persisted contents of .driftpin/requirements.json."""

    registry_version: int = Field(default=2, ge=1)
    requirements: list[Requirement] = Field(default_factory=list)
    nfrs: list[NonFunctionalRequirement] = Field(default_factory=list)
    unassigned_acs: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
