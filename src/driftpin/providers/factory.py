"""Builds the configured LLMProvider from persisted config and stored secrets."""

from __future__ import annotations

from pathlib import Path

from driftpin.cli.init_wizard import ANTHROPIC_API_KEY_ENV_VAR, ANTHROPIC_API_KEY_SECRET
from driftpin.config.secrets import SecretStore
from driftpin.config.settings import ProviderKind, driftpin_dir, load_config
from driftpin.providers.anthropic_provider import AnthropicProvider
from driftpin.providers.base import LLMProvider
from driftpin.providers.ollama_provider import OllamaProvider


class ProviderNotConfiguredError(Exception):
    """Raised when no `.driftpin/config.yaml` exists; the caller must run `driftpin init`."""


def build_configured_provider(project_root: Path) -> LLMProvider:
    config = load_config(project_root)
    if config is None:
        raise ProviderNotConfiguredError(
            "No provider configured for this project. Run `driftpin init` first."
        )

    if config.provider.kind == ProviderKind.ANTHROPIC:
        secrets = SecretStore(driftpin_dir(project_root))
        api_key = secrets.get(ANTHROPIC_API_KEY_SECRET, env_var=ANTHROPIC_API_KEY_ENV_VAR)
        if not api_key:
            raise ProviderNotConfiguredError(
                "No Anthropic API key found. Run `driftpin init` again or set "
                f"{ANTHROPIC_API_KEY_ENV_VAR}."
            )
        return AnthropicProvider(api_key=api_key, model=config.provider.model)

    if config.provider.kind == ProviderKind.OLLAMA:
        base_url = config.provider.base_url
        if not base_url:
            raise ProviderNotConfiguredError("Ollama provider configured without a base_url.")
        return OllamaProvider(base_url=base_url, model=config.provider.model)

    raise ProviderNotConfiguredError(f"Unsupported provider kind: {config.provider.kind}")
