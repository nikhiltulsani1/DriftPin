# Design Decisions

This document records the architectural calls behind Driftpin and the reasoning
behind them. Some of these decisions cover subsystems not built yet (mutation
scoring, healing provenance, the workspace UI) — I'm recording the rationale
now, while the constraint that produced it is fresh, rather than reconstructing
it later when the code lands.

## Python core, no Node dependency

The CLI, the agent runtime, ingestion, and rendering are all Python. A test
automation tool that eventually has to drive Playwright, run pytest, and shell
out to mutation-testing tools benefits from living in the same runtime as the
artifacts it produces and executes. Keeping the core in one language also
means one dependency tree, one type system, one test runner for the tool
itself — not two stacks glued together with JSON over a socket.

## Schema-first outputs, not free-form generation

Every agent (test-architect, functional-tester, reviewer) emits a pydantic
model, not prose that gets parsed hopefully. `providers/structured.py` is the
one chokepoint every agent goes through: schema validation happens before
anything downstream — a renderer, the registry, another agent — ever sees the
output. A model that produces invalid JSON gets one bounded retry with the
exact validation errors fed back to it; after that, the caller must halt. No
downstream code defensively re-validates or coerces malformed output, because
none should ever reach it.

This is slower to build than "ask the LLM for JSON and hope" but it's the
difference between a tool whose failure mode is a clear halt with a diagnostic,
versus one whose failure mode is a corrupted spreadsheet three steps later.

## The requirement registry is the spine, not an afterthought

Everything traces back to a requirement ID: strategies, test cases,
traceability rows, review findings. The registry (`ingestion/registry.py`)
is deliberately the most tightly-controlled piece of the whole system:

- **IDs are content-addressed, never LLM-assigned.** A requirement ID is
  derived from a SHA-256 of the source document's hash plus the requirement's
  normalized verbatim text. Re-ingesting an unchanged PRD produces identical
  IDs, regardless of extraction order or which model ran the extraction.
  Nothing downstream has to guess whether "R-07" means the same thing it meant
  yesterday.
- **Every candidate requirement's source span is verified against the
  document after extraction**, not trusted on the model's word. A quote that
  can't be found verbatim in the source text gets demoted to a flagged
  ambiguity instead of becoming a requirement. This is the single most
  important guard rail in the ingestion pipeline — it's the difference between
  a registry that reflects what a PRD actually says and one that reflects what
  a model hallucinated it might say.

## Deterministic merge logic instead of council mode

The orchestrator (`agents/orchestrator.py`) runs a fixed pipeline —
test-architect, then functional-tester, then reviewer — and resolves conflicts
with plain guard-rail code: a scenario referencing an unknown requirement ID
gets dropped and logged to `ASSUMPTIONS.md`; a test case that expands scope
beyond its source scenario gets dropped the same way. There is no multi-model
debate, no judge model, no voting between agents.

This isn't a shortcut taken for lack of time. Council-style machinery adds
cost and latency without a clear mechanism for *why* one model's opinion should
override another's, and it makes failures much harder to reason about — "the
judge model disagreed with the generator" is not a debuggable state. Tagged
schemas plus deterministic code that can drop or flag a violation is legible:
you can point at the exact line that decided a scenario didn't belong. If a
future request asks for council mode, multi-model debate, or a judge model,
the answer is no — this line is the reason why.

## Context-scoped synthesis: single-call is the default, and stays the default until proven otherwise

The plan explicitly allows the test-architect to spawn scoped sub-calls across
multiple context sources (PRD, codebase, logs) with a rubric-based merge, but
only if it measurably beats a single-call baseline on the golden evals. As of
this writing that harness hasn't run yet, so the single-call baseline is all
that's implemented — there's no multi-call synthesis path sitting dormant in
the codebase waiting to be justified after the fact. If the evals eventually
show it wins, it ships with the comparison table attached. If it doesn't win,
it doesn't get built at all, not built-and-quietly-kept.

## The requirement extraction agent doesn't get to invent

`ingestion/extractor.py` treats the LLM's output as a claim, not a fact. The
extraction schema (`CandidateRequirement`) never lets a candidate carry its
own requirement ID or document hash — those are assigned by the registry,
after the fact, deterministically. And as noted above, every source span gets
checked against the actual document text before it's trusted. An extraction
agent that's wrong about *what* a requirement says is a bug to catch during
evals; an extraction agent that's allowed to assign its own IDs or fabricate
quotes is a design flaw that would poison every downstream artifact silently.

## One action layer, two front-ends

`cli/actions.py` holds the actual logic for ingesting documents, generating a
strategy, and generating full test cases. Both the one-shot CLI commands
(`driftpin ingest`, `driftpin generate cases`) and the interactive REPL
(`driftpin chat`) call the exact same functions. This wasn't originally
planned as a refactor — it fell out of building the REPL after the one-shot
commands already existed, and duplicating the ingestion/generation logic
between two front-ends would have meant fixing every bug twice and risking the
two surfaces drifting apart. A run should behave identically whether it's
triggered by a flag or by a slash command.

## The run ledger is the only accepted evidence

Every LLM call — including every failed structured-output attempt, not just
the winning one — gets appended to `.driftpin/runs/<id>/ledger.jsonl` via a
hook threaded through `providers/structured.py`. A claim that something ran
without a corresponding ledger entry isn't evidence. This applies to me
building the tool as much as it will apply to Driftpin's own generated
reports: "the pipeline ran successfully" needs a ledger reference, not a
sentence saying so.

## Interactive CLI over one-shot-only

Every action is available non-interactively (`--yes` flags, explicit
`--docs`/`--out` paths) for CI, but the primary interface is `driftpin chat`.
Requirement extraction, strategy generation, and coverage review are
exploratory tasks — a user reviewing what got extracted from a PRD, or
deciding whether a generated strategy's scope looks right before spending
tokens on full case generation, benefits from a session that keeps state
(the open registry, the configured provider) rather than a fresh process per
command. The REPL's `/strategy` and `/cases` commands show live per-stage
status ("Running test-architect...", "Running functional-tester...") because
that reflects real pipeline transitions via an `on_stage` callback — not a
synthetic token stream, since the current structured-output calls are
single-shot rather than incrementally streamed.

## Groq added to the provider roster ahead of schedule

The original plan scoped Release 1 to Anthropic and Ollama, with OpenAI
arriving in Release 3. Groq was added mid-Release-1 at explicit request,
because it's genuinely useful (fast, cheap hosted inference) and the person
building this also uses Groq's `llama-3.3-70b-versatile` in a different
product. The provider abstraction (`providers/base.py`) is deliberately
designed so a new provider is one new file implementing `LLMProvider` — Groq's
implementation reuses the same forced-tool-call structured-output strategy as
Anthropic, since Groq's API is OpenAI-compatible and supports the same
tool-choice forcing. Adding it cost one file and some wiring, not a
rearchitecture. That's the abstraction working as intended.

## Why mutation scoring will matter more than "tests pass" (Release 2)

A generated suite that passes proves the suite doesn't crash against current
behavior. It says nothing about whether the suite would catch a regression.
Release 2 wires `mutmut`/Stryker so generated suites are scored against
injected mutants — the objective, automatable half of test quality. The
golden dataset (human-authored expected cases) is the subjective half, scoring
whether the *right* things got tested. Neither substitutes for the other: a
suite can kill 100% of mutants while completely missing a business-critical
scenario a human would have caught immediately, and a suite that matches a
human's expectations perfectly can still be full of assertions too weak to
catch anything.

## Why healing provenance will exist (Release 2)

A self-healing test loop that silently patches a failing selector is
indistinguishable, from the outside, from one that's silently patching over a
real regression by loosening an assertion. Release 2's provenance report logs
every heal with a before/after diff and a classification — test adaptation
versus potential bug-masking — and bug-masking suspicions block the auto-heal
and surface for a human to look at. The alternative (heal everything
automatically, trust the green checkmark) is exactly the failure mode that
makes automated test maintenance untrustworthy: a suite that always passes
because it's quietly been taught not to notice anything.

## Why the eventual UI will be a workspace, not a chat pane (Release 4)

Conversational interaction is the CLI's job — it already has the REPL. The
planned Release 4 UI is explicitly not a second chat surface bolted onto a
web page; it's a project workspace (Sources, Run, Dashboard, Artifacts) whose
centerpiece is the traceability graph, something only Driftpin's data model
can render meaningfully. A chat pane duplicating the REPL would be scope
creep with no new capability. The dashboard is the actual differentiator.
