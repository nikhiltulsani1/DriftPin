from __future__ import annotations

from pathlib import Path

from driftpin.ledger.ledger import LedgerEntryType, RunLedger


def test_record_llm_call_writes_jsonl_line(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path, run_id="run-1")
    ledger.record_llm_call(
        agent_name="test-architect",
        provider="anthropic",
        model="claude-test",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.01,
    )

    entries = ledger.read_all()
    assert len(entries) == 1
    assert entries[0].entry_type == LedgerEntryType.LLM_CALL
    assert entries[0].agent_name == "test-architect"
    assert entries[0].tokens_in == 100


def test_record_subprocess_captures_exit_code(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path, run_id="run-1")
    ledger.record_subprocess(command="pytest -q", exit_code=0, output_excerpt="2 passed")

    entries = ledger.read_all()
    assert entries[0].entry_type == LedgerEntryType.SUBPROCESS
    assert entries[0].exit_code == 0


def test_multiple_entries_append_in_order(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path, run_id="run-1")
    ledger.record_subprocess(command="cmd1", exit_code=0, output_excerpt="ok")
    ledger.record_subprocess(command="cmd2", exit_code=1, output_excerpt="fail")

    entries = ledger.read_all()
    assert [e.command for e in entries] == ["cmd1", "cmd2"]
    assert [e.exit_code for e in entries] == [0, 1]


def test_record_assumption_creates_markdown_file(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path, run_id="run-1")
    ledger.record_assumption(
        heading="Contradictory requirements",
        detail="PRD section 2 and 4 disagree on session timeout duration.",
    )

    content = ledger.assumptions_path.read_text(encoding="utf-8")
    assert "Contradictory requirements" in content
    assert "session timeout duration" in content


def test_read_all_returns_empty_list_when_no_ledger_file(tmp_path: Path) -> None:
    ledger = RunLedger(tmp_path, run_id="run-1")
    assert ledger.read_all() == []
