# Evals

Two independent tracks, never substituting for each other: a human-authored
golden dataset for whether generated output is *right*, and (from Release 2
onward) mutation scoring for whether generated test suites are *effective*.
Neither the golden dataset nor its scoring is authored or modified by the
system being evaluated — I write the harness; I don't write the answer key.

## Track 1: golden dataset (human-authored)

Lives at `evals/golden/`, one PRD per file, each paired with the requirements
and (where the author included them) acceptance criteria that PRD's own
author wrote. See `evals/golden/README.md` for the exact format and scoring
rules; the short version:

- **Requirement recall/precision** — always scored. Does Driftpin's extracted
  registry recover every requirement stated in the PRD, without inventing
  ones that aren't there?
- **Acceptance-criteria coverage** — scored only for PRDs that include an AC
  section. For each acceptance criterion, does at least one generated test
  case exercise the behavior it describes, traced to the correct requirement
  ID?

The dataset is intentionally mixed — some PRDs carry acceptance criteria,
some don't — because real intake looks like that, and a scoring harness that
only handles the tidy case isn't testing anything real.

I don't describe the contents of individual golden PRDs here; that would let
the harness get gamed by anyone (including future me) tuning the pipeline
against known answers. This file describes methodology, not the answer key.

## Track 2: mutation scoring (Release 2)

Not wired yet — arrives with `automation-engineer` in Release 2. `mutmut` for
Python targets, Stryker for JS/TS targets. Generated suites will need to kill
an agreed percentage of injected mutants before a suite counts as adequate,
independent of whether it happens to pass against current behavior. Threshold
to be set once the harness exists and there's a real suite to calibrate
against — publishing a number before there's data to justify it would just be
a guess dressed up as a target.

## Classifier evals (Release 2)

Two classifiers get scored against human-labeled samples before they're
trusted, per the verification protocol:

- **Automate-vs-manual recommendation** (test-architect) — scored against 20
  human-labeled cases.
- **Failure triage** (flaky / selector-drift / suspected-real-bug, from
  `triage-analyst`) — scored against 15 human-labeled failure samples.

Neither classifier is live yet; both ship with Release 2.

## Scores by release

| Release | Status | Notes |
|---|---|---|
| Release 1 | Not yet run | Code-complete: ingestion, requirement registry, ChromaDB chunk store, test-architect/functional-tester/reviewer agents, orchestrator, Excel/Markdown renderers, CLI + REPL, Docker packaging. Blocked on live provider credentials for the actual gate run against the golden PRD(s) in `evals/golden/`. |
| Release 2 | Not started | — |
| Release 3 | Not started | — |
| Release 4 | Not started | — |

## Regressions

None tracked yet — nothing has been scored. This section starts getting
entries the first time a change makes a previously-passing eval fail.
