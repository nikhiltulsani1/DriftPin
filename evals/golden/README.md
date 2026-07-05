# Golden Dataset

Human-authored only. Nothing in this directory is generated, edited, or
scored by the same system it evaluates — that would make the gate
meaningless. Each PRD here is paired with its own requirements and acceptance
criteria, written by the person who wrote the PRD, before Driftpin ever sees
it.

## Format

Each file is a standalone PRD in whatever structure its author used. PRDs in
this set vary — some include acceptance criteria, some don't. The one
convention this harness relies on: requirements are individually identified
(e.g. `R-01`, `R-02`, ...). Where present, acceptance criteria reference the
requirement(s) they validate (e.g. `AC-01 (R-02): ...`).

## Scoring

Run by the human per the verification protocol — never self-scored by the
agent that produced the output. Applied per PRD, depending on what that PRD
provides:

1. **Requirement recall/precision** (always applicable) — does Driftpin's
   extracted requirement registry recover every `R-xx` in the PRD, without
   inventing requirements that aren't there?
2. **Acceptance-criteria coverage** (only for PRDs that include an AC
   section) — for each `AC-xx`, does at least one generated test case
   exercise the behavior it describes, traced back to the correct
   requirement ID?

For a PRD with no acceptance criteria, requirement recall/precision is the
only available check — that's expected, not a gap in the dataset.

## Inventory

| File | Title |
|---|---|
| `prd-1-voice-assistant-fab.md` | Voice Assistant — Quick Capture FAB |
