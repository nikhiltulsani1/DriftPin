from __future__ import annotations

from pathlib import Path

from driftpin.ingestion.registry import (
    RequirementRegistry,
    derive_ac_id,
    derive_nfr_id,
    derive_requirement_id,
)
from driftpin.schemas.requirements import (
    CandidateNFR,
    CandidateRequirement,
    ExtractionResult,
    NfrScope,
    RiskTier,
)


def _candidate(
    span: str, title: str = "A requirement", acceptance_criteria: list[str] | None = None
) -> CandidateRequirement:
    return CandidateRequirement(
        title=title,
        description="Description text.",
        source_span=span,
        risk_tier=RiskTier.MEDIUM,
        acceptance_criteria=acceptance_criteria or [],
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


def test_derive_ac_id_is_deterministic_and_scoped_to_requirement() -> None:
    first = derive_ac_id("R-abc123", "If no match is found, saves as a note.")
    second = derive_ac_id("R-abc123", "If no match is found, saves as a note.")
    assert first == second
    assert first.startswith("AC-")

    # Identical AC text under a different parent requirement must not collide.
    under_other_requirement = derive_ac_id("R-def456", "If no match is found, saves as a note.")
    assert under_other_requirement != first


def test_ingest_assigns_ac_ids_and_persists(tmp_path: Path) -> None:
    registry_path = tmp_path / "requirements.json"
    registry = RequirementRegistry(registry_path)

    extraction = ExtractionResult(
        candidate_requirements=[
            _candidate(
                "Schedule matching span.",
                acceptance_criteria=["If no match, saves as a note instead."],
            )
        ]
    )
    added = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")
    registry.save()

    assert len(added[0].acceptance_criteria) == 1
    assert added[0].acceptance_criteria[0].ac_id.startswith("AC-")
    assert added[0].acceptance_criteria[0].text == "If no match, saves as a note instead."

    reloaded = RequirementRegistry(registry_path)
    assert reloaded.requirements[0].acceptance_criteria[0].ac_id == added[0].acceptance_criteria[0].ac_id


def test_reingesting_unchanged_document_produces_identical_ac_ids(tmp_path: Path) -> None:
    registry_path = tmp_path / "requirements.json"
    registry = RequirementRegistry(registry_path)

    extraction = ExtractionResult(
        candidate_requirements=[
            _candidate("Same requirement text.", acceptance_criteria=["Same AC text."])
        ]
    )
    first_pass = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")
    second_pass = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")

    assert len(registry.requirements) == 1
    assert (
        first_pass[0].acceptance_criteria[0].ac_id == second_pass[0].acceptance_criteria[0].ac_id
    )


def test_reingesting_backfills_acceptance_criteria_onto_existing_requirement(tmp_path: Path) -> None:
    """A requirement previously ingested with no ACs (older registry) gets
    its acceptance criteria populated on re-ingestion against an extraction
    that now includes them, without changing its requirement_id."""
    registry_path = tmp_path / "requirements.json"
    registry = RequirementRegistry(registry_path)

    bare_extraction = ExtractionResult(
        candidate_requirements=[_candidate("Same requirement text.")]
    )
    first_pass = registry.ingest(bare_extraction, source_doc_path="prd.md", source_doc_hash="hash-a")
    assert first_pass[0].acceptance_criteria == []

    enriched_extraction = ExtractionResult(
        candidate_requirements=[
            _candidate("Same requirement text.", acceptance_criteria=["Newly extracted AC."])
        ]
    )
    second_pass = registry.ingest(enriched_extraction, source_doc_path="prd.md", source_doc_hash="hash-a")

    assert second_pass[0].requirement_id == first_pass[0].requirement_id
    assert len(second_pass[0].acceptance_criteria) == 1
    assert second_pass[0].acceptance_criteria[0].text == "Newly extracted AC."
    assert len(registry.requirements) == 1


def test_derive_nfr_id_is_deterministic_and_differs_across_documents() -> None:
    first = derive_nfr_id("hash-a", "Response within 3 seconds end-to-end.")
    second = derive_nfr_id("hash-a", "Response within 3 seconds end-to-end.")
    assert first == second
    assert first.startswith("NFR-")

    other_doc = derive_nfr_id("hash-b", "Response within 3 seconds end-to-end.")
    assert other_doc != first


def test_ingest_global_nfr_stored_once_and_not_duplicated_per_requirement(tmp_path: Path) -> None:
    registry = RequirementRegistry(tmp_path / "requirements.json")
    extraction = ExtractionResult(
        candidate_requirements=[_candidate("Req A span."), _candidate("Req B span.", title="Req B")],
        candidate_nfrs=[
            CandidateNFR(text="Response within 3 seconds end-to-end.", scope=NfrScope.GLOBAL)
        ],
    )

    added = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")

    assert len(registry.nfrs) == 1
    assert registry.nfrs[0].scope == NfrScope.GLOBAL
    # Global NFR is not written onto either requirement's own nfr_ids list —
    # it's resolved as universally applicable at review-input-build time.
    assert added[0].nfr_ids == []
    assert added[1].nfr_ids == []


def test_ingest_scoped_nfr_links_to_matching_requirement_via_source_span(tmp_path: Path) -> None:
    registry = RequirementRegistry(tmp_path / "requirements.json")
    extraction = ExtractionResult(
        candidate_requirements=[
            _candidate("Req A span.", title="Req A"),
            _candidate("Req B span.", title="Req B"),
        ],
        candidate_nfrs=[
            CandidateNFR(
                text="Req A's endpoint must respond within 500ms.",
                scope=NfrScope.SCOPED,
                applies_to_source_spans=["Req A span."],
            )
        ],
    )

    added = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")
    req_a = next(r for r in added if r.title == "Req A")
    req_b = next(r for r in added if r.title == "Req B")

    assert len(registry.nfrs) == 1
    assert req_a.nfr_ids == [registry.nfrs[0].nfr_id]
    assert req_b.nfr_ids == []


def test_ingest_scoped_nfr_with_unmatched_span_links_to_nothing(tmp_path: Path) -> None:
    """A scoped NFR whose applies_to_source_spans doesn't exactly match any
    requirement's own source_span simply links to nothing — the registry
    never guesses which requirement it meant."""
    registry = RequirementRegistry(tmp_path / "requirements.json")
    extraction = ExtractionResult(
        candidate_requirements=[_candidate("Req A span.", title="Req A")],
        candidate_nfrs=[
            CandidateNFR(
                text="Some scoped constraint.",
                scope=NfrScope.SCOPED,
                applies_to_source_spans=["This span matches nothing in the document."],
            )
        ],
    )

    added = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")

    assert len(registry.nfrs) == 1
    assert added[0].nfr_ids == []


def test_ingest_bumps_registry_version_on_an_older_registry(tmp_path: Path) -> None:
    """An existing registry from before ACs/NFRs existed (version 1) gets
    bumped forward on ingest, so a re-ingested registry file reflects that
    the new schema fields are actually populated, not just structurally
    present with empty defaults."""
    registry_path = tmp_path / "requirements.json"
    registry_path.write_text('{"registry_version": 1, "requirements": [], "nfrs": []}', encoding="utf-8")
    registry = RequirementRegistry(registry_path)
    assert registry.registry_version == 1

    registry.ingest(
        ExtractionResult(candidate_requirements=[_candidate("New requirement.")]),
        source_doc_path="prd.md",
        source_doc_hash="hash-a",
    )

    assert registry.registry_version >= 2


def test_reingesting_unchanged_document_produces_identical_nfr_ids_and_links(tmp_path: Path) -> None:
    registry_path = tmp_path / "requirements.json"
    registry = RequirementRegistry(registry_path)
    extraction = ExtractionResult(
        candidate_requirements=[_candidate("Req A span.", title="Req A")],
        candidate_nfrs=[
            CandidateNFR(
                text="Req A's endpoint must respond within 500ms.",
                scope=NfrScope.SCOPED,
                applies_to_source_spans=["Req A span."],
            )
        ],
    )

    first_pass = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")
    first_nfr_id = registry.nfrs[0].nfr_id
    second_pass = registry.ingest(extraction, source_doc_path="prd.md", source_doc_hash="hash-a")

    assert len(registry.nfrs) == 1  # not duplicated on re-ingestion
    assert registry.nfrs[0].nfr_id == first_nfr_id
    assert first_pass[0].nfr_ids == second_pass[0].nfr_ids == [first_nfr_id]
