from __future__ import annotations

from driftpin.render.labels import (
    build_requirement_labels,
    label_for,
    labels_for,
    substitute_labels_in_text,
)


def test_build_requirement_labels_assigns_sequential_labels_in_order() -> None:
    labels = build_requirement_labels(["R-aaaaaaaa", "R-bbbbbbbb", "R-cccccccc"])

    assert labels == {"R-aaaaaaaa": "Req-1", "R-bbbbbbbb": "Req-2", "R-cccccccc": "Req-3"}


def test_build_requirement_labels_dedupes_first_occurrence_wins() -> None:
    labels = build_requirement_labels(["R-aaaaaaaa", "R-bbbbbbbb", "R-aaaaaaaa"])

    assert labels == {"R-aaaaaaaa": "Req-1", "R-bbbbbbbb": "Req-2"}


def test_label_for_returns_mapped_label() -> None:
    labels = {"R-aaaaaaaa": "Req-1"}

    assert label_for("R-aaaaaaaa", labels) == "Req-1"


def test_label_for_falls_back_to_real_id_when_unmapped() -> None:
    labels = {"R-aaaaaaaa": "Req-1"}

    assert label_for("R-unknownid", labels) == "R-unknownid"


def test_labels_for_maps_a_list_preserving_order() -> None:
    labels = {"R-aaaaaaaa": "Req-1", "R-bbbbbbbb": "Req-2"}

    assert labels_for(["R-bbbbbbbb", "R-aaaaaaaa"], labels) == ["Req-2", "Req-1"]


def test_substitute_labels_in_text_replaces_every_occurrence() -> None:
    labels = {"R-aaaaaaaa": "Req-1", "R-bbbbbbbb": "Req-2"}
    text = "Coverage gap on R-aaaaaaaa and R-bbbbbbbb, also R-aaaaaaaa again."

    result = substitute_labels_in_text(text, labels)

    assert result == "Coverage gap on Req-1 and Req-2, also Req-1 again."


def test_substitute_labels_in_text_leaves_unmapped_ids_untouched() -> None:
    labels = {"R-aaaaaaaa": "Req-1"}
    text = "Hallucinated reference to R-unknownid found."

    result = substitute_labels_in_text(text, labels)

    assert result == "Hallucinated reference to R-unknownid found."
