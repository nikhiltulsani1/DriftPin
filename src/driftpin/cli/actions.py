"""Shared business logic behind `driftpin ingest` / `generate strategy` / `generate
cases` and the interactive REPL's equivalent commands — one implementation,
two front-ends, so a run behaves identically whether triggered from a one-shot
CLI invocation or from inside `driftpin chat`.

This module is presentation-agnostic: it returns data, never prints. Callers
(the typer commands in `app.py`, the REPL loop in `repl.py`) own formatting.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from driftpin.agents.orchestrator import (
    OnStage,
    PipelineResult,
    generate_strategy_only,
    run_pipeline,
)
from driftpin.config.settings import driftpin_dir
from driftpin.ingestion.extractor import extract_requirements
from driftpin.ingestion.parsers import parse_document
from driftpin.ingestion.registry import RequirementRegistry, compute_doc_hash
from driftpin.ledger.ledger import RunLedger
from driftpin.providers.base import LLMProvider
from driftpin.render.excel import save_excel_workbook
from driftpin.render.headers import build_header
from driftpin.render.markdown import render_markdown_report
from driftpin.schemas.requirements import Requirement
from driftpin.schemas.strategy import TestStrategy

_SLUG_SANITIZE_PATTERN = re.compile(r"[^a-z0-9]+")
_MAX_SOURCE_NAMES_IN_SLUG = 2


class DocumentNotFoundError(Exception):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"Document not found: {path}")


class EmptyRegistryError(Exception):
    """Raised when a generation action is attempted against an empty requirement registry."""


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _sanitize_slug(text: str) -> str:
    slug = _SLUG_SANITIZE_PATTERN.sub("-", text.lower()).strip("-")
    return slug or "doc"


def derive_source_slug(requirements: list[Requirement]) -> str | None:
    """Best-effort short label for the source document(s) behind a set of
    requirements, for use in generated-artifact filenames. Returns None for
    an empty requirement list; falls back to "multi-source" when the
    requirements span more documents than reasonably fit in a filename."""
    distinct_stems: list[str] = []
    seen: set[str] = set()
    for requirement in requirements:
        stem = Path(requirement.source_doc_path).stem
        if stem not in seen:
            seen.add(stem)
            distinct_stems.append(stem)

    if not distinct_stems:
        return None
    if len(distinct_stems) > _MAX_SOURCE_NAMES_IN_SLUG:
        return "multi-source"
    return "-".join(_sanitize_slug(stem) for stem in distinct_stems)


def artifact_filename(prefix: str, extension: str, source_slug: str | None = None) -> str:
    """Every generated file is named `<Prefix>_<source-slug>_<timestamp>.<ext>`
    (e.g. `Strategy_prd-1-voice-assistant-fab_20260705-121805.json`) — the
    artifact type and the PRD it came from are both readable at a glance, and
    files sort chronologically. The run ID isn't in the filename; it's still
    recoverable from the artifact's own embedded content (the header on
    Excel/Markdown reports, the `strategy_id`/`suite_id` fields in a strategy
    or cases file) for cross-referencing against the ledger."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    title_prefix = prefix.capitalize()
    if source_slug:
        return f"{title_prefix}_{source_slug}_{timestamp}.{extension}"
    return f"{title_prefix}_{timestamp}.{extension}"


def open_registry(project_root: Path) -> RequirementRegistry:
    return RequirementRegistry(driftpin_dir(project_root) / "requirements.json")


def require_nonempty_registry(registry: RequirementRegistry) -> None:
    if not registry.requirements:
        raise EmptyRegistryError(
            "The requirement registry is empty. Run ingestion against a PRD first."
        )


@dataclass
class IngestOutcome:
    doc_path: Path
    added_count: int
    ambiguity_count: int


@dataclass
class IngestRunResult:
    run_id: str
    ledger: RunLedger
    outcomes: list[IngestOutcome] = field(default_factory=list)


async def run_ingest(
    provider: LLMProvider,
    project_root: Path,
    doc_paths: list[Path],
    run_id: str | None = None,
) -> IngestRunResult:
    run_id = run_id or new_run_id()
    ledger = RunLedger(driftpin_dir(project_root), run_id=run_id)
    registry = open_registry(project_root)

    outcomes: list[IngestOutcome] = []
    for doc_path in doc_paths:
        if not doc_path.exists():
            raise DocumentNotFoundError(doc_path)

        blocks = parse_document(doc_path)
        doc_hash = compute_doc_hash(doc_path)
        extraction = await extract_requirements(provider, blocks, ledger=ledger)
        added = registry.ingest(
            extraction, source_doc_path=str(doc_path), source_doc_hash=doc_hash
        )

        for ambiguity in extraction.ambiguities:
            ledger.record_assumption(
                heading=f"{doc_path.name}: {ambiguity.description[:80]}",
                detail=f"Source span: {ambiguity.source_span}",
            )

        outcomes.append(
            IngestOutcome(
                doc_path=doc_path,
                added_count=len(added),
                ambiguity_count=len(extraction.ambiguities),
            )
        )

    registry.save()
    return IngestRunResult(run_id=run_id, ledger=ledger, outcomes=outcomes)


@dataclass
class StrategyRunResult:
    run_id: str
    ledger: RunLedger
    strategy: TestStrategy


async def run_generate_strategy(
    provider: LLMProvider,
    project_root: Path,
    run_id: str | None = None,
    on_stage: OnStage | None = None,
) -> StrategyRunResult:
    registry = open_registry(project_root)
    require_nonempty_registry(registry)

    run_id = run_id or new_run_id()
    ledger = RunLedger(driftpin_dir(project_root), run_id=run_id)
    strategy = await generate_strategy_only(
        provider, registry.requirements, run_id=run_id, ledger=ledger, on_stage=on_stage
    )
    return StrategyRunResult(run_id=run_id, ledger=ledger, strategy=strategy)


@dataclass
class CasesRunResult:
    run_id: str
    ledger: RunLedger
    result: PipelineResult
    excel_path: Path
    markdown_path: Path


async def run_generate_cases(
    provider: LLMProvider,
    project_root: Path,
    out_dir: Path,
    run_id: str | None = None,
    on_stage: OnStage | None = None,
) -> CasesRunResult:
    registry = open_registry(project_root)
    require_nonempty_registry(registry)

    run_id = run_id or new_run_id()
    ledger = RunLedger(driftpin_dir(project_root), run_id=run_id)
    pipeline_result = await run_pipeline(
        provider, registry.requirements, run_id=run_id, ledger=ledger, on_stage=on_stage
    )

    header = build_header(
        run_id=run_id,
        requirements=registry.requirements,
        registry_version=registry.registry_version,
    )
    source_slug = derive_source_slug(registry.requirements)
    out_dir.mkdir(parents=True, exist_ok=True)
    excel_path = out_dir / artifact_filename("cases", "xlsx", source_slug=source_slug)
    markdown_path = out_dir / artifact_filename("cases", "md", source_slug=source_slug)
    save_excel_workbook(pipeline_result, header, excel_path)
    markdown_path.write_text(render_markdown_report(pipeline_result, header), encoding="utf-8")

    return CasesRunResult(
        run_id=run_id,
        ledger=ledger,
        result=pipeline_result,
        excel_path=excel_path,
        markdown_path=markdown_path,
    )
