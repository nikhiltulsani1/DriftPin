from __future__ import annotations

import pytest
from pydantic import ValidationError

from driftpin.schemas.review import FindingSeverity, ReviewerFinding, ReviewReport


def _finding(**overrides: object) -> ReviewerFinding:
    defaults: dict[str, object] = dict(
        severity=FindingSeverity.MINOR,
        subject_id="S-1",
        description="Coverage gap on a medium-risk requirement.",
    )
    defaults.update(overrides)
    return ReviewerFinding(**defaults)  # type: ignore[arg-type]


def test_reviewer_finding_accepts_valid_payload() -> None:
    finding = _finding()
    assert finding.severity == FindingSeverity.MINOR


@pytest.mark.parametrize("field", ["subject_id", "description"])
def test_reviewer_finding_rejects_empty_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        _finding(**{field: ""})


def test_review_report_rejects_empty_summary() -> None:
    with pytest.raises(ValidationError):
        ReviewReport(
            review_id="review-1",
            target_run_id="run-1",
            findings=[],
            passed=True,
            summary="",
        )


def test_review_report_accepts_valid_payload() -> None:
    report = ReviewReport(
        review_id="review-1",
        target_run_id="run-1",
        findings=[_finding()],
        passed=False,
        summary="One coverage gap found.",
    )
    assert report.passed is False
