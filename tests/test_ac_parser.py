from __future__ import annotations

from driftpin.ingestion.ac_parser import (
    extract_ac_section_text,
    parse_acceptance_criteria,
)
from driftpin.schemas.requirements import CandidateRequirement, RiskTier


def _candidate(title: str, source_span: str) -> CandidateRequirement:
    return CandidateRequirement(
        title=title,
        description="Description.",
        source_span=source_span,
        risk_tier=RiskTier.MEDIUM,
    )


# Mirrors the golden PRD's own Requirements + Acceptance Criteria structure,
# including a multi-line-wrapped AC (AC-04) and no blank lines between entries.
_GOLDEN_STYLE_DOCUMENT = """## Requirements

R-01: Voice input captured via device-native STT.
R-02: AI parses intent from transcribed text and routes to exactly one of five
      services.
R-05: FAB is hidden on the Schedule screen.

## Acceptance Criteria

AC-01 (R-02): "Had dal rice for lunch" -> meal_logs entry, category Lunch.
AC-04 (R-02): "Finished my morning study" -> block_completions entry for closest
              matching active block.
AC-09 (R-05): FAB component does not render on Schedule screen.

## Out of Scope
Multi-action in a single utterance.
"""

_R01 = _candidate("Voice input", "Voice input captured via device-native STT.")
_R02 = _candidate(
    "Intent routing",
    "AI parses intent from transcribed text and routes to exactly one of five\n      services.",
)
_R05 = _candidate("FAB visibility", "FAB is hidden on the Schedule screen.")


def test_parse_finds_all_labeled_acs_with_zero_dependence_on_llm() -> None:
    result = parse_acceptance_criteria(_GOLDEN_STYLE_DOCUMENT, [_R01, _R02, _R05])

    assert result.labels_parsed == 3
    assert result.found_ac_section is True
    assert result.unassigned_acs == []


def test_parse_captures_full_multiline_ac_text_not_truncated_to_label() -> None:
    result = parse_acceptance_criteria(_GOLDEN_STYLE_DOCUMENT, [_R01, _R02, _R05])

    acs_for_r02 = result.acs_by_requirement_span[_R02.source_span]
    assert any("matching active block." in ac for ac in acs_for_r02)
    wrapped = next(ac for ac in acs_for_r02 if "matching active block." in ac)
    assert wrapped == (
        '"Finished my morning study" -> block_completions entry for closest matching active block.'
    )


def test_parse_links_ac_to_correct_requirement_via_inline_reference() -> None:
    result = parse_acceptance_criteria(_GOLDEN_STYLE_DOCUMENT, [_R01, _R02, _R05])

    assert _R01.source_span not in result.acs_by_requirement_span
    assert len(result.acs_by_requirement_span[_R02.source_span]) == 2
    assert len(result.acs_by_requirement_span[_R05.source_span]) == 1
    assert "does not render on Schedule screen" in result.acs_by_requirement_span[_R05.source_span][0]


def test_parse_handles_plain_and_bold_label_variants() -> None:
    document = """## Requirements

R-01: Alpha requirement text.

## Acceptance Criteria

AC-01: Plain label with no parenthetical reference.
**AC-02**: Bold label, no reference.
**AC-03 (R-01)**: Bold label wrapping the parenthetical reference too.
AC 4: Space instead of hyphen before the number.
"""
    r01 = _candidate("Alpha", "Alpha requirement text.")

    result = parse_acceptance_criteria(document, [r01])

    assert result.labels_parsed == 4
    # AC-01 and AC-02 have no inline ref -> nearest preceding requirement (R-01).
    # AC-03 references R-01 explicitly. AC-04 also falls back to nearest preceding.
    assert len(result.acs_by_requirement_span[r01.source_span]) == 4


def test_parse_nearest_preceding_requirement_heading_grouping_without_inline_ref() -> None:
    """No "(R-xx)" on either AC bullet -- both fall back to the nearest
    requirement label found anywhere earlier in the document (not
    necessarily inside the Acceptance Criteria section itself), which for
    both entries here — since nothing between them changes it — is the
    last requirement declared before the Acceptance Criteria section."""
    document = """## Requirements

R-01: First requirement.
R-02: Second requirement.

## Acceptance Criteria

AC-01: Applies to whichever requirement precedes it, no inline reference.
AC-02: Also applies to whichever requirement precedes it, no inline reference.
"""
    r01 = _candidate("First", "First requirement.")
    r02 = _candidate("Second", "Second requirement.")

    result = parse_acceptance_criteria(document, [r01, r02])

    assert r01.source_span not in result.acs_by_requirement_span
    assert result.acs_by_requirement_span[r02.source_span] == [
        "Applies to whichever requirement precedes it, no inline reference.",
        "Also applies to whichever requirement precedes it, no inline reference.",
    ]


def test_parse_ac_before_any_requirement_heading_is_unassigned() -> None:
    document = """## Acceptance Criteria

AC-01: Appears before any requirement heading exists.

## Requirements

R-01: First requirement.
"""
    r01 = _candidate("First", "First requirement.")

    result = parse_acceptance_criteria(document, [r01])

    assert result.unassigned_acs == ["Appears before any requirement heading exists."]
    assert r01.source_span not in result.acs_by_requirement_span


def test_parse_zero_padding_mismatch_still_resolves() -> None:
    document = """## Requirements

R-02: Second requirement.

## Acceptance Criteria

AC-01 (R-2): Inline reference uses no zero-padding, requirement label does.
"""
    r02 = _candidate("Second", "Second requirement.")

    result = parse_acceptance_criteria(document, [r02])

    assert result.acs_by_requirement_span[r02.source_span] == [
        "Inline reference uses no zero-padding, requirement label does."
    ]


def test_parse_document_with_no_ac_section_at_all() -> None:
    document = "## Requirements\n\nR-01: Only a requirement, no AC section anywhere.\n"
    r01 = _candidate("Only", "Only a requirement, no AC section anywhere.")

    result = parse_acceptance_criteria(document, [r01])

    assert result.found_ac_section is False
    assert result.labels_parsed == 0
    assert result.acs_by_requirement_span == {}


def test_parse_recognizes_numbered_plain_text_headings_not_just_markdown() -> None:
    """Live bug found via the PocketBudget PRD: a PRD that numbers its
    sections ("4. Acceptance Criteria") instead of using Markdown "#"
    headings used to leave `found_ac_section=True` (a bare substring match
    anywhere in the document) while `extract_ac_section_text` returned
    `None` (heading detection required a literal "#") -- silently starving
    the LLM fallback of the section text it needs. Both must now agree."""
    document = (
        "3. Requirements\n\n"
        "R-01: First requirement.\n\n"
        "4. Acceptance Criteria\n\n"
        "AC-01 (R-01): First acceptance criterion.\n\n"
        "5. Out of Scope\n\n"
        "Nothing relevant here.\n"
    )
    r01 = _candidate("First", "First requirement.")

    result = parse_acceptance_criteria(document, [r01])

    assert result.found_ac_section is True
    assert result.labels_parsed == 1
    assert result.acs_by_requirement_span[r01.source_span] == ["First acceptance criterion."]


def test_parse_numbered_heading_pattern_does_not_match_ordinary_numbered_steps() -> None:
    """Guard against over-broadening: a numbered test step or list item
    ("1. Speak the phrase 'x' -> y happens") must not be mistaken for a
    section heading and truncate the Acceptance Criteria section early."""
    document = (
        "## Requirements\n\n"
        "R-01: Only requirement.\n\n"
        "## Acceptance Criteria\n\n"
        "AC-01 (R-01): First criterion.\n"
        "1. This looks numbered but is prose content, not a heading, and runs long.\n"
        "AC-02 (R-01): Second criterion, still inside the same section.\n\n"
        "## Out of Scope\n\nNothing here.\n"
    )
    r01 = _candidate("Only", "Only requirement.")

    result = parse_acceptance_criteria(document, [r01])

    assert result.labels_parsed == 2
    assert result.acs_by_requirement_span[r01.source_span] == [
        "First criterion. 1. This looks numbered but is prose content, not a heading, and runs long.",
        "Second criterion, still inside the same section.",
    ]


def test_extract_ac_section_text_returns_text_between_heading_and_next_heading() -> None:
    section = extract_ac_section_text(_GOLDEN_STYLE_DOCUMENT)

    assert section is not None
    assert "AC-01 (R-02)" in section
    assert "AC-09 (R-05)" in section
    assert "Out of Scope" not in section
    assert "R-01: Voice input" not in section


def test_extract_ac_section_text_returns_none_when_no_heading_exists() -> None:
    assert extract_ac_section_text("## Requirements\n\nR-01: Just a requirement.\n") is None
