"""Driftpin CLI entry point: `driftpin <command>`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
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
from driftpin.cli.init_wizard import run_init_wizard
from driftpin.cli.repl import run_chat_repl
from driftpin.ingestion.parsers import UnsupportedDocumentFormatError
from driftpin.providers.base import LLMProvider
from driftpin.providers.factory import ProviderNotConfiguredError, build_configured_provider

app = typer.Typer(
    name="driftpin",
    help="Requirement-centric agentic QA system.",
    no_args_is_help=True,
)
generate_app = typer.Typer(help="Generate a test strategy or full test cases from the registry.")
app.add_typer(generate_app, name="generate")
console = Console()


def _build_provider_or_exit(project_root: Path) -> LLMProvider:
    try:
        return build_configured_provider(project_root)
    except ProviderNotConfiguredError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


def _confirm_run(requirement_count: int, provider_name: str, model: str, skip_confirmation: bool) -> None:
    if skip_confirmation:
        return

    proceed = Confirm.ask(
        f"About to generate against {requirement_count} requirement(s) using "
        f"{provider_name}/{model}. Continue?",
        default=True,
    )
    if not proceed:
        raise typer.Exit(code=0)


@app.command()
def init(
    project_root: Path | None = typer.Option(
        None, "--project-root", help="Project directory to configure. Defaults to the current directory."
    ),
) -> None:
    """Configure the LLM provider for this project."""
    run_init_wizard(project_root or Path.cwd(), console)


@app.command()
def ingest(
    docs: list[Path] = typer.Option(
        ..., "--docs", help="One or more PRD/requirement documents to ingest."
    ),
    project_root: Path | None = typer.Option(
        None,
        "--project-root",
        help="Project directory containing .driftpin/. Defaults to the current directory.",
    ),
) -> None:
    """Parse documents, extract requirements, and merge them into the registry."""
    project_root = project_root or Path.cwd()
    provider = _build_provider_or_exit(project_root)

    try:
        outcome = asyncio.run(run_ingest(provider, project_root, docs))
    except DocumentNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except UnsupportedDocumentFormatError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"Ingestion run {outcome.run_id}")
    table.add_column("Document")
    table.add_column("Requirements", justify="right")
    table.add_column("Ambiguities", justify="right")
    for item in outcome.outcomes:
        table.add_row(str(item.doc_path), str(item.added_count), str(item.ambiguity_count))
    console.print(table)

    console.print(f"Registry saved. Ledger: {outcome.ledger.ledger_path}")
    if outcome.ledger.assumptions_path.exists():
        console.print(f"Assumptions flagged for review: {outcome.ledger.assumptions_path}")


@generate_app.command("strategy")
def generate_strategy(
    project_root: Path | None = typer.Option(
        None, "--project-root", help="Project directory containing .driftpin/."
    ),
    out: Path = typer.Option(Path("./out"), "--out", help="Directory to write the strategy report to."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt (for CI)."),
) -> None:
    """Generate a test strategy (scenarios) from the requirement registry, without cases."""
    project_root = project_root or Path.cwd()
    provider = _build_provider_or_exit(project_root)

    try:
        registry = open_registry(project_root)
        _confirm_run(len(registry.requirements), provider.name, provider.model, yes)
        outcome = asyncio.run(run_generate_strategy(provider, project_root))
    except EmptyRegistryError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

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

    out.mkdir(parents=True, exist_ok=True)
    strategy_path = out / f"{strategy.strategy_id}.json"
    strategy_path.write_text(strategy.model_dump_json(indent=2), encoding="utf-8")
    console.print(f"Strategy saved to {strategy_path}")
    console.print(f"Ledger: {outcome.ledger.ledger_path}")


@generate_app.command("cases")
def generate_cases(
    project_root: Path | None = typer.Option(
        None, "--project-root", help="Project directory containing .driftpin/."
    ),
    out: Path = typer.Option(Path("./out"), "--out", help="Directory to write generated artifacts to."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt (for CI)."),
) -> None:
    """Run the full strategy -> test cases -> review pipeline and render Excel + Markdown."""
    project_root = project_root or Path.cwd()
    provider = _build_provider_or_exit(project_root)

    try:
        registry = open_registry(project_root)
        _confirm_run(len(registry.requirements), provider.name, provider.model, yes)
        outcome = asyncio.run(run_generate_cases(provider, project_root, out))
    except EmptyRegistryError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

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


@app.command()
def chat(
    project_root: Path | None = typer.Option(
        None, "--project-root", help="Project directory containing .driftpin/."
    ),
) -> None:
    """Start an interactive session: ingest, generate, and inspect the registry."""
    run_chat_repl(project_root or Path.cwd(), console)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
