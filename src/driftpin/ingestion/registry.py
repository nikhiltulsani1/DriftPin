"""Persistent requirement registry.

The registry is the only component permitted to mint requirement IDs. IDs are
derived deterministically from the source document hash and the requirement's
verbatim source span, so re-ingesting an unchanged document produces identical
IDs on every run. The extracting LLM never assigns IDs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from driftpin.ingestion.text_utils import normalize_whitespace
from driftpin.schemas.requirements import (
    AcceptanceCriterion,
    CandidateNFR,
    ExtractionResult,
    NfrScope,
    NonFunctionalRequirement,
    RegistryFile,
    Requirement,
)

_ID_LENGTH = 8
_CURRENT_REGISTRY_VERSION = 2  # acceptance_criteria[] and nfrs[] introduced at this version


def compute_doc_hash(path: Path) -> str:
    """SHA-256 of a source document's raw bytes, used to detect content changes."""
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def derive_requirement_id(source_doc_hash: str, source_span: str) -> str:
    """Stable requirement ID: content-addressed, independent of extraction order."""
    basis = f"{source_doc_hash}:{normalize_whitespace(source_span)}"
    fingerprint = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_ID_LENGTH]
    return f"R-{fingerprint}"


def derive_ac_id(requirement_id: str, ac_text: str) -> str:
    """Stable AC ID: scoped under its parent requirement_id (already stable),
    so re-ingesting an unchanged document produces identical AC IDs too."""
    basis = f"{requirement_id}:{normalize_whitespace(ac_text)}"
    fingerprint = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_ID_LENGTH]
    return f"AC-{fingerprint}"


def derive_nfr_id(source_doc_hash: str, nfr_text: str) -> str:
    """Stable NFR ID: content-addressed the same way requirement_id is."""
    basis = f"{source_doc_hash}:NFR:{normalize_whitespace(nfr_text)}"
    fingerprint = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_ID_LENGTH]
    return f"NFR-{fingerprint}"


class RequirementRegistry:
    """Loads, updates, and persists the requirement registry at a fixed path."""

    def __init__(self, registry_path: Path) -> None:
        self._path = registry_path
        self._file = self._load()

    def _load(self) -> RegistryFile:
        if self._path.exists():
            return RegistryFile.model_validate_json(self._path.read_text(encoding="utf-8"))
        return RegistryFile()

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            self._file.model_dump_json(indent=2, exclude_none=False), encoding="utf-8"
        )

    @property
    def requirements(self) -> list[Requirement]:
        return list(self._file.requirements)

    @property
    def registry_version(self) -> int:
        return self._file.registry_version

    @property
    def nfrs(self) -> list[NonFunctionalRequirement]:
        return list(self._file.nfrs)

    @property
    def unassigned_acs(self) -> list[str]:
        return list(self._file.unassigned_acs)

    def get(self, requirement_id: str) -> Requirement | None:
        for requirement in self._file.requirements:
            if requirement.requirement_id == requirement_id:
                return requirement
        return None

    def ingest(
        self,
        extraction: ExtractionResult,
        source_doc_path: str,
        source_doc_hash: str,
    ) -> list[Requirement]:
        """Assigns deterministic IDs to candidate requirements and merges them in.

        A candidate whose derived ID already exists in the registry is treated
        as unchanged (its title/description/source_span/risk_tier are left
        alone, idempotent re-ingestion) — but if it previously had no
        acceptance criteria and the candidate now supplies some, those are
        still populated onto the existing requirement, so re-ingesting a doc
        against an older registry backfills the new AC fields rather than
        requiring a full reset. New candidates are appended. Returns the
        requirements added or already present for this ingestion call, in
        candidate order.
        """
        existing_ids = {r.requirement_id for r in self._file.requirements}
        result: list[Requirement] = []

        for candidate in extraction.candidate_requirements:
            requirement_id = derive_requirement_id(source_doc_hash, candidate.source_span)
            acceptance_criteria = [
                AcceptanceCriterion(ac_id=derive_ac_id(requirement_id, text), text=text)
                for text in candidate.acceptance_criteria
            ]

            if requirement_id in existing_ids:
                existing = self.get(requirement_id)
                assert existing is not None
                if not existing.acceptance_criteria and acceptance_criteria:
                    existing.acceptance_criteria = acceptance_criteria
                existing.ac_extraction_failed = candidate.ac_extraction_failed
                result.append(existing)
                continue

            requirement = Requirement(
                requirement_id=requirement_id,
                title=candidate.title,
                description=candidate.description,
                source_span=candidate.source_span,
                source_doc_path=source_doc_path,
                source_doc_hash=source_doc_hash,
                risk_tier=candidate.risk_tier,
                acceptance_criteria=acceptance_criteria,
                ac_extraction_failed=candidate.ac_extraction_failed,
            )
            self._file.requirements.append(requirement)
            existing_ids.add(requirement_id)
            result.append(requirement)

        self._ingest_nfrs(extraction.candidate_nfrs, source_doc_hash, source_doc_path)
        self._ingest_unassigned_acs(extraction.unassigned_acs)
        self._file.registry_version = max(self._file.registry_version, _CURRENT_REGISTRY_VERSION)

        return result

    def _ingest_unassigned_acs(self, unassigned_acs: list[str]) -> None:
        """Acceptance criteria the parser/fallback found but couldn't link
        to any requirement — never silently dropped, surfaced here (and, by
        the caller, in ASSUMPTIONS.md) so a human can resolve the linkage
        by hand."""
        existing = set(self._file.unassigned_acs)
        for text in unassigned_acs:
            if text not in existing:
                self._file.unassigned_acs.append(text)
                existing.add(text)

    def _ingest_nfrs(
        self,
        candidate_nfrs: list[CandidateNFR],
        source_doc_hash: str,
        source_doc_path: str,
    ) -> None:
        """Global NFRs are stored once and treated as applicable to every
        requirement at review-input-build time (see
        `orchestrator._resolve_requirement_nfrs`), never duplicated into each
        requirement's own record. Scoped NFRs link via `nfr_ids`, resolved
        against requirement IDs the same content-addressed way requirement
        IDs themselves are derived — a scoped NFR whose source span doesn't
        exactly match a requirement's own span simply links to nothing
        rather than guessing which requirement it meant.
        """
        existing_nfr_ids = {n.nfr_id for n in self._file.nfrs}
        requirements_by_id = {r.requirement_id: r for r in self._file.requirements}

        for candidate in candidate_nfrs:
            nfr_id = derive_nfr_id(source_doc_hash, candidate.text)
            if nfr_id not in existing_nfr_ids:
                self._file.nfrs.append(
                    NonFunctionalRequirement(
                        nfr_id=nfr_id,
                        text=candidate.text,
                        scope=candidate.scope,
                        source_doc_path=source_doc_path,
                        source_doc_hash=source_doc_hash,
                    )
                )
                existing_nfr_ids.add(nfr_id)

            if candidate.scope == NfrScope.SCOPED:
                for span in candidate.applies_to_source_spans:
                    target_id = derive_requirement_id(source_doc_hash, span)
                    target = requirements_by_id.get(target_id)
                    if target is not None and nfr_id not in target.nfr_ids:
                        target.nfr_ids.append(nfr_id)
