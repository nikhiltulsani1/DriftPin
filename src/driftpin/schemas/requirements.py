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


class Requirement(BaseModel):
    """A single traceable requirement extracted from a source document.

    `requirement_id` is derived deterministically from `source_doc_hash` and
    `source_span` by the registry, never assigned by the extracting LLM, so
    re-ingesting an unchanged document yields identical IDs.
    """

    requirement_id: str
    title: str
    description: str
    source_span: str = Field(
        description="Verbatim quote from the source document supporting this requirement."
    )
    source_doc_path: str
    source_doc_hash: str
    risk_tier: RiskTier
    version: int = Field(default=1, ge=1)
    superseded_by: str | None = Field(
        default=None, description="ID of the requirement that replaced this one, if any."
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CandidateRequirement(BaseModel):
    """Raw LLM extraction output, before the registry assigns an ID and stamps
    the source document's path and hash — the extracting LLM knows neither."""

    title: str
    description: str
    source_span: str = Field(
        description="Verbatim quote from the source document supporting this requirement."
    )
    risk_tier: RiskTier


class CandidateAmbiguity(BaseModel):
    """A contradiction, gap, or unresolvable ambiguity found during extraction.

    Routed to ASSUMPTIONS.md instead of being silently resolved into invented
    requirement coverage.
    """

    description: str
    source_span: str


class ExtractionResult(BaseModel):
    """Raw output of the extraction agent, before registry ID assignment."""

    candidate_requirements: list[CandidateRequirement]
    ambiguities: list[CandidateAmbiguity] = Field(default_factory=list)


class RegistryFile(BaseModel):
    """Persisted contents of .driftpin/requirements.json."""

    registry_version: int = Field(default=1, ge=1)
    requirements: list[Requirement] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
