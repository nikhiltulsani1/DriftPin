"""Loads agent definitions from YAML under `agents/` at the repo root."""

from __future__ import annotations

import importlib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from driftpin.paths import find_repo_dir


class AgentDefinitionError(Exception):
    """Raised when an agent YAML is malformed or references an unresolvable schema."""


class AgentDefinition(BaseModel):
    name: str
    model_binding: str | None = None
    system_prompt: str = Field(description="Filename of the jinja2 template under prompts/.")
    tools: list[str] = Field(default_factory=list)
    output_schema: str = Field(description="Dotted path to the pydantic response model.")
    max_iterations: int = Field(default=1, ge=1)


def load_agent_definition(name: str) -> AgentDefinition:
    """Loads `agents/<name>.yaml` relative to the repo root."""
    agents_dir = find_repo_dir("agents", Path(__file__))
    yaml_path = agents_dir / f"{name}.yaml"
    if not yaml_path.exists():
        raise AgentDefinitionError(f"No agent definition found at {yaml_path}")

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    return AgentDefinition.model_validate(data)


def resolve_output_schema(dotted_path: str) -> type[BaseModel]:
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        raise AgentDefinitionError(f"Invalid output_schema path: '{dotted_path}'")

    module = importlib.import_module(module_path)
    schema_cls = getattr(module, class_name, None)
    if not (isinstance(schema_cls, type) and issubclass(schema_cls, BaseModel)):
        raise AgentDefinitionError(
            f"'{dotted_path}' does not resolve to a pydantic BaseModel subclass."
        )
    return schema_cls
