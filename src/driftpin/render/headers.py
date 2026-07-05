"""Provenance header stamped onto every generated artifact.

Every renderer output carries the generator version, run ID, registry
version, and the source document(s) behind the requirements it covers — so
a reader can always trace an artifact back to exactly what produced it.
Source documents are shown by their actual filename first (what a human
recognizes), with the SHA-256 hash also included — the master plan requires
the hash specifically, for provenance/verification, not as the primary
label a reader has to parse.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from driftpin import __version__
from driftpin.schemas.requirements import Requirement


class ArtifactHeader(BaseModel):
    generator: str = "driftpin"
    generator_version: str
    run_id: str
    registry_version: int
    source_doc_titles: list[str]
    source_doc_hashes: list[str]


def build_header(
    run_id: str, requirements: list[Requirement], registry_version: int
) -> ArtifactHeader:
    unique_titles = sorted({Path(r.source_doc_path).name for r in requirements})
    unique_hashes = sorted({r.source_doc_hash for r in requirements})
    return ArtifactHeader(
        generator_version=__version__,
        run_id=run_id,
        registry_version=registry_version,
        source_doc_titles=unique_titles,
        source_doc_hashes=unique_hashes,
    )
