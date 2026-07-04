from __future__ import annotations

from pathlib import Path

from driftpin.ingestion.registry import RequirementRegistry, derive_requirement_id
from driftpin.schemas.requirements import ExtractionResult, Requirement, RiskTier


def _candidate(span: str, title: str = "A requirement") -> Requirement:
    return Requirement(
        requirement_id="placeholder",
        title=title,
        description="Description text.",
        source_span=span,
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
        risk_tier=RiskTier.MEDIUM,
    )


def test_derive_requirement_id_is_deterministic() -> None:
    first = derive_requirement_id("hash-a", "The system shall log out idle users.")
    second = derive_requirement_id("hash-a", "The system shall log out idle users.")
    assert first == second
    assert first.startswith("R-")


def test_derive_requirement_id_ignores_whitespace_and_case_noise() -> None:
    first = derive_requirement_id("hash-a", "The system SHALL log out idle users.")
    second = derive_requirement_id("hash-a", "  the system shall   log out idle users.  ")
    assert first == second


def test_derive_requirement_id_differs_across_documents() -> None:
    first = derive_requirement_id("hash-a", "Same text.")
    second = derive_requirement_id("hash-b", "Same text.")
    assert first != second


def test_ingest_assigns_stable_ids_and_persists(tmp_path: Path) -> None:
    registry_path = tmp_path / "requirements.json"
    registry = RequirementRegistry(registry_path)

    extraction = ExtractionResult(
        candidate_requirements=[
            _candidate("Users must reset passwords via email."),
            _candidate("Sessions expire after 30 minutes of inactivity."),
        ]
    )
    added = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")
    registry.save()

    assert len(added) == 2
    assert all(r.requirement_id.startswith("R-") for r in added)

    reloaded = RequirementRegistry(registry_path)
    assert len(reloaded.requirements) == 2
    assert {r.requirement_id for r in reloaded.requirements} == {r.requirement_id for r in added}


def test_reingesting_unchanged_document_is_idempotent(tmp_path: Path) -> None:
    registry_path = tmp_path / "requirements.json"
    registry = RequirementRegistry(registry_path)

    extraction = ExtractionResult(candidate_requirements=[_candidate("Same requirement text.")])
    first_pass = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")
    second_pass = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")

    assert len(registry.requirements) == 1
    assert first_pass[0].requirement_id == second_pass[0].requirement_id


def test_get_returns_none_for_unknown_id(tmp_path: Path) -> None:
    registry = RequirementRegistry(tmp_path / "requirements.json")
    assert registry.get("R-doesnotexist") is None
