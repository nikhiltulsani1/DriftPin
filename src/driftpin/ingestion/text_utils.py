"""Shared text-normalization helpers for requirement ID derivation and span verification."""

from __future__ import annotations

import re


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())
