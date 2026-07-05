"""`driftpin init`: provider setup, credential validation, and local-model conformance.

The interactive orchestrator (`run_init_wizard`) is a thin layer over a set of
independently testable async functions — validation and conformance never get
tangled up with prompt-rendering, so the decision logic can be exercised
without a real network or a real terminal.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from rich.console import Console
from rich.prompt import Confirm, Prompt

from driftpin.config.secrets import SecretStore
from driftpin.config.settings import (
    DriftpinConfig,
    ProviderConfig,
    ProviderKind,
    driftpin_dir,
    save_config,
)
from driftpin.providers.anthropic_provider import AnthropicProvider
from driftpin.providers.base import ProviderValidationError
from driftpin.providers.conformance import ConformanceResult, run_conformance_probe
from driftpin.providers.groq_provider import GroqProvider
from driftpin.providers.ollama_provider import OllamaProvider

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
ANTHROPIC_API_KEY_SECRET = "anthropic_api_key"
ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
GROQ_API_KEY_SECRET = "groq_api_key"
GROQ_API_KEY_ENV_VAR = "GROQ_API_KEY"


class InitWizardError(Exception):
    """Raised when setup cannot proceed; the wizard must stop, never degrade silently."""


async def list_ollama_models(base_url: str) -> list[str]:
    """Returns installed model names. Raises if Ollama is unreachable or empty —
    the wizard must never fall through to a free-text model prompt for local
    models."""
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise InitWizardError(
            f"Ollama is not reachable at {base_url}. Start it and retry, "
            "or choose the Anthropic provider instead."
        ) from exc

    models = [entry["name"] for entry in response.json().get("models", [])]
    if not models:
        raise InitWizardError(
            f"Ollama is running at {base_url} but has no models installed. "
            "Run `ollama pull <model>` first."
        )
    return models


async def validate_anthropic_key(api_key: str, model: str) -> None:
    """Raises ProviderValidationError if the key or model is rejected."""
    provider = AnthropicProvider(api_key=api_key, model=model)
    await provider.validate()


async def validate_groq_key(api_key: str, model: str) -> None:
    """Raises ProviderValidationError if the key or model is rejected."""
    provider = GroqProvider(api_key=api_key, model=model)
    await provider.validate()


async def probe_local_model_conformance(base_url: str, model: str) -> ConformanceResult:
    provider = OllamaProvider(base_url=base_url, model=model)
    return await run_conformance_probe(provider)


def build_config(
    kind: ProviderKind,
    model: str,
    base_url: str | None = None,
    local_model_path: str | None = None,
) -> DriftpinConfig:
    return DriftpinConfig(
        provider=ProviderConfig(
            kind=kind, model=model, base_url=base_url, local_model_path=local_model_path
        )
    )


def run_init_wizard(project_root: Path, console: Console | None = None) -> None:
    """Interactive entry point for `driftpin init` (no CI flags)."""
    console = console or Console()

    provider_choice = Prompt.ask(
        "Which provider will drive Driftpin's agents?",
        choices=["anthropic", "groq", "local"],
        default="anthropic",
    )

    if provider_choice == "anthropic":
        _run_anthropic_setup(project_root, console)
    elif provider_choice == "groq":
        _run_groq_setup(project_root, console)
    else:
        _run_ollama_setup(project_root, console)


def _run_anthropic_setup(project_root: Path, console: Console) -> None:
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV_VAR)
    if not api_key:
        api_key = Prompt.ask("Anthropic API key", password=True)

    model = Prompt.ask("Model", default=DEFAULT_ANTHROPIC_MODEL)

    console.print("Validating Anthropic credentials...")
    try:
        asyncio.run(validate_anthropic_key(api_key, model))
    except ProviderValidationError as exc:
        console.print(f"[red]Validation failed:[/red] {exc}")
        raise SystemExit(1) from exc

    secrets = SecretStore(driftpin_dir(project_root))
    secrets.set(ANTHROPIC_API_KEY_SECRET, api_key)

    config = build_config(ProviderKind.ANTHROPIC, model=model)
    save_config(project_root, config)
    console.print(f"[green]Configured Anthropic provider with model '{model}'.[/green]")


def _run_groq_setup(project_root: Path, console: Console) -> None:
    api_key = os.environ.get(GROQ_API_KEY_ENV_VAR)
    if not api_key:
        api_key = Prompt.ask("Groq API key", password=True)

    model = Prompt.ask("Model", default=DEFAULT_GROQ_MODEL)

    console.print("Validating Groq credentials...")
    try:
        asyncio.run(validate_groq_key(api_key, model))
    except ProviderValidationError as exc:
        console.print(f"[red]Validation failed:[/red] {exc}")
        raise SystemExit(1) from exc

    secrets = SecretStore(driftpin_dir(project_root))
    secrets.set(GROQ_API_KEY_SECRET, api_key)

    config = build_config(ProviderKind.GROQ, model=model)
    save_config(project_root, config)
    console.print(f"[green]Configured Groq provider with model '{model}'.[/green]")


def _run_ollama_setup(project_root: Path, console: Console) -> None:
    base_url = Prompt.ask("Ollama base URL", default=DEFAULT_OLLAMA_BASE_URL)
    local_model_path = Prompt.ask(
        "Local GGUF model path (leave blank to pick from installed Ollama models)",
        default="",
    )

    if local_model_path:
        model = local_model_path
    else:
        try:
            models = asyncio.run(list_ollama_models(base_url))
        except InitWizardError as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(1) from exc

        model = Prompt.ask("Choose an installed model", choices=models, default=models[0])

    console.print("Running structured-output conformance probe...")
    result = asyncio.run(probe_local_model_conformance(base_url, model))
    console.print(f"Conformance: {result.successes}/{result.total} probes passed.")

    if not result.passed:
        console.print(
            "[yellow]This model failed schema conformance on a majority of probes. "
            "Schema-first agents may produce invalid output.[/yellow]"
        )
        if not Confirm.ask("Proceed anyway?", default=False):
            raise SystemExit(1)

    config = build_config(
        ProviderKind.OLLAMA, model=model, base_url=base_url, local_model_path=local_model_path or None
    )
    save_config(project_root, config)
    console.print(f"[green]Configured Ollama provider with model '{model}'.[/green]")
