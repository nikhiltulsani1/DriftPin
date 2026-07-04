from __future__ import annotations

import pytest

from driftpin.agents.loader import (
    AgentDefinitionError,
    load_agent_definition,
    resolve_output_schema,
)
from driftpin.schemas.strategy import TestStrategy as StrategyModel


def test_resolve_output_schema_returns_pydantic_class() -> None:
    schema = resolve_output_schema("driftpin.schemas.strategy.TestStrategy")
    assert schema is StrategyModel


def test_resolve_output_schema_rejects_non_model_target() -> None:
    with pytest.raises(AgentDefinitionError):
        resolve_output_schema("driftpin.schemas.strategy.ExecutionRecommendation")


def test_resolve_output_schema_rejects_malformed_path() -> None:
    with pytest.raises(AgentDefinitionError):
        resolve_output_schema("not_a_dotted_path")


def test_load_agent_definition_raises_for_missing_agent() -> None:
    with pytest.raises(AgentDefinitionError):
        load_agent_definition("does-not-exist")


def test_load_agent_definition_loads_test_architect() -> None:
    definition = load_agent_definition("test-architect")
    assert definition.name == "test-architect"
    assert definition.output_schema == "driftpin.schemas.strategy.TestStrategy"
    assert definition.system_prompt.endswith(".md.j2")
