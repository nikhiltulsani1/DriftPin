from __future__ import annotations

import pytest
from pydantic import ValidationError

from driftpin.schemas.requirements import Requirement, RiskTier
from driftpin.schemas.strategy import ExecutionRecommendation, OwningAgent, Scenario
from driftpin.schemas.test_cases import TestCase as CaseModel
from driftpin.schemas.test_cases import TestStep as StepModel


def _requirement(**overrides: object) -> Requirement:
    defaults: dict[str, object] = dict(
        requirement_id="R-abc12345",
        title="Password reset",
        description="Users can reset their password via email link.",
        source_span="Users must be able to reset their password via email.",
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
        risk_tier=RiskTier.HIGH,
    )
    defaults.update(overrides)
    return Requirement(**defaults)  # type: ignore[arg-type]


def test_requirement_round_trips_through_json() -> None:
    requirement = _requirement()
    restored = Requirement.model_validate_json(requirement.model_dump_json())
    assert restored == requirement


@pytest.mark.parametrize("field", ["title", "description", "source_span"])
def test_requirement_rejects_empty_string_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        _requirement(**{field: ""})


def test_scenario_requires_at_least_one_requirement_id() -> None:
    with pytest.raises(ValidationError):
        Scenario(
            scenario_id="S-1",
            title="Login flow",
            requirement_ids=[],
            owning_agent=OwningAgent.FUNCTIONAL_TESTER,
            risk_tier=RiskTier.HIGH,
            execution_recommendation=ExecutionRecommendation.AUTOMATE,
            recommendation_justification="Deterministic assertions, low setup cost.",
        )


def test_scenario_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        Scenario(
            scenario_id="S-1",
            title="",
            requirement_ids=["R-abc12345"],
            owning_agent=OwningAgent.FUNCTIONAL_TESTER,
            risk_tier=RiskTier.HIGH,
            execution_recommendation=ExecutionRecommendation.AUTOMATE,
            recommendation_justification="Deterministic assertions, low setup cost.",
        )


def test_scenario_rejects_empty_justification() -> None:
    with pytest.raises(ValidationError):
        Scenario(
            scenario_id="S-1",
            title="Login flow",
            requirement_ids=["R-abc12345"],
            owning_agent=OwningAgent.FUNCTIONAL_TESTER,
            risk_tier=RiskTier.HIGH,
            execution_recommendation=ExecutionRecommendation.AUTOMATE,
            recommendation_justification="",
        )


def test_scenario_accepts_valid_payload() -> None:
    scenario = Scenario(
        scenario_id="S-1",
        title="Login flow",
        requirement_ids=["R-abc12345"],
        owning_agent=OwningAgent.FUNCTIONAL_TESTER,
        risk_tier=RiskTier.HIGH,
        execution_recommendation=ExecutionRecommendation.AUTOMATE,
        recommendation_justification="Deterministic assertions, low setup cost.",
    )
    assert scenario.owning_agent == OwningAgent.FUNCTIONAL_TESTER


def test_test_case_requires_at_least_one_step() -> None:
    with pytest.raises(ValidationError):
        CaseModel(
            case_id="TC-1",
            scenario_id="S-1",
            requirement_ids=["R-abc12345"],
            title="Reset password",
            steps=[],
            owning_agent=OwningAgent.FUNCTIONAL_TESTER,
            execution_recommendation=ExecutionRecommendation.MANUAL,
        )


def test_test_case_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        CaseModel(
            case_id="TC-1",
            scenario_id="S-1",
            requirement_ids=["R-abc12345"],
            title="",
            steps=[StepModel(step_number=1, action="Click reset link", expected_result="Email sent")],
            owning_agent=OwningAgent.FUNCTIONAL_TESTER,
            execution_recommendation=ExecutionRecommendation.MANUAL,
        )


@pytest.mark.parametrize("field", ["action", "expected_result"])
def test_test_step_rejects_empty_fields(field: str) -> None:
    defaults: dict[str, object] = dict(step_number=1, action="Click reset link", expected_result="Email sent")
    defaults[field] = ""
    with pytest.raises(ValidationError):
        StepModel(**defaults)  # type: ignore[arg-type]


def test_test_case_accepts_valid_payload() -> None:
    case = CaseModel(
        case_id="TC-1",
        scenario_id="S-1",
        requirement_ids=["R-abc12345"],
        title="Reset password",
        steps=[StepModel(step_number=1, action="Click reset link", expected_result="Email sent")],
        owning_agent=OwningAgent.FUNCTIONAL_TESTER,
        execution_recommendation=ExecutionRecommendation.MANUAL,
    )
    assert case.steps[0].step_number == 1
