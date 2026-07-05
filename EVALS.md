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
| Release 1 | Runs happened; human scoring not yet done | Code-complete and executed for real: ingestion, generation, and rendering all ran end-to-end against `evals/golden/prd-1-voice-assistant-fab.md` on Groq (`llama-3.1-8b-instant`, `llama-3.3-70b-versatile`) and local Ollama (`llama3.2:3b`), with ledger evidence for every run. A separate adversarial PRD (`samples/adversarial-account-lifecycle-prd.md`) was run to verify ambiguity-flagging behavior — see below. What's outstanding is the human precision/recall scoring itself, which this tool deliberately does not perform on its own behalf. |
| Release 2 | Not started | — |
| Release 3 | Not started | — |
| Release 4 | Not started | — |

## Adversarial-input check

A synthetic PRD with a direct requirement contradiction (permanent deletion
within 24 hours vs. 7-year mandatory retention of the same data), two
dangling references to documents that don't exist in the text, and an
internally self-contradictory requirement (instant password reset vs. a
multi-day manual review before "complete") was run through ingestion three
times total (Groq `llama-3.3-70b-versatile`), across two extraction-prompt
versions.

**First prompt version:** all 6 stated requirements extracted verbatim with
no fabricated or invented "resolution," but the ambiguities caught were only
the ones the source document itself admitted to ("this draft has not yet
been reconciled...") — not independently-reasoned findings like "R-01 and
R-02 directly contradict each other." The extractor noticed textual
admissions of uncertainty far more reliably than it independently detected
logical conflicts between two separately-true-sounding requirements.

**Hardened prompt version** (`prompts/extraction.md.j2`, explicit
instruction to cross-check every requirement against every other one for
direct contradictions, dangling external references, and internal
self-contradiction, as a deliberate second pass rather than a passive aside):
caught all four of the deliberately engineered issues in a single run —
the R-01/R-02 contradiction (citing both source spans), both dangling
references (Appendix C, the undefined account-merge policy), and R-04's
internal self-contradiction. Zero missed. Requirement extraction remained
accurate (still 6/6, still verbatim, still no fabrication).

**New honest caveat found while re-verifying:** re-running ingestion against
the identical document across these two prompt versions produced a
requirement with two different content-addressed IDs, because the model's
choice of exact sentence boundary for the same requirement shifted slightly
between the two calls (one included a leading clause, "When a user requests
account deletion,"; the other didn't) — different verbatim substrings hash
to different IDs. `DESIGN_DECISIONS.md`'s claim that re-ingesting an
unchanged PRD produces identical IDs holds for *extraction order*, which is
what it was written to address, but not necessarily for *exact
phrasing/boundary drift* across separate model calls on the same
requirement — that's a real, narrower gap than the original claim might
imply, worth fixing with either near-duplicate detection or boundary
normalization in a future pass, not something to gloss over.

**Second hardening round — gaps, not just conflicts:** the categories above
(contradictions, dangling references) are all about requirements that
conflict with each other or with unseen content. A separate, real failure
mode is a PRD that's simply too thin — vague requirements, or a capability
mentioned once and never actually specified. Extended the adversarial PRD
with three more cases and re-ran three times as the prompt was iterated:

- **Vague/unmeasurable requirement** ("the account settings page must load
  quickly and feel responsive to the user," no threshold given) — caught
  correctly on the first attempt and every attempt after.
- **Requirement fragment** ("Notification preferences — TBD, pending design
  review") — caught correctly on the first attempt and every attempt after.
- **Capability named in the overview but never given its own requirement**
  — three attempts across two prompt revisions failed to catch this, and one
  attempt produced a false positive in the opposite direction (flagging an
  "Out of Scope" item as if it were an unspecified capability — wrong by
  definition, since Out of Scope is a deliberate exclusion, not a gap; fixed
  by explicitly telling the prompt to skip Out of Scope/Non-Goals sections).

  On investigation, the test case itself was confounded: the phrasing used
  ("...data-retention behavior for the platform, including session activity
  export for compliance audits") reads as an elaboration of a requirement
  that already exists (R-02, data retention), not as an independent
  capability — a careful reader could reasonably conclude it's already
  covered. That's different from a genuinely independent, unrelated mention.
  Rewrote the test case to name something with zero thematic overlap to any
  existing requirement (a referral program crediting both users on a
  friend's signup) and added a worked example to the prompt itself,
  modeled on a different domain (login/payment/receipt-emailing) to avoid
  teaching the model to pattern-match the literal test wording. Result:
  **caught correctly**, alongside all six other categories, in a single run
  — 7 of 7 ambiguity types flagged, still 8/8 requirements extracted
  verbatim with zero fabrication.

  Re-ran the fully hardened prompt against the real golden PRD
  (`evals/golden/prd-1-voice-assistant-fab.md`, a normal, well-specified
  document, not adversarial) to check for false positives from all this
  hardening: 11/11 requirements extracted, same as before hardening began.
  One ambiguity appeared — a candidate for R-10 whose proposed quote used a
  single-quote character where the source document uses a double quote,
  caught by the pre-existing verbatim source-span check, unrelated to any of
  the new contradiction/gap rules. Zero new false positives from the
  hardening itself.

## Regressions

**Schema validation gap (found and fixed 2026-07-05):** an independent
fresh-context review found that `Scenario.title`, `TestCase.title`,
`TestStep.action`/`expected_result`, `ReviewReport.summary`, and
`ReviewerFinding.subject_id`/`description` had no `min_length` constraint,
unlike `requirement_ids` which already required at least one entry. A weak
model could satisfy the schema with an empty string and it would pass
validation silently — this is exactly how an earlier local-model run
produced blank scenario titles and a literal placeholder review summary
("Review summary") without tripping any validation error. Fixed by adding
`min_length=1` across all of the above, with new tests asserting each field
rejects an empty string.
