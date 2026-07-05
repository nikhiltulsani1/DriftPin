"""Human-facing display labels for requirement IDs.

The registry's real requirement IDs are content-addressed hashes
(`R-c44071f0`) by design — that's what makes re-ingestion idempotent and lets
the extraction guard rail tell a real ID apart from a model inventing one
that merely looks plausible (see `ingestion/registry.py` and
`DESIGN_DECISIONS.md`). That property is exactly why hash IDs are wrong to
show a human scoring a report: "R-c44071f0" carries none of the meaning "R-01"
would, and a human reading the artifact doesn't need the hash's stability
guarantee — they need something they can hold in their head while comparing
against a PRD's own numbering.

This module bridges the two: every renderer maps real IDs to simple
`Req-1`, `Req-2`, ... labels *only at render time*. The registry, the ledger,
and every internal schema keep using the real hash IDs untouched.
"""

from __future__ import annotations

_LABEL_PREFIX = "Req"


def build_requirement_labels(requirement_ids: list[str]) -> dict[str, str]:
    """Maps each real requirement ID to a simple sequential label, in the
    order given — callers pass registry/traceability order, whichever list
    of real IDs they have on hand. Duplicate IDs in the input are collapsed
    to a single label (first occurrence wins), so callers can pass IDs
    straight from scenarios/cases without pre-deduplicating."""
    labels: dict[str, str] = {}
    for requirement_id in requirement_ids:
        if requirement_id not in labels:
            labels[requirement_id] = f"{_LABEL_PREFIX}-{len(labels) + 1}"
    return labels


def label_for(requirement_id: str, labels: dict[str, str]) -> str:
    """Falls back to the real ID if it's somehow not in the map (e.g. a
    hallucinated reference that slipped past the guard rail) — better to show
    an unfamiliar-looking ID than to silently drop it from a report."""
    return labels.get(requirement_id, requirement_id)


def labels_for(requirement_ids: list[str], labels: dict[str, str]) -> list[str]:
    return [label_for(rid, labels) for rid in requirement_ids]


def substitute_labels_in_text(text: str, labels: dict[str, str]) -> str:
    """Replaces any real requirement ID appearing in free-form model-generated
    prose (e.g. the reviewer's summary) with its display label. Structured
    fields like `requirement_ids` lists are mapped via `labels_for` above;
    prose has no such field to key off, so this scans for the raw IDs
    directly."""
    for requirement_id, label in labels.items():
        text = text.replace(requirement_id, label)
    return text
