"""Interactive REPL: `driftpin chat`.

A thin, testable command parser wraps the same actions used by the one-shot
CLI commands (`cli/actions.py`), so a run behaves identically whether
triggered here or via `driftpin ingest` / `driftpin generate ...`. During
`/strategy` and `/cases`, the live status line reflects real pipeline stage
transitions via an `OnStage` callback — genuine progress visibility, not a
synthetic token stream, since the current structured-output calls are
single-shot rather than incrementally streamed.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from driftpin.cli.actions import (
    DocumentNotFoundError,
    EmptyRegistryError,
    open_registry,
    run_generate_cases,
    run_generate_strategy,
    run_ingest,
)
from driftpin.ingestion.parsers import UnsupportedDocumentFormatError
from driftpin.providers.base import LLMProvider
from driftpin.providers.factory import ProviderNotConfiguredError, build_configured_provider

_HELP_TEXT = """[bold]Commands[/bold]
  /help                      Show this message.
  /status                    Show project root, provider, and requirement count.
  /requirements               List requirements currently in the registry.
  /ingest <doc> [<doc> ...]  Parse and ingest one or more documents.
  /strategy                  Generate a test strategy from the registry.
  /cases                     Run the full strategy -> cases -> review pipeline.
  /exit, /quit               Leave the session."""

_DEFAULT_OUT_DIR = Path("./out")


@dataclass
class ParsedCommand:
    name: str
    args: list[str]


def parse_command(line: str) -> ParsedCommand | None:
    """Returns None for blank input. Raises ValueError on unbalanced quotes,
    same as the underlying `shlex.split` call."""
    stripped = line.lstrip("﻿").strip()
    if not stripped:
        return None
    tokens = shlex.split(stripped)
    name = tokens[0].lstrip("/").lower()
    return ParsedCommand(name=name, args=tokens[1:])


def run_chat_repl(project_root: Path, console: Console) -> None:
    try:
        provider = build_configured_provider(project_root)
    except ProviderNotConfiguredError as exc:
        console.print(f"[red]{exc}[/red]")
        return

    console.print(
        f"[bold]Driftpin interactive session[/bold] - project: {project_root} - "
        f"provider: {provider.name}/{provider.model}"
    )
    console.print("Type /help for commands.\n")

    while True:
        try:
            line = console.input("[bold cyan]driftpin>[/bold cyan] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        try:
            command = parse_command(line)
        except ValueError as exc:
            console.print(f"[red]Could not parse input: {exc}[/red]")
            continue

        if command is None:
            continue
        if command.name in ("exit", "quit"):
            break

        _dispatch(command, provider, project_root, console)


def _dispatch(command: ParsedCommand, provider: LLMProvider, project_root: Path, console: Console) -> None:
    if command.name == "help":
        console.print(_HELP_TEXT)
    elif command.name == "status":
        _handle_status(project_root, provider, console)
    elif command.name == "requirements":
        _handle_requirements(project_root, console)
    elif command.name == "ingest":
        _handle_ingest(provider, project_root, command.args, console)
    elif command.name == "strategy":
        _handle_strategy(provider, project_root, console)
    elif command.name == "cases":
        _handle_cases(provider, project_root, console)
    else:
        console.print(f"[yellow]Unknown command '/{command.name}'. Type /help for the list.[/yellow]")


def _handle_status(project_root: Path, provider: LLMProvider, console: Console) -> None:
    registry = open_registry(project_root)
    console.print(f"Project root: {project_root}")
    console.print(f"Provider: {provider.name}/{provider.model}")
    console.print(f"Requirements in registry: {len(registry.requirements)}")


def _handle_requirements(project_root: Path, console: Console) -> None:
    registry = open_registry(project_root)
    if not registry.requirements:
        console.print("[yellow]No requirements yet. Use /ingest <doc> first.[/yellow]")
        return

    table = Table(title="Requirements")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Risk Tier")
    for requirement in registry.requirements:
        table.add_row(requirement.requirement_id, requirement.title, requirement.risk_tier.value)
    console.print(table)


def _handle_ingest(provider: LLMProvider, project_root: Path, args: list[str], console: Console) -> None:
    if not args:
        console.print("[yellow]Usage: /ingest <doc-path> [<doc-path> ...][/yellow]")
        return

    doc_paths = [Path(a) for a in args]
    try:
        outcome = asyncio.run(run_ingest(provider, project_root, doc_paths))
    except DocumentNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    except UnsupportedDocumentFormatError as exc:
        console.print(f"[red]{exc}[/red]")
        return

    table = Table(title=f"Ingestion run {outcome.run_id}")
    table.add_column("Document")
    table.add_column("Requirements", justify="right")
    table.add_column("Ambiguities", justify="right")
    for item in outcome.outcomes:
        table.add_row(str(item.doc_path), str(item.added_count), str(item.ambiguity_count))
    console.print(table)
    if outcome.ledger.assumptions_path.exists():
        console.print(f"Assumptions flagged for review: {outcome.ledger.assumptions_path}")


def _handle_strategy(provider: LLMProvider, project_root: Path, console: Console) -> None:
    registry = open_registry(project_root)
    if not registry.requirements:
        console.print("[yellow]The requirement registry is empty. Use /ingest first.[/yellow]")
        return
    if not Confirm.ask(
        f"About to generate a strategy against {len(registry.requirements)} requirement(s) "
        f"using {provider.name}/{provider.model}. Continue?",
        default=True,
    ):
        return

    with console.status("Starting...") as status:

        def on_stage(name: str) -> None:
            status.update(f"Running {name}...")

        try:
            outcome = asyncio.run(run_generate_strategy(provider, project_root, on_stage=on_stage))
        except EmptyRegistryError as exc:
            console.print(f"[red]{exc}[/red]")
            return

    strategy = outcome.strategy
    table = Table(title=f"Strategy {strategy.strategy_id}")
    table.add_column("Scenario")
    table.add_column("Requirement IDs")
    table.add_column("Owning Agent")
    table.add_column("Execution")
    for scenario in strategy.scenarios:
        table.add_row(
            f"{scenario.scenario_id}: {scenario.title}",
            ", ".join(scenario.requirement_ids),
            scenario.owning_agent.value,
            scenario.execution_recommendation.value,
        )
    console.print(table)
    console.print(f"Ledger: {outcome.ledger.ledger_path}")


def _handle_cases(provider: LLMProvider, project_root: Path, console: Console) -> None:
    registry = open_registry(project_root)
    if not registry.requirements:
        console.print("[yellow]The requirement registry is empty. Use /ingest first.[/yellow]")
        return
    if not Confirm.ask(
        f"About to run the full pipeline against {len(registry.requirements)} requirement(s) "
        f"using {provider.name}/{provider.model}. Continue?",
        default=True,
    ):
        return

    with console.status("Starting...") as status:

        def on_stage(name: str) -> None:
            status.update(f"Running {name}...")

        try:
            outcome = asyncio.run(
                run_generate_cases(provider, project_root, _DEFAULT_OUT_DIR, on_stage=on_stage)
            )
        except EmptyRegistryError as exc:
            console.print(f"[red]{exc}[/red]")
            return

    result = outcome.result
    table = Table(title=f"Run {outcome.run_id}")
    table.add_column("Scenarios", justify="right")
    table.add_column("Test Cases", justify="right")
    table.add_column("Review Passed")
    table.add_row(
        str(len(result.strategy.scenarios)), str(len(result.suite.cases)), str(result.review.passed)
    )
    console.print(table)
    console.print(f"Excel report: {outcome.excel_path}")
    console.print(f"Markdown report: {outcome.markdown_path}")
    console.print(f"Ledger: {outcome.ledger.ledger_path}")
    if outcome.ledger.assumptions_path.exists():
        console.print(f"Assumptions flagged for review: {outcome.ledger.assumptions_path}")
