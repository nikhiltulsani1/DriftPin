"""Run ledger: append-only JSONL record of every LLM call, tool call, and subprocess.

Success claims without a corresponding ledger entry are not valid evidence that
an action occurred. Every write is a single JSON line, flushed immediately, so
a crash mid-run still leaves an accurate partial record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class LedgerEntryType(StrEnum):
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    SUBPROCESS = "subprocess"
    AGENT_STEP = "agent_step"


class LedgerEntry(BaseModel):
    run_id: str
    entry_type: LedgerEntryType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    agent_name: str | None = None
    provider: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    command: str | None = None
    exit_code: int | None = None
    output_excerpt: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunLedger:
    """One instance per run; writes to `.driftpin/runs/<run_id>/ledger.jsonl`."""

    def __init__(self, driftpin_dir: Path, run_id: str) -> None:
        self.run_id = run_id
        self._run_dir = driftpin_dir / "runs" / run_id
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._ledger_path = self._run_dir / "ledger.jsonl"
        self._assumptions_path = self._run_dir / "ASSUMPTIONS.md"

    @property
    def ledger_path(self) -> Path:
        return self._ledger_path

    @property
    def assumptions_path(self) -> Path:
        return self._assumptions_path

    def record(self, entry: LedgerEntry) -> None:
        with self._ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")

    def record_llm_call(
        self,
        agent_name: str,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.record(
            LedgerEntry(
                run_id=self.run_id,
                entry_type=LedgerEntryType.LLM_CALL,
                agent_name=agent_name,
                provider=provider,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                metadata=metadata or {},
            )
        )

    def record_subprocess(
        self,
        command: str,
        exit_code: int,
        output_excerpt: str,
        agent_name: str | None = None,
    ) -> None:
        self.record(
            LedgerEntry(
                run_id=self.run_id,
                entry_type=LedgerEntryType.SUBPROCESS,
                agent_name=agent_name,
                command=command,
                exit_code=exit_code,
                output_excerpt=output_excerpt,
            )
        )

    def record_tool_call(
        self,
        agent_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        result_excerpt: str,
    ) -> None:
        self.record(
            LedgerEntry(
                run_id=self.run_id,
                entry_type=LedgerEntryType.TOOL_CALL,
                agent_name=agent_name,
                metadata={"tool_name": tool_name, "arguments": arguments, "result": result_excerpt},
            )
        )

    def read_all(self) -> list[LedgerEntry]:
        if not self._ledger_path.exists():
            return []
        entries = []
        for line in self._ledger_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(LedgerEntry.model_validate_json(line))
        return entries

    def record_assumption(self, heading: str, detail: str) -> None:
        """Appends a flagged ambiguity or assumption; never silently resolved."""
        is_new = not self._assumptions_path.exists()
        with self._assumptions_path.open("a", encoding="utf-8") as handle:
            if is_new:
                handle.write(f"# Assumptions and Ambiguities — run {self.run_id}\n\n")
            timestamp = datetime.now(UTC).isoformat()
            handle.write(f"## {heading}\n\n_{timestamp}_\n\n{detail}\n\n")
