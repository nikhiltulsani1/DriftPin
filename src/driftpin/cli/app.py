"""Driftpin CLI entry point: `driftpin <command>`."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from driftpin.cli.init_wizard import run_init_wizard
from driftpin.config.settings import driftpin_dir
from driftpin.ingestion.extractor import extract_requirements
from driftpin.ingestion.parsers import UnsupportedDocumentFormatError, parse_document
from driftpin.ingestion.registry import RequirementRegistry, compute_doc_hash
from driftpin.ledger.ledger import RunLedger
from driftpin.providers.factory import ProviderNotConfiguredError, build_configured_provider

app = typer.Typer(
    name="driftpin",
    help="Requirement-centric agentic QA system.",
    no_args_is_help=True,
)
console = Console()


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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
