"""Tests use a deterministic fake embedding function so they never download a
real model or touch the network."""

from __future__ import annotations

import hashlib
from pathlib import Path

from driftpin.ingestion.parsers import SourceBlock
from driftpin.ingestion.vectorstore import ChunkStore

_VECTOR_DIM = 8


class _FakeEmbeddingFunction:
    """Class-based, per chromadb's strict signature check on embedding functions —
    a bare function object doesn't satisfy `validate_embedding_function`."""

    def __call__(self, input: list[str]) -> list[list[float]]:
        vectors = []
        for text in input:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([b / 255.0 for b in digest[:_VECTOR_DIM]])
        return vectors


_fake_embedding_function = _FakeEmbeddingFunction()


def _blocks() -> list[SourceBlock]:
    return [
        SourceBlock(
            text="Users must reset their password via an emailed link.",
            anchor="paragraph 1",
            source_doc_path="prd.md",
        ),
        SourceBlock(
            text="Sessions expire after 30 minutes of inactivity.",
            anchor="paragraph 2",
            source_doc_path="prd.md",
        ),
    ]


def test_add_blocks_and_count(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path / "chroma", embedding_function=_fake_embedding_function)
    store.add_blocks(_blocks(), source_doc_hash="hash-a")

    assert store.count() == 2


def test_add_blocks_is_idempotent_via_upsert(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path / "chroma", embedding_function=_fake_embedding_function)
    store.add_blocks(_blocks(), source_doc_hash="hash-a")
    store.add_blocks(_blocks(), source_doc_hash="hash-a")

    assert store.count() == 2


def test_add_blocks_with_empty_list_is_a_no_op(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path / "chroma", embedding_function=_fake_embedding_function)
    store.add_blocks([], source_doc_hash="hash-a")

    assert store.count() == 0


def test_query_returns_matching_source_block(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path / "chroma", embedding_function=_fake_embedding_function)
    store.add_blocks(_blocks(), source_doc_hash="hash-a")

    results = store.query("password reset", n_results=1)

    assert len(results) == 1
    assert results[0].source_doc_path == "prd.md"
    assert results[0].anchor in {"paragraph 1", "paragraph 2"}


def test_query_respects_n_results(tmp_path: Path) -> None:
    store = ChunkStore(tmp_path / "chroma", embedding_function=_fake_embedding_function)
    store.add_blocks(_blocks(), source_doc_hash="hash-a")

    results = store.query("anything", n_results=1)

    assert len(results) == 1
