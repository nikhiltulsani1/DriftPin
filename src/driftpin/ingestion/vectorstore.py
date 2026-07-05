"""Chroma-backed chunk store for ingested document blocks.

This provides semantic retrieval over ingested content. It is not currently
wired into the generation pipeline — for the PRD sizes Release 1 targets,
agents operate directly over the full requirement registry. This store
exists as the retrieval substrate context-scoped synthesis would need if
that experiment ever beats the single-call baseline on evals; until then
it is dormant infrastructure, not a hidden dependency of the agent pipeline.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import chromadb
from chromadb.api.types import EmbeddingFunction

from driftpin.ingestion.parsers import SourceBlock

_COLLECTION_NAME = "document_chunks"
_CHUNK_ID_LENGTH = 16


def _chunk_id(source_doc_hash: str, anchor: str) -> str:
    basis = f"{source_doc_hash}:{anchor}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_CHUNK_ID_LENGTH]


class ChunkStore:
    """One instance per project, persisted at `.driftpin/chroma/`."""

    def __init__(
        self,
        persist_directory: Path,
        embedding_function: EmbeddingFunction[Any] | None = None,
    ) -> None:
        persist_directory.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_directory))
        if embedding_function is not None:
            self._collection = self._client.get_or_create_collection(
                _COLLECTION_NAME, embedding_function=embedding_function
            )
        else:
            self._collection = self._client.get_or_create_collection(_COLLECTION_NAME)

    def add_blocks(self, blocks: list[SourceBlock], source_doc_hash: str) -> None:
        if not blocks:
            return

        ids = [_chunk_id(source_doc_hash, block.anchor) for block in blocks]
        documents = [block.text for block in blocks]
        metadatas: list[Mapping[str, str | int | float | bool]] = [
            {
                "source_doc_path": block.source_doc_path,
                "anchor": block.anchor,
                "source_doc_hash": source_doc_hash,
            }
            for block in blocks
        ]
        self._collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

    def query(self, query_text: str, n_results: int = 5) -> list[SourceBlock]:
        result = self._collection.query(query_texts=[query_text], n_results=n_results)
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]

        blocks: list[SourceBlock] = []
        for document, metadata in zip(documents, metadatas, strict=True):
            blocks.append(
                SourceBlock(
                    text=document,
                    anchor=str(metadata["anchor"]),
                    source_doc_path=str(metadata["source_doc_path"]),
                )
            )
        return blocks

    def count(self) -> int:
        return self._collection.count()
