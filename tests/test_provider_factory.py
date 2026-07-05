from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from driftpin.providers.anthropic_provider import AnthropicProvider
from driftpin.providers.factory import ProviderNotConfiguredError, build_configured_provider
from driftpin.providers.groq_provider import GroqProvider
from driftpin.providers.ollama_provider import OllamaProvider


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests must not depend on whatever is actually stored in the host
    OS keyring — a real `driftpin init` run on this machine would otherwise
    make the "raises without key" tests pass or fail based on machine state
    rather than the code under test."""
    monkeypatch.setattr("driftpin.config.secrets.keyring.get_password", lambda *a, **k: None)
    monkeypatch.setattr("driftpin.config.secrets.keyring.set_password", lambda *a, **k: None)


def _write_config(project_root: Path, kind: str, model: str, base_url: str | None = None) -> None:
    driftpin_dir = project_root / ".driftpin"
    driftpin_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "provider": {
            "kind": kind,
            "model": model,
            "base_url": base_url,
            "local_model_path": None,
        },
        "schema_version": 1,
    }
    (driftpin_dir / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_build_configured_provider_raises_when_no_config(tmp_path: Path) -> None:
    with pytest.raises(ProviderNotConfiguredError):
        build_configured_provider(tmp_path)


def test_build_configured_provider_anthropic_uses_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, kind="anthropic", model="claude-sonnet-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    provider = build_configured_provider(tmp_path)

    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-sonnet-5"


def test_build_configured_provider_anthropic_raises_without_key(tmp_path: Path) -> None:
    _write_config(tmp_path, kind="anthropic", model="claude-sonnet-5")

    with pytest.raises(ProviderNotConfiguredError, match="Groq|Anthropic"):
        build_configured_provider(tmp_path)


def test_build_configured_provider_groq_uses_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, kind="groq", model="llama-3.3-70b-versatile")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    provider = build_configured_provider(tmp_path)

    assert isinstance(provider, GroqProvider)
    assert provider.model == "llama-3.3-70b-versatile"


def test_build_configured_provider_groq_raises_without_key(tmp_path: Path) -> None:
    _write_config(tmp_path, kind="groq", model="llama-3.3-70b-versatile")

    with pytest.raises(ProviderNotConfiguredError, match="Groq"):
        build_configured_provider(tmp_path)


def test_build_configured_provider_ollama(tmp_path: Path) -> None:
    _write_config(tmp_path, kind="ollama", model="llama3", base_url="http://localhost:11434")

    provider = build_configured_provider(tmp_path)

    assert isinstance(provider, OllamaProvider)
    assert provider.model == "llama3"


def test_build_configured_provider_ollama_raises_without_base_url(tmp_path: Path) -> None:
    _write_config(tmp_path, kind="ollama", model="llama3", base_url=None)

    with pytest.raises(ProviderNotConfiguredError, match="base_url"):
        build_configured_provider(tmp_path)
