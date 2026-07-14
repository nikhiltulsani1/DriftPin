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
    assumptions: list[str] = Field(
        default_factory=list,
        description=(
            "Behavior this case needed but the linked requirement doesn't specify "
            "(error handling, endpoint shapes, status codes, thresholds, UI responses), "
            "stated as 'ASSUMED: <behavior> — not specified in <Req-ID>'. Never used to "
            "smuggle invented specifics into a step's action/expected_result instead."
        ),
    )


class TestSuite(BaseModel):
    """A complete generated suite for one ingestion run."""

    suite_id: str
    strategy_id: str
    cases: list[TestCase]


class FillResult(BaseModel):
    """Output of a single functional-tester fill call: the concrete test cases
    for exactly one scenario.

    Deliberately has no `min_length` on `cases` — an empty list is a valid,
    schema-conformant response that the orchestrator's completeness
    enforcement (not schema validation) is responsible for detecting and
    retrying. `case_id` values here are placeholders; Python assigns final
    sequential IDs across the whole suite at merge time, the same way
    requirement IDs are never trusted to the extracting LLM.
    """

    cases: list[TestCase] = Field(default_factory=list)


class TraceabilityRow(BaseModel):
    """One row of the traceability matrix: a requirement mapped to the cases covering it."""

    requirement_id: str
    requirement_title: str
    risk_tier: str
    case_ids: list[str]
    coverage_count: int = Field(ge=0)
