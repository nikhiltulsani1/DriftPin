"""Schemas produced by the reviewer agent: independent, fresh-context audits."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class FindingSeverity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class ReviewerFinding(BaseModel):
    severity: FindingSeverity
    subject_id: str = Field(description="ID of the scenario, case, or artifact under review.")
    description: str
    requirement_ids: list[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    review_id: str
    target_run_id: str
    findings: list[ReviewerFinding] = Field(default_factory=list)
    passed: bool
    summary: str
