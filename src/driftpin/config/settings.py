"""Persisted configuration at `.driftpin/config.yaml`."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DRIFTPIN_DIR_NAME = ".driftpin"
CONFIG_FILE_NAME = "config.yaml"


class ProviderKind(StrEnum):
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    OPENAI = "openai"
    GROQ = "groq"
    NVIDIA = "nvidia"


class ProviderConfig(BaseModel):
    kind: ProviderKind
    model: str
    base_url: str | None = None
    local_model_path: str | None = None


class DriftpinConfig(BaseModel):
    provider: ProviderConfig
    schema_version: int = Field(default=1, ge=1)


def driftpin_dir(project_root: Path) -> Path:
    return project_root / DRIFTPIN_DIR_NAME


def config_path(project_root: Path) -> Path:
    return driftpin_dir(project_root) / CONFIG_FILE_NAME


def load_config(project_root: Path) -> DriftpinConfig | None:
    path = config_path(project_root)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return DriftpinConfig.model_validate(data)


def save_config(project_root: Path, config: DriftpinConfig) -> None:
    path = config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
