# Driftpin

Requirement-centric QA automation. Every strategy, test case, automation script, and failure report in this system traces back to a specific requirement extracted from a source product document — not a test name, not a file path, a requirement ID.

Most "AI writes tests" tools optimize for volume: generate a pile of Playwright scripts and call it coverage. Driftpin optimizes for a different question — when something breaks, which requirement is at risk, and is the rest of the surface still covered. The requirement registry is the spine everything else hangs off of.

## What it answers

- Which requirement does this failing test protect?
- Which requirements are under-tested relative to their risk tier, not their line coverage?
- The PRD changed — which tests are now stale, and which requirements lost coverage?
- Are the generated tests actually good, measured against injected mutants and a human-authored golden set — not just "they pass"?
- Did a self-heal repair a broken selector, or quietly paper over a real regression?

## Status

Early build. Release 1 (interactive CLI, PRD ingestion, requirement registry, strategy and test-case generation with a traceability matrix) is in progress. See the release plan in `DESIGN_DECISIONS.md` once it lands.

## Architecture

Python 3.11+ core, no Node dependency for the CLI. Agents are declared as YAML (`agents/`) bound to a provider-agnostic `LLMProvider` interface — Anthropic and Ollama at launch, OpenAI later. Every agent output is schema-validated pydantic before any renderer touches it. Every LLM call, tool call, and subprocess execution is appended to a per-run ledger; that ledger is the only accepted evidence that something actually ran.

## Setup

```
pip install -e ".[dev]"
driftpin init
```

`init` walks through provider selection (Anthropic API key or a local Ollama model), validates the connection, and for local models runs a structured-output conformance probe before trusting it with schema-first agents.

## License

Proprietary. All rights reserved.
