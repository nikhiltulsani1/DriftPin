"""Markdown renderer: a human-readable report with the traceability matrix as
a first-class section, not an afterthought.

Requirement IDs are shown using the simple `Req-N` display labels built by
`render/labels.py`, not the registry's real content-addressed IDs — a human
scoring this report needs something readable, not a hash.
"""

from __future__ import annotations

from driftpin.agents.orchestrator import PipelineResult
from driftpin.render.headers import ArtifactHeader
from driftpin.render.labels import (
    build_requirement_labels,
    label_for,
    labels_for,
    substitute_labels_in_text,
)
from driftpin.schemas.strategy import Scenario, TestStrategy


def _header_section(header: ArtifactHeader) -> str:
    lines = [
        "# Driftpin Test Report",
        "",
        f"- Generator: {header.generator} v{header.generator_version}",
        f"- Run ID: {header.run_id}",
        f"- Registry version: {header.registry_version}",
        f"- Source document(s): {', '.join(header.source_doc_titles) or '(none)'}",
        f"- Source document hash (for verification): {', '.join(header.source_doc_hashes) or '(none)'}",
        "",
    ]
    return "\n".join(lines)


def _generation_failures_section(result: PipelineResult, labels: dict[str, str]) -> str:
    """Only produces content when a scenario's fill call came back empty on
    every retry — surfaced prominently, right after the header, rather than
    left to look like an ordinary zero-coverage row in the traceability
    matrix below. A silent zero and a `GENERATION_FAILED` scenario need to
    read differently to a human scoring the report."""
    if not result.failed_scenario_ids:
        return ""

    scenarios_by_id = {s.scenario_id: s for s in result.strategy.scenarios}
    lines = [
        "## Generation Failures",
        "",
        "The following scenarios could not be filled with test cases after retries "
        "and require human attention — see `ASSUMPTIONS.md` for this run.",
        "",
    ]
    for scenario_id in result.failed_scenario_ids:
        scenario = scenarios_by_id.get(scenario_id)
        title = substitute_labels_in_text(scenario.title, labels) if scenario else "(unknown scenario)"
        lines.append(f"- **GENERATION_FAILED** — {scenario_id}: {title}")
    lines.append("")
    return "\n".join(lines)


def _traceability_section(result: PipelineResult, labels: dict[str, str]) -> str:
    lines = [
        "## Traceability Matrix",
        "",
        "| Requirement | Title | Risk Tier | Coverage | Case IDs |",
        "|---|---|---|---|---|",
    ]
    for row in result.traceability:
        case_ids = ", ".join(row.case_ids) if row.case_ids else "(none)"
        lines.append(
            f"| {label_for(row.requirement_id, labels)} | {row.requirement_title} | "
            f"{row.risk_tier} | {row.coverage_count} | {case_ids} |"
        )
    lines.append("")
    return "\n".join(lines)


def _scenarios_section(scenarios: list[Scenario], labels: dict[str, str]) -> str:
    lines = ["## Scenarios", ""]
    for scenario in scenarios:
        lines.append(f"### {scenario.scenario_id}: {substitute_labels_in_text(scenario.title, labels)}")
        lines.append(f"- Requirements: {', '.join(labels_for(scenario.requirement_ids, labels))}")
        lines.append(f"- Owning agent: {scenario.owning_agent.value}")
        lines.append(f"- Risk tier: {scenario.risk_tier.value}")
        lines.append(
            f"- Execution recommendation: {scenario.execution_recommendation.value} "
            f"— {substitute_labels_in_text(scenario.recommendation_justification, labels)}"
        )
        lines.append("")
    return "\n".join(lines)


def _test_cases_section(result: PipelineResult, labels: dict[str, str]) -> str:
    lines = ["## Test Cases", ""]
    for case in result.suite.cases:
        lines.append(f"### {case.case_id}: {substitute_labels_in_text(case.title, labels)}")
        lines.append(f"- Scenario: {case.scenario_id}")
        lines.append(f"- Requirements: {', '.join(labels_for(case.requirement_ids, labels))}")
        if case.preconditions:
            lines.append(f"- Preconditions: {substitute_labels_in_text(case.preconditions, labels)}")
        for step in case.steps:
            action = substitute_labels_in_text(step.action, labels)
            expected = substitute_labels_in_text(step.expected_result, labels)
            lines.append(f"{step.step_number}. {action} → {expected}")
        if case.assumptions:
            lines.append("- Assumptions:")
            for assumption in case.assumptions:
                lines.append(f"  - {substitute_labels_in_text(assumption, labels)}")
        lines.append("")
    return "\n".join(lines)


def _review_section(result: PipelineResult, labels: dict[str, str]) -> str:
    lines = [
        "## Review",
        "",
        f"- Passed: {result.review.passed}",
        f"- Summary: {substitute_labels_in_text(result.review.summary, labels)}",
        "",
    ]
    if result.review.findings:
        lines.append("### Findings")
        lines.append("")
        for finding in result.review.findings:
            requirement_labels = (
                ", ".join(labels_for(finding.requirement_ids, labels))
                if finding.requirement_ids
                else "(none)"
            )
            description = substitute_labels_in_text(finding.description, labels)
            quote_suffix = (
                f' — quote: "{substitute_labels_in_text(finding.requirement_quote, labels)}"'
                if finding.requirement_quote
                else ""
            )
            lines.append(
                f"- **[{finding.severity.value}]** {finding.subject_id}: {description} "
                f"(requirements: {requirement_labels}){quote_suffix}"
            )
        lines.append("")
    return "\n".join(lines)


def render_markdown_report(result: PipelineResult, header: ArtifactHeader) -> str:
    labels = build_requirement_labels([row.requirement_id for row in result.traceability])
    sections = [
        _header_section(header),
        _generation_failures_section(result, labels),
        _traceability_section(result, labels),
        _scenarios_section(result.strategy.scenarios, labels),
        _test_cases_section(result, labels),
        _review_section(result, labels),
    ]
    return "\n".join(section for section in sections if section)


def render_strategy_markdown(strategy: TestStrategy, header: ArtifactHeader) -> str:
    """Human-readable report for `generate strategy` — scenarios only, no
    test cases or review, since that stage hasn't run yet. Kept separate from
    `render_markdown_report` rather than passing a partially-populated
    `PipelineResult`, since a strategy-only run has no suite or review to
    fake placeholder values for."""
    labels = build_requirement_labels([rid for s in strategy.scenarios for rid in s.requirement_ids])
    sections = [
        _header_section(header),
        _scenarios_section(strategy.scenarios, labels),
    ]
    return "\n".join(sections)
