"""Document parsing: normalizes PDF/DOCX/MD/TXT into anchored text blocks.

Every block carries a human-readable anchor (page, paragraph, or line range)
so a requirement extracted from it can be traced back to an exact location in
the source document, not just the document as a whole.
"""

from __future__ import annotations

from pathlib import Path

import pypdf
from docx import Document as DocxDocument
from pydantic import BaseModel

_MIN_BLOCK_LENGTH = 3


class SourceBlock(BaseModel):
    text: str
    anchor: str
    source_doc_path: str


class UnsupportedDocumentFormatError(Exception):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"Unsupported document format '{path.suffix}' for {path}. "
            "Supported formats: .pdf, .docx, .md, .txt"
        )


def parse_document(path: Path) -> list[SourceBlock]:
    """Dispatches to a format-specific parser. Raises on unsupported formats
    rather than guessing — ingestion must fail loudly, never silently degrade."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf(path)
    if suffix == ".docx":
        return _parse_docx(path)
    if suffix in (".md", ".txt"):
        return _parse_plain_text(path)
    raise UnsupportedDocumentFormatError(path)


def _parse_pdf(path: Path) -> list[SourceBlock]:
    blocks: list[SourceBlock] = []
    reader = pypdf.PdfReader(str(path))
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        for paragraph in _split_paragraphs(text):
            blocks.append(
                SourceBlock(
                    text=paragraph,
                    anchor=f"page {page_number}",
                    source_doc_path=str(path),
                )
            )
    return blocks


def _parse_docx(path: Path) -> list[SourceBlock]:
    blocks: list[SourceBlock] = []
    document = DocxDocument(str(path))
    for index, paragraph in enumerate(document.paragraphs, start=1):
        text = paragraph.text.strip()
        if len(text) >= _MIN_BLOCK_LENGTH:
            blocks.append(
                SourceBlock(
                    text=text,
                    anchor=f"paragraph {index}",
                    source_doc_path=str(path),
                )
            )
    return blocks


def _parse_plain_text(path: Path) -> list[SourceBlock]:
    blocks: list[SourceBlock] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    current_start: int | None = None
    current_lines: list[str] = []

    def flush(end_line: int) -> None:
        if current_start is not None and current_lines:
            text = " ".join(current_lines).strip()
            if len(text) >= _MIN_BLOCK_LENGTH:
                blocks.append(
                    SourceBlock(
                        text=text,
                        anchor=f"lines {current_start}-{end_line}",
                        source_doc_path=str(path),
                    )
                )

    for line_number, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if stripped:
            if current_start is None:
                current_start = line_number
            current_lines.append(stripped)
        else:
            flush(line_number - 1)
            current_start = None
            current_lines = []

    flush(len(lines))
    return blocks


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if len(p.strip()) >= _MIN_BLOCK_LENGTH]
