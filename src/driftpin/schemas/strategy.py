"""Schemas produced by the test-architect agent: strategy and scenario definitions."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from driftpin.schemas.requirements import RiskTier


class ExecutionRecommendation(StrEnum):
    AUTOMATE = "automate"
    MANUAL = "manual"
    HYBRID = "hybrid"


class OwningAgent(StrEnum):
    """Exactly one owner per scenario, preventing cross-domain duplication and gaps."""

    FUNCTIONAL_TESTER = "functional-tester"
    AUTOMATION_ENGINEER = "automation-engineer"
    API_BACKEND_TESTER = "api-backend-tester"
    ACCESSIBILITY_TESTER = "accessibility-tester"


class Scenario(BaseModel):
    """A single test scenario within a strategy, scoped to one or more requirements."""

    scenario_id: str
    title: str = Field(min_length=1)
    requirement_ids: list[str] = Field(min_length=1)
    owning_agent: OwningAgent
    risk_tier: RiskTier
    execution_recommendation: ExecutionRecommendation
    recommendation_justification: str = Field(
        min_length=1,
        description=(
            "One line citing the rubric basis: assertion determinism, flakiness risk, "
            "or setup cost vs. run frequency."
        ),
    )


class TestStrategy(BaseModel):
    """Top-level strategy document produced by test-architect for one ingestion run."""

    strategy_id: str
    scenarios: list[Scenario]
    coverage_notes: str = Field(default="")
