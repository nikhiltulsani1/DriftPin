"""Deterministic acceptance-criteria parser.

PRDs in this project's golden set number their acceptance criteria in a
predictable, machine-parseable way (`AC-01 (R-02): ...`), and requirements
the same way (`R-02: ...`). Extracting these with a regex costs zero LLM
calls and cannot truncate, hallucinate, or lose breadth under prompt
complexity — three failure modes measured live when this was asked of a
single LLM extraction call instead (see DESIGN_DECISIONS.md's "Registry
AC/NFR ingestion" section). This parser is the primary path; a
per-requirement LLM fallback (`extractor.py`) only fires when this parser
finds an acceptance-criteria-like section but can't parse any labeled
entries out of it — never as a substitute for machine-parseable input.

Matching is position-based (`re.finditer` over the raw text), not line-
based: `ingestion/parsers.py` collapses each parsed block's internal line
breaks into a single space-joined string, so an entire "Acceptance
Criteria" section legitimately arrives here as one long line with no
newlines between entries at all. A `^...$`-per-line approach — the first
version of this module — only ever matched the first label in such a
block and silently missed everything after it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from driftpin.ingestion.text_utils import normalize_whitespace
from driftpin.schemas.requirements import CandidateRequirement

_AC_LABEL_RE = re.compile(
    r"\bAC[\s-]?(\d+)\s*\**\s*(?:\(\s*\**\s*([A-Za-z0-9\s-]+?)\s*\**\s*\)\s*)?\**\s*:\s*",
    re.IGNORECASE,
)
_REQUIREMENT_LABEL_RE = re.compile(
    r"\bR[\s-]?(\d+)\s*\**\s*:\s*",
    re.IGNORECASE,
)
_MARKDOWN_HEADING_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?#{1,6}\s")
# Plain numbered-section headings ("4. Acceptance Criteria", "7) Non-Functional
# Requirements") are common in non-Markdown PRDs. Deliberately narrow: the
# WHOLE line (after an optional block anchor) must be just "<number>.<title>"
# with a short, capitalized, punctuation-light title — so an ordinary numbered
# sentence or test step ("1. Speak the phrase 'x' -> y happens") doesn't get
# mistaken for a section boundary.
_NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)?\d{1,2}[.)]\s+[A-Z][A-Za-z0-9\s/&'-]{1,60}$"
)
_AC_SECTION_HEADING_RE = re.compile(r"acceptance criteria", re.IGNORECASE)
_DIGIT_RUN_RE = re.compile(r"(\d+)")
_WHITESPACE_RE = re.compile(r"\s+")


def _collapse_whitespace(text: str) -> str:
    """Collapses runs of whitespace like `normalize_whitespace`, but
    preserves case — the AC text returned here must stay a verbatim quote
    from the document, not the lowercased form `normalize_whitespace` uses
    for span-matching comparisons elsewhere in this module."""
    return _WHITESPACE_RE.sub(" ", text.strip())


def _normalized_number(digits: str) -> str:
    """"01" and "1" must resolve to the same requirement — an AC's inline
    reference isn't guaranteed to use the same zero-padding as the
    requirement's own label."""
    return str(int(digits))


def _extract_requirement_number(ref_text: str) -> str | None:
    match = _DIGIT_RUN_RE.search(ref_text)
    return _normalized_number(match.group(1)) if match else None


def _is_heading_line(line: str) -> bool:
    return bool(_MARKDOWN_HEADING_RE.match(line) or _NUMBERED_HEADING_RE.match(line))


def extract_ac_section_text(document_text: str) -> str | None:
    """The text of the document's Acceptance-Criteria-like heading section
    (everything between that heading and the next heading), for feeding to
    the per-requirement LLM fallback — never the whole document, keeping
    that call's input small by construction — and for bounding where the
    deterministic parser itself looks for AC labels. `None` if no such
    heading exists at all. Headings are matched per-line since each is its
    own short, unflattened block in practice, unlike the body text between
    them (both Markdown `#` headings and plain numbered ones like "4.
    Acceptance Criteria" — a PRD naming the section without a `#` prefix
    is common enough that treating only Markdown as "a real heading" left
    `found_ac_section` true while this returned `None`, silently starving
    the LLM fallback of the section text it needs)."""
    lines = document_text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if _is_heading_line(line) and _AC_SECTION_HEADING_RE.search(line):
            start = i + 1
            break
    if start is None:
        return None

    end = len(lines)
    for i in range(start, len(lines)):
        if _is_heading_line(lines[i]):
            end = i
            break
    return "\n".join(lines[start:end]).strip()


@dataclass
class ParsedACResult:
    """`acs_by_requirement_span` keys on a candidate requirement's own
    `source_span` — the only stable handle available at this stage, since
    registry IDs aren't assigned until `RequirementRegistry.ingest()` runs
    afterward."""

    acs_by_requirement_span: dict[str, list[str]] = field(default_factory=dict)
    unassigned_acs: list[str] = field(default_factory=list)
    found_ac_section: bool = False
    labels_parsed: int = 0


def _build_label_to_candidate(
    document_text: str, candidates: list[CandidateRequirement]
) -> tuple[dict[str, CandidateRequirement], list[tuple[int, str]]]:
    """Scans the whole document once for its own internal requirement-
    numbering scheme (e.g. "R-05: ..."), resolves each label to one of the
    already-extracted candidates by substring-matching that candidate's
    `source_span` against the text from the label's own start through the
    next label (starting from the label itself, not just past it, since a
    candidate's `source_span` may or may not include its own document
    label as a prefix — observed both ways live), and returns both the
    resolved mapping and an ordered position index used for nearest-
    preceding lookups when an AC has no inline reference of its own."""
    normalized_spans = [(c, normalize_whitespace(c.source_span)) for c in candidates if c.source_span.strip()]
    matches = list(_REQUIREMENT_LABEL_RE.finditer(document_text))

    label_to_candidate: dict[str, CandidateRequirement] = {}
    positions: list[tuple[int, str]] = []

    for pos, match in enumerate(matches):
        number = _normalized_number(match.group(1))
        end = matches[pos + 1].start() if pos + 1 < len(matches) else len(document_text)
        block_text = document_text[match.start() : end]
        normalized_block = normalize_whitespace(block_text)
        for candidate, span in normalized_spans:
            if span and span in normalized_block:
                label_to_candidate[number] = candidate
                break
        positions.append((match.start(), number))

    return label_to_candidate, positions


def _nearest_preceding_candidate(
    position: int,
    positions: list[tuple[int, str]],
    label_to_candidate: dict[str, CandidateRequirement],
) -> CandidateRequirement | None:
    best: CandidateRequirement | None = None
    for pos, number in positions:
        if pos >= position:
            break
        if number in label_to_candidate:
            best = label_to_candidate[number]
    return best


def parse_acceptance_criteria(
    document_text: str, candidates: list[CandidateRequirement]
) -> ParsedACResult:
    section_text = extract_ac_section_text(document_text)
    result = ParsedACResult(found_ac_section=section_text is not None)
    if not result.found_ac_section:
        return result
    assert section_text is not None
    label_to_candidate, positions = _build_label_to_candidate(document_text, candidates)

    matches = list(_AC_LABEL_RE.finditer(section_text))
    for i, match in enumerate(matches):
        result.labels_parsed += 1
        ref_text = match.group(2)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(section_text)
        ac_text = _collapse_whitespace(section_text[match.end() : end])

        candidate: CandidateRequirement | None = None
        if ref_text:
            number = _extract_requirement_number(ref_text)
            if number is not None:
                candidate = label_to_candidate.get(number)
        if candidate is None:
            # Locate this AC's position back in the full document (not just
            # the extracted section) so nearest-preceding-requirement
            # lookups compare against the same coordinate space `positions`
            # was built in.
            absolute_position = document_text.find(section_text) + match.start()
            candidate = _nearest_preceding_candidate(absolute_position, positions, label_to_candidate)

        if candidate is not None:
            result.acs_by_requirement_span.setdefault(candidate.source_span, []).append(ac_text)
        else:
            result.unassigned_acs.append(ac_text)

    return result
