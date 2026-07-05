from __future__ import annotations

import pytest

from driftpin.cli.repl import ParsedCommand, parse_command


def test_parse_command_returns_none_for_blank_input() -> None:
    assert parse_command("") is None
    assert parse_command("   ") is None


def test_parse_command_strips_leading_slash() -> None:
    result = parse_command("/help")
    assert result == ParsedCommand(name="help", args=[])


def test_parse_command_lowercases_command_name() -> None:
    result = parse_command("/STATUS")
    assert result is not None
    assert result.name == "status"


def test_parse_command_without_leading_slash_still_parses() -> None:
    result = parse_command("help")
    assert result == ParsedCommand(name="help", args=[])


def test_parse_command_splits_arguments() -> None:
    result = parse_command("/ingest a.md b.md")
    assert result == ParsedCommand(name="ingest", args=["a.md", "b.md"])


def test_parse_command_respects_quoted_paths_with_spaces() -> None:
    result = parse_command('/ingest "my prd.md"')
    assert result == ParsedCommand(name="ingest", args=["my prd.md"])


def test_parse_command_raises_on_unbalanced_quotes() -> None:
    with pytest.raises(ValueError):
        parse_command('/ingest "unterminated')


def test_parse_command_strips_leading_bom() -> None:
    """Some terminals/pipes prepend a UTF-8 BOM to the first line of input;
    it must not become part of the command name."""
    result = parse_command("﻿/help")
    assert result == ParsedCommand(name="help", args=[])
