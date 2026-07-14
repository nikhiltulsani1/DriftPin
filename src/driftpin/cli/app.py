"""Driftpin CLI entry point: `driftpin <command>`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from driftpin.agents.orchestrator import TooManyScenariosError
from driftpin.cli.actions import (
    DocumentNotFoundError,
    EmptyRegistryError,
    GenerationAbortedError,
    open_registry,
    run_generate_cases,
    run_generate_strategy,
    run_ingest,
)
from driftpin.cli.init_wizard import run_init_wizard
from driftpin.cli.repl import run_chat_repl
from driftpin.consistency.checker import ConsistencyCheckAbortedError
from driftpin.ingestion.parsers import UnsupportedDocumentFormatError
from driftpin.providers.base import LLMProvider, ServerExhaustedError
from driftpin.providers.factory import ProviderNotConfiguredError, build_configured_provider
from driftpin.render.labels import build_requirement_labels, labels_for, substitute_labels_in_text
from driftpin.schemas.consistency import ConsistencyReport

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


def _make_scenario_count_check(skip_confirmation: bool) -> Callable[[int], bool]:
    def _check(scenario_count: int) -> bool:
        if skip_confirmation:
            console.print(
                f"[yellow]{scenario_count} scenarios generated (above the 100-scenario guard) — "
                "proceeding without confirmation (--yes).[/yellow]"
            )
            return True
        console.print(
            f"[yellow]This PRD produced {scenario_count} scenarios, above the 100-scenario "
            "guard — it may be too large for a single run; consider splitting it by module.[/yellow]"
        )
        return Confirm.ask("Proceed with the full fill stage anyway?", default=False)

    return _check


def _make_pair_count_check(skip_confirmation: bool) -> Callable[[int], bool]:
    def _check(pair_count: int) -> bool:
        if skip_confirmation:
            console.print(
                f"[yellow]{pair_count} consistency pairs enumerated — this will use ~{pair_count} "
                "LLM calls — proceeding without confirmation (--yes).[/yellow]"
            )
            return True
        console.print(
            f"[yellow]{pair_count} consistency pairs enumerated — this will use ~{pair_count} "
            "LLM calls.[/yellow]"
        )
        return Confirm.ask("Proceed?", default=True)

    return _check


def _make_consistency_report_check(skip_confirmation: bool) -> Callable[[ConsistencyReport], bool]:
    def _check(report: ConsistencyReport) -> bool:
        summary = (
            f"Found {len(report.findings)} spec issue(s) ({report.contradictions} contradiction(s), "
            f"{report.threshold_mismatches} threshold mismatch(es), {report.silence_gaps} silence "
            f"gap(s), {report.modal_ambiguities} modal ambiguity(ies), {report.flagged_for_review} "
            f"flagged for review)."
        )
        if not report.findings:
            console.print(f"[green]{summary}[/green]")
            return True
        if skip_confirmation:
            console.print(
                f"[yellow]{summary} Proceeding without confirmation (--yes) — see ASSUMPTIONS.md.[/yellow]"
            )
            return True
        console.print(f"[yellow]{summary} Review ASSUMPTIONS.md before proceeding.[/yellow]")
        return Confirm.ask("Proceed with scenario generation?", default=False)

    return _check


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
    except ServerExhaustedError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"Ingestion run {outcome.run_id}")
    table.add_column("Document")
    table.add_column("Requirements", justify="right")
    table.add_column("ACs", justify="right")
    table.add_column("Ambiguities", justify="right")
    for item in outcome.outcomes:
        table.add_row(
            str(item.doc_path), str(item.added_count), str(item.acs_extracted_count), str(item.ambiguity_count)
        )
    console.print(table)

    for item in outcome.outcomes:
        if item.zero_ac_requirement_ids:
            console.print(
                f"[yellow]No acceptance criteria found for: {', '.join(item.zero_ac_requirement_ids)}[/yellow]"
            )
        if item.unassigned_ac_count:
            console.print(
                f"[yellow]{item.unassigned_ac_count} acceptance criterion/criteria could not be linked "
                "to a requirement — see assumptions.[/yellow]"
            )

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
    self_consistency_n: int = typer.Option(
        1,
        "--self-consistency-n",
        help=(
            "Rerun each spec-consistency verdict this many times independently and require "
            "unanimous agreement (disagreement is flagged for review, not majority-voted). "
            "1 = off (default). Multiplies consistency-check LLM calls by N -- meant for a "
            "deliberate GATE-style verification run, not routine use."
        ),
    ),
) -> None:
    """Generate a test strategy (scenarios) from the requirement registry, without cases."""
    project_root = project_root or Path.cwd()
    provider = _build_provider_or_exit(project_root)

    try:
        registry = open_registry(project_root)
        _confirm_run(len(registry.requirements), provider.name, provider.model, yes)
        outcome = asyncio.run(
            run_generate_strategy(
                provider,
                project_root,
                out,
                on_pair_count_check=_make_pair_count_check(yes),
                on_consistency_report=_make_consistency_report_check(yes),
                self_consistency_n=self_consistency_n,
            )
        )
    except EmptyRegistryError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except (ConsistencyCheckAbortedError, GenerationAbortedError) as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=0) from exc
    except ServerExhaustedError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    strategy = outcome.strategy
    labels = build_requirement_labels([r.requirement_id for r in registry.requirements])
    table = Table(title=f"Strategy {strategy.strategy_id}")
    table.add_column("Scenario")
    table.add_column("Requirements")
    table.add_column("Owning Agent")
    table.add_column("Execution")
    for scenario in strategy.scenarios:
        table.add_row(
            f"{scenario.scenario_id}: {substitute_labels_in_text(scenario.title, labels)}",
            ", ".join(labels_for(scenario.requirement_ids, labels)),
            scenario.owning_agent.value,
            scenario.execution_recommendation.value,
        )
    console.print(table)

    console.print(f"Strategy saved to {outcome.strategy_path}")
    console.print(f"Markdown report saved to {outcome.markdown_path}")
    console.print(f"Ledger: {outcome.ledger.ledger_path}")


@generate_app.command("cases")
def generate_cases(
    project_root: Path | None = typer.Option(
        None, "--project-root", help="Project directory containing .driftpin/."
    ),
    out: Path = typer.Option(Path("./out"), "--out", help="Directory to write generated artifacts to."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt (for CI)."),
    self_consistency_n: int = typer.Option(
        1,
        "--self-consistency-n",
        help=(
            "Rerun each spec-consistency verdict this many times independently and require "
            "unanimous agreement (disagreement is flagged for review, not majority-voted). "
            "1 = off (default). Multiplies consistency-check LLM calls by N -- meant for a "
            "deliberate GATE-style verification run, not routine use."
        ),
    ),
) -> None:
    """Run the full strategy -> test cases -> review pipeline and render Excel + Markdown."""
    project_root = project_root or Path.cwd()
    provider = _build_provider_or_exit(project_root)

    try:
        registry = open_registry(project_root)
        _confirm_run(len(registry.requirements), provider.name, provider.model, yes)
        outcome = asyncio.run(
            run_generate_cases(
                provider,
                project_root,
                out,
                on_scenario_count_check=_make_scenario_count_check(yes),
                on_pair_count_check=_make_pair_count_check(yes),
                on_consistency_report=_make_consistency_report_check(yes),
                self_consistency_n=self_consistency_n,
            )
        )
    except EmptyRegistryError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except (ConsistencyCheckAbortedError, GenerationAbortedError) as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=0) from exc
    except TooManyScenariosError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    except ServerExhaustedError as exc:
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

    zero_coverage = [row for row in result.traceability if row.coverage_count == 0]
    if zero_coverage:
        ids = ", ".join(row.requirement_id for row in zero_coverage)
        console.print(
            f"[bold red]WARNING: {len(zero_coverage)} requirement(s) have ZERO test coverage "
            f"in the final suite: {ids}. Review Passed is forced False.[/bold red]"
        )

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
