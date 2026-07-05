"""Schemas produced by the functional-tester agent: concrete test cases and traceability."""

from __future__ import annotations

from pydantic import BaseModel, Field

from driftpin.schemas.strategy import ExecutionRecommendation, OwningAgent


class TestStep(BaseModel):
    step_number: int = Field(ge=1)
    action: str = Field(min_length=1)
    expected_result: str = Field(min_length=1)


class TestCase(BaseModel):
    """A single executable test case tracing back to one or more requirements."""

    case_id: str
    scenario_id: str
    requirement_ids: list[str] = Field(min_length=1)
    title: str = Field(min_length=1)
    preconditions: str = Field(default="")
    steps: list[TestStep] = Field(min_length=1)
    owning_agent: OwningAgent
    execution_recommendation: ExecutionRecommendation


class TestSuite(BaseModel):
    """A complete generated suite for one ingestion run."""

    suite_id: str
    strategy_id: str
    cases: list[TestCase]


class TraceabilityRow(BaseModel):
    """One row of the traceability matrix: a requirement mapped to the cases covering it."""

    requirement_id: str
    requirement_title: str
    risk_tier: str
    case_ids: list[str]
    coverage_count: int = Field(ge=0)
