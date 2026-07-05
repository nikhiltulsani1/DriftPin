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


def _scenarios_section(result: PipelineResult, labels: dict[str, str]) -> str:
    lines = ["## Scenarios", ""]
    for scenario in result.strategy.scenarios:
        lines.append(f"### {scenario.scenario_id}: {scenario.title}")
        lines.append(f"- Requirements: {', '.join(labels_for(scenario.requirement_ids, labels))}")
        lines.append(f"- Owning agent: {scenario.owning_agent.value}")
        lines.append(f"- Risk tier: {scenario.risk_tier.value}")
        lines.append(
            f"- Execution recommendation: {scenario.execution_recommendation.value} "
            f"— {scenario.recommendation_justification}"
        )
        lines.append("")
    return "\n".join(lines)


def _test_cases_section(result: PipelineResult, labels: dict[str, str]) -> str:
    lines = ["## Test Cases", ""]
    for case in result.suite.cases:
        lines.append(f"### {case.case_id}: {case.title}")
        lines.append(f"- Scenario: {case.scenario_id}")
        lines.append(f"- Requirements: {', '.join(labels_for(case.requirement_ids, labels))}")
        if case.preconditions:
            lines.append(f"- Preconditions: {case.preconditions}")
        for step in case.steps:
            lines.append(f"{step.step_number}. {step.action} → {step.expected_result}")
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
            lines.append(
                f"- **[{finding.severity.value}]** {finding.subject_id}: {finding.description} "
                f"(requirements: {requirement_labels})"
            )
        lines.append("")
    return "\n".join(lines)


def render_markdown_report(result: PipelineResult, header: ArtifactHeader) -> str:
    labels = build_requirement_labels([row.requirement_id for row in result.traceability])
    sections = [
        _header_section(header),
        _traceability_section(result, labels),
        _scenarios_section(result, labels),
        _test_cases_section(result, labels),
        _review_section(result, labels),
    ]
    return "\n".join(sections)
