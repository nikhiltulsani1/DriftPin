"""Driftpin CLI entry point: `driftpin <command>`."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from driftpin.agents.orchestrator import generate_strategy_only, run_pipeline
from driftpin.cli.init_wizard import run_init_wizard
from driftpin.config.settings import driftpin_dir
from driftpin.ingestion.extractor import extract_requirements
from driftpin.ingestion.parsers import UnsupportedDocumentFormatError, parse_document
from driftpin.ingestion.registry import RequirementRegistry, compute_doc_hash
from driftpin.ledger.ledger import RunLedger
from driftpin.providers.factory import ProviderNotConfiguredError, build_configured_provider
from driftpin.render.excel import save_excel_workbook
from driftpin.render.headers import build_header
from driftpin.render.markdown import render_markdown_report

app = typer.Typer(
    name="driftpin",
    help="Requirement-centric agentic QA system.",
    no_args_is_help=True,
)
generate_app = typer.Typer(help="Generate a test strategy or full test cases from the registry.")
app.add_typer(generate_app, name="generate")
console = Console()


def _load_registry_or_exit(project_root: Path) -> RequirementRegistry:
    registry = RequirementRegistry(driftpin_dir(project_root) / "requirements.json")
    if not registry.requirements:
        console.print(
            "[red]The requirement registry is empty. Run `driftpin ingest` against a "
            "PRD first.[/red]"
        )
        raise typer.Exit(code=1)
    return registry


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
    try:
        provider = build_configured_provider(project_root)
    except ProviderNotConfiguredError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    run_id = uuid.uuid4().hex[:12]
    ledger = RunLedger(driftpin_dir(project_root), run_id=run_id)
    registry = RequirementRegistry(driftpin_dir(project_root) / "requirements.json")

    table = Table(title=f"Ingestion run {run_id}")
    table.add_column("Document")
    table.add_column("Requirements", justify="right")
    table.add_column("Ambiguities", justify="right")

    for doc_path in docs:
        if not doc_path.exists():
            console.print(f"[red]Document not found: {doc_path}[/red]")
            raise typer.Exit(code=1)

        try:
            blocks = parse_document(doc_path)
        except UnsupportedDocumentFormatError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        doc_hash = compute_doc_hash(doc_path)
        extraction = asyncio.run(extract_requirements(provider, blocks, ledger=ledger))

        added = registry.ingest(
            extraction, source_doc_path=str(doc_path), source_doc_hash=doc_hash
        )

        for ambiguity in extraction.ambiguities:
            ledger.record_assumption(
                heading=f"{doc_path.name}: {ambiguity.description[:80]}",
                detail=f"Source span: {ambiguity.source_span}",
            )

        table.add_row(str(doc_path), str(len(added)), str(len(extraction.ambiguities)))

    registry.save()
    console.print(table)
    console.print(f"Registry saved. Ledger: {ledger.ledger_path}")
    if ledger.assumptions_path.exists():
        console.print(f"Assumptions flagged for review: {ledger.assumptions_path}")


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
    try:
        provider = build_configured_provider(project_root)
    except ProviderNotConfiguredError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    registry = _load_registry_or_exit(project_root)
    _confirm_run(len(registry.requirements), provider.name, provider.model, yes)

    run_id = uuid.uuid4().hex[:12]
    ledger = RunLedger(driftpin_dir(project_root), run_id=run_id)
    strategy = asyncio.run(
        generate_strategy_only(provider, registry.requirements, run_id=run_id, ledger=ledger)
    )

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
    console.print(f"Ledger: {ledger.ledger_path}")


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
    try:
        provider = build_configured_provider(project_root)
    except ProviderNotConfiguredError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    registry = _load_registry_or_exit(project_root)
    _confirm_run(len(registry.requirements), provider.name, provider.model, yes)

    run_id = uuid.uuid4().hex[:12]
    ledger = RunLedger(driftpin_dir(project_root), run_id=run_id)
    result = asyncio.run(run_pipeline(provider, registry.requirements, run_id=run_id, ledger=ledger))

    header = build_header(
        run_id=run_id, requirements=registry.requirements, registry_version=registry.registry_version
    )

    out.mkdir(parents=True, exist_ok=True)
    excel_path = out / f"{run_id}.xlsx"
    markdown_path = out / f"{run_id}.md"
    save_excel_workbook(result, header, excel_path)
    markdown_path.write_text(render_markdown_report(result, header), encoding="utf-8")

    table = Table(title=f"Run {run_id}")
    table.add_column("Scenarios", justify="right")
    table.add_column("Test Cases", justify="right")
    table.add_column("Review Passed")
    table.add_row(
        str(len(result.strategy.scenarios)), str(len(result.suite.cases)), str(result.review.passed)
    )
    console.print(table)
    console.print(f"Excel report: {excel_path}")
    console.print(f"Markdown report: {markdown_path}")
    console.print(f"Ledger: {ledger.ledger_path}")
    if ledger.assumptions_path.exists():
        console.print(f"Assumptions flagged for review: {ledger.assumptions_path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
