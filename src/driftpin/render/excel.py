"""Excel renderer: the traceability matrix ships as a first-class sheet,
not a footnote to the test cases."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from driftpin.agents.orchestrator import PipelineResult
from driftpin.render.headers import ArtifactHeader


def _write_header_sheet(ws: Worksheet, header: ArtifactHeader) -> None:
    ws.title = "Header"
    ws.append(["Field", "Value"])
    ws.append(["Generator", header.generator])
    ws.append(["Generator version", header.generator_version])
    ws.append(["Run ID", header.run_id])
    ws.append(["Registry version", header.registry_version])
    ws.append(["Source document hashes", ", ".join(header.source_doc_hashes)])


def _write_traceability_sheet(ws: Worksheet, result: PipelineResult) -> None:
    ws.append(["Requirement ID", "Title", "Risk Tier", "Coverage Count", "Case IDs"])
    for row in result.traceability:
        ws.append(
            [
                row.requirement_id,
                row.requirement_title,
                row.risk_tier,
                row.coverage_count,
                ", ".join(row.case_ids),
            ]
        )


def _write_scenarios_sheet(ws: Worksheet, result: PipelineResult) -> None:
    ws.append(
        [
            "Scenario ID",
            "Title",
            "Requirement IDs",
            "Owning Agent",
            "Risk Tier",
            "Execution Recommendation",
            "Justification",
        ]
    )
    for scenario in result.strategy.scenarios:
        ws.append(
            [
                scenario.scenario_id,
                scenario.title,
                ", ".join(scenario.requirement_ids),
                scenario.owning_agent.value,
                scenario.risk_tier.value,
                scenario.execution_recommendation.value,
                scenario.recommendation_justification,
            ]
        )


def _write_test_cases_sheet(ws: Worksheet, result: PipelineResult) -> None:
    ws.append(
        [
            "Case ID",
            "Scenario ID",
            "Requirement IDs",
            "Title",
            "Preconditions",
            "Steps",
            "Owning Agent",
            "Execution Recommendation",
        ]
    )
    for case in result.suite.cases:
        steps_text = "; ".join(f"{s.step_number}. {s.action} -> {s.expected_result}" for s in case.steps)
        ws.append(
            [
                case.case_id,
                case.scenario_id,
                ", ".join(case.requirement_ids),
                case.title,
                case.preconditions,
                steps_text,
                case.owning_agent.value,
                case.execution_recommendation.value,
            ]
        )


def _write_review_sheet(ws: Worksheet, result: PipelineResult) -> None:
    ws.append(["Passed", result.review.passed])
    ws.append(["Summary", result.review.summary])
    ws.append([])
    ws.append(["Severity", "Subject ID", "Description", "Requirement IDs"])
    for finding in result.review.findings:
        ws.append(
            [
                finding.severity.value,
                finding.subject_id,
                finding.description,
                ", ".join(finding.requirement_ids),
            ]
        )


def build_excel_workbook(result: PipelineResult, header: ArtifactHeader) -> Workbook:
    workbook = Workbook()
    header_sheet = workbook.active
    assert header_sheet is not None
    _write_header_sheet(header_sheet, header)

    _write_traceability_sheet(workbook.create_sheet("Traceability Matrix"), result)
    _write_scenarios_sheet(workbook.create_sheet("Scenarios"), result)
    _write_test_cases_sheet(workbook.create_sheet("Test Cases"), result)
    _write_review_sheet(workbook.create_sheet("Review"), result)

    return workbook


def save_excel_workbook(result: PipelineResult, header: ArtifactHeader, output_path: Path) -> None:
    workbook = build_excel_workbook(result, header)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(output_path))
