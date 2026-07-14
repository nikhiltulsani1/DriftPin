"""Schemas produced by the reviewer agent: independent, fresh-context audits."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class FindingSeverity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"
    ASSUMPTION = "assumption"


class ReviewerFinding(BaseModel):
    severity: FindingSeverity
    subject_id: str = Field(
        min_length=1, description="ID of the scenario, case, or artifact under review."
    )
    description: str = Field(min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)
    requirement_quote: str = Field(
        default="",
        description=(
            "For contradiction findings: a one-line verbatim quote of the conflicting "
            "requirement text, so the finding is checkable without re-reading the whole "
            "requirement. Empty for findings that aren't about a specific requirement conflict."
        ),
    )


class ReviewReport(BaseModel):
    """The final review artifact — assembled by Python from three sources:
    the zero-LLM-call structural review, one or more per-group semantic
    review calls, and one suite-wide fallback-rule call. No single LLM call
    returns this shape directly; `review_id`/`target_run_id`/`passed`/
    `summary` are all Python-determined, the same way case IDs and
    scenario/owning-agent fields are never trusted to a model's own
    bookkeeping elsewhere in this system."""

    review_id: str
    target_run_id: str
    findings: list[ReviewerFinding] = Field(default_factory=list)
    passed: bool
    summary: str = Field(min_length=1)


class GroupReviewResult(BaseModel):
    """Output of one per-group semantic review call, or the suite-wide
    fallback-rule call: just the findings that call turned up. Deliberately
    no `min_length` — a fully grounded group producing zero findings is the
    correct, expected result for clean input, not something to reject."""

    findings: list[ReviewerFinding] = Field(default_factory=list)
