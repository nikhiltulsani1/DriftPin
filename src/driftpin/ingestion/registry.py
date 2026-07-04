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
    ExtractionResult,
    RegistryFile,
    Requirement,
)

_ID_LENGTH = 8


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
        as unchanged and left alone (idempotent re-ingestion). New candidates
        are appended. Returns the requirements added or already present for
        this ingestion call, in candidate order.
        """
        existing_ids = {r.requirement_id for r in self._file.requirements}
        result: list[Requirement] = []

        for candidate in extraction.candidate_requirements:
            requirement_id = derive_requirement_id(source_doc_hash, candidate.source_span)
            if requirement_id in existing_ids:
                result.append(self.get(requirement_id))  # type: ignore[arg-type]
                continue

            requirement = Requirement(
                requirement_id=requirement_id,
                title=candidate.title,
                description=candidate.description,
                source_span=candidate.source_span,
                source_doc_path=source_doc_path,
                source_doc_hash=source_doc_hash,
                risk_tier=candidate.risk_tier,
            )
            self._file.requirements.append(requirement)
            existing_ids.add(requirement_id)
            result.append(requirement)

        return result
