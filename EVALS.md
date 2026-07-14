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
| Release 1 | Enumerate-then-fill, reviewer redesign, and AC/NFR registry ingestion all code-verified AND live-verified across NVIDIA/Groq; human precision/recall scoring still outstanding | Code-complete: the enumerate-then-fill rebuild, the reviewer redesign (structural + split semantic + fallback + 504 handling), and registry AC/NFR ingestion (deterministic parser + LLM fallback + fallback adjudication rule + 503-body classification) — see `DESIGN_DECISIONS.md`. Fill stage: 100% coverage on all 3 providers. Reviewer redesign: live-verified on NVIDIA (504-on-identical-payload auto-split-and-recovered, run `96d1dcff9eff`) and Groq (fallback call caught a real never-drop-rule violation live, run `20a0e981df7d`) — see "Reviewer redesign" below. AC/NFR registry ingestion: live-verified on NVIDIA — 12/12 requirements, 14/14 acceptance criteria correctly extracted and linked, 1 total LLM call, after fixing two real bugs (line-anchored regex breaking against block-reconstructed text) found only by running the actual pipeline, not the parser's own unit tests — see "503-body classification fix + deterministic AC extraction" below. A separate adversarial PRD was run to verify ambiguity-flagging behavior — see below. What's outstanding: the human precision/recall scoring itself against the golden set, which this tool deliberately does not perform on its own behalf. |
| Release 2 | Not started | — |
| Release 3 | Not started | — |
| Release 4 | Not started | — |

## Release 1 gate criteria (revised)

The gate itself was tightened after the coverage-ceiling finding below. The
current criteria a human runs before Release 1 is declared done:

- `driftpin generate cases` against a human-authored PRD, scored against
  hand-written golden test cases: **≥85% requirement coverage (recall)**,
  scored on a 70B-class model. A small-model run (e.g. local Ollama 3B)
  proves the pipeline's *structure* works — schema validation, traceability,
  ledger entries — never content quality, and must be labeled as such rather
  than presented as a quality result.
- **Step quality**: spot-check 5 random generated test cases against the
  step-quality rubric in `prompts/functional_tester.md.j2` — no single-step
  placeholder cases ("enter valid input → it works").
- **Contradiction check**: no generated test asserts behavior its linked
  requirement explicitly forbids. Any case that would have to is flagged as
  `flagged_contradiction` instead of emitted as a test — this schema field
  doesn't exist yet; the current implementation only carries an in-prompt
  instruction not to generate the contradiction in the first place (verified
  working live, see below), not a structured flag for the case where a fill
  call catches itself about to violate it.
- **Completeness-enforcement fixture**: mocked fills with a scenario
  deliberately left empty — must be detected and refilled by Python's diff
  against the Stage-1 checklist, not by a human noticing and rerunning the
  model manually; still empty after retries must surface as
  `GENERATION_FAILED` in `ASSUMPTIONS.md` and the rendered report. Done —
  `tests/test_orchestrator.py::test_fill_stage_refills_scenario_that_returns_empty_then_succeeds`
  and `::test_fill_stage_marks_generation_failed_after_exhausting_refills`
  (2 scenarios each, not the full 40 the plan originally specified — the
  logic under test doesn't scale with scenario count, so a larger fixture
  would add runtime without adding coverage of a new code path).
- **Error-class fixture**: one fill call mocked to return
  `finish_reason="length"` → verified to trigger a distinct concise-retry
  message rather than a generic validation-error one
  (`tests/test_structured_retry.py::test_length_truncation_gets_a_distinct_retry_message_not_generic_validation_text`);
  one mocked "request too large" (413, and a 400 naming a context-length
  ceiling) → verified to raise `RequestTooLargeError` distinctly rather than
  being blind-retried (`tests/test_groq_provider.py`,
  `tests/test_nvidia_provider.py`). Automatic input-splitting on a
  too-large request is not built — see `DESIGN_DECISIONS.md` for why that
  specific piece is an honest partial implementation.
- One full run on a local Ollama model with all outputs passing schema
  validation — structural check only, not a quality signal. Done, repeatedly.
- Adversarial PRD produces flagged ambiguities in `ASSUMPTIONS.md`, not
  invented coverage. Done — see "Adversarial-input check" below.
- Context-scoped synthesis: eval table showing it beats single-call, or it's
  removed. Not run yet (single-call remains the only implemented path).

## The 67%-coverage-ceiling finding (the evidence behind the enumerate-then-fill revision)

A human-executed review of `generate cases` output (local Ollama 3B) found
three real problems: placeholder-quality steps ("enter X → Y happens" with
no verifiable detail), only 4 of 12 requirements with any test case at all
(33% coverage), and one test case that directly contradicted its own
requirement — a case titled "Route Silent Drop" testing that unrecognised
voice input gets silently dropped, when the requirement it covered
explicitly states input must **never** be silently dropped. The reviewer
agent correctly flagged the coverage gaps on its own; it had not been asked
to check for the contradiction, which is what a human catching it revealed.

Root-caused the contradiction one stage earlier than expected: it wasn't a
`functional-tester` bug, it was `test-architect` misreading a negative
constraint ("never silently dropped") as if it described two valid branches
to test rather than one required behavior plus an explicit prohibition.
Fixed with an explicit rule in `prompts/test_architect.md.j2` about
interpreting "never X"/"must not X" language correctly, a matching guard in
`prompts/functional_tester.md.j2`, and a new contradiction-check rule in
`prompts/reviewer.md.j2` as a safety net. Also added a step-quality rubric
with a worked bad/good example to `functional_tester.md.j2`.

Verified on a stronger model (NVIDIA `nemotron-3-ultra-550b-a55b`) against
the same PRD, across three configurations, to test whether the fixes held
and whether coverage improved:

| Configuration | Requirements covered | Notes |
|---|---|---|
| Default token budget (8192), rubric fix only | 8/12 (67%) | Even 2 cases per covered requirement; stopped after scenario 8 of 12 |
| Doubled token budget (16384), same prompt | 5/12 (42%) — worse | Model spent the extra budget going deeper on the one `high`-risk requirement (7 cases) instead of covering more requirements |
| Doubled token budget (16384), prompt rewritten to force breadth before depth (two explicit passes) | 8/12 (67%) — identical to the first row | Depth/breadth balance fixed (no more one scenario eating 7 cases) but the same ~8-scenario ceiling remained |

Contradiction and step quality held correctly across all three runs — the
reviewer explicitly reported "no contradictions found" each time, and steps
consistently named concrete values and observable outcomes instead of
placeholders. Coverage alone stayed capped at 67% regardless of budget or
instruction ordering, which is the direct evidence behind the
enumerate-then-fill plan revision in `DESIGN_DECISIONS.md`: this is a
pacing/attention limit of one call enumerating-and-filling a long array, not
something more tokens or clearer wording fixes.

## Post-rebuild verification: enumerate-then-fill against all three providers

After building the two-stage architecture (`DESIGN_DECISIONS.md`), reran
`generate cases` against the same PRD 1 registry on all three configured
providers, back to back, same run methodology as the ceiling finding above:

| Provider | Coverage | Cases | Review | Notes |
|---|---|---|---|---|
| Groq `llama-3.3-70b-versatile` | **12/12 (100%)** | 27 | Passed | First fully clean run this project has had. Review summary was terse ("Audit review") — thin content, not a defect. |
| NVIDIA `nemotron-3-ultra-550b-a55b` | **12/12 (100%)** | 36 | Passed | Best review-summary quality of the three, explicitly enumerated 12/12 and 36/36 checks. First attempt's reviewer call hit a 504 from NVIDIA's endpoint after the fill stage had already completed all 16 scenarios it enumerated that run (confirmed via the ledger's `agent_step` summary: `scenarios_filled: 16, scenarios_refilled: 1, scenarios_failed: 0`) — a transient capacity issue on NVIDIA's side, not a pipeline defect; an immediate retry of the whole run succeeded end to end. |
| Ollama `llama3.2:3b` (local, structural check only — never a quality signal per gate criteria) | **12/12 (100%)** | 29 | **Failed** | Coverage hit 100% even on the weakest model tested — direct evidence the original ceiling was architectural, not a model-capability limit. The review failure here is a real weak-model content issue, unrelated to coverage: the reviewer's own `passed: false` contradicted its only findings being `info`-severity (which by its own stated rubric should yield `passed: true`), and its `summary` read like a test-case description rather than an audit conclusion. Zero refills or generation failures — the fill stage's own output was schema-valid and complete on the first attempt for all 12 scenarios. |

Coverage went from a hard 67% ceiling to 100% on every provider tested,
including the smallest model — confirming the fix was architectural
(per-scenario calls structurally can't stop partway through a list), not a
matter of model capability. The one real remaining gap: the **reviewer**
stage is still a single call over the whole suite, unlike functional-tester;
NVIDIA's transient 504 there is a reminder that a large reviewer prompt
carries the same latency exposure enumerate-then-fill was built to avoid —
not yet revisited, since the plan scoped the two-stage rebuild specifically
to `functional-tester`. Human precision/recall scoring against the golden
set's hand-written expected cases is still the outstanding gate item; this
verification proves the pipeline reliably produces complete, contradiction-free,
concretely-specified output to score, but scoring the *correctness* of that
output against the golden answer key remains a human task this tool
deliberately doesn't perform on its own behalf.

## Reviewer redesign: structural review + split semantic review + 504 handling

The post-rebuild verification above found the reviewer stage's remaining gap
honestly: a real human review of the generated output (not this tool
scoring itself) found the reviewer passing suites that contained a
mutually-exclusive-outcome contradiction and a rejection case violating a
requirement's own never-drop rule — both invisible to a reviewer that only
checks structure (IDs, coverage counts, owning_agent), never semantics
(does the asserted outcome match what the requirement actually says). Separately,
NVIDIA's single-call reviewer (full suite + full requirement text + the new
semantic checks) started failing with a 504 gateway timeout on 3 consecutive
attempts with an unchanged payload — evidence the single-call design was
"dead at 12 scenarios," not a transient blip.

Redesigned per `DESIGN_DECISIONS.md`: structural checks moved to Python
(zero LLM calls), semantic review split into per-group calls (≤3 scenarios
each, deduped requirement text) plus one dedicated suite-wide fallback-rule
call, and provider-layer 502/503/504 handling capped at 2 retries before
raising `PayloadTooHeavyError` (distinct from the existing 429 backoff,
which stays generous since waiting genuinely helps there).

**Code-path verification (mocked, all passing):** 21 orchestrator tests
including the four requested regression fixtures — a case asserting two
mutually exclusive outcomes forces a blocker and a failed review; the exact
"Unrecognised Input Silently Dropped" case from the live 3B run is caught by
the dedicated fallback call even when the per-group semantic call misses it
(replicating what actually happened live); an invented endpoint/error code
is flagged as `assumption` severity, not a blocker; a fully grounded group
reviews clean with zero findings; a hand-built structurally broken suite
(hallucinated requirement ID + duplicate case ID) is caught by the
zero-LLM-call structural review; and a group that raises
`PayloadTooHeavyError` on its first (unsplit) attempt is automatically
split into two smaller groups that both succeed, with the reclassification
logged to `ASSUMPTIONS.md`.

**Live LLM-level verification: complete on NVIDIA, and it confirms the
fix.** Run `96d1dcff9eff` (NVIDIA `nemotron-3-ultra-550b-a55b`, full
`generate cases` pipeline against the same golden PRD) completed
end-to-end: 12 scenarios, 53 test cases, review passed. The ledger shows
the reviewer stage broke into 4 groups (`groups_reviewed: 4`) needing 6
group calls (`group_calls: 6`) plus 1 fallback call
(`structural_findings: 0, semantic_findings: 41, fallback_findings: 0`).
The gap between 4 groups and 6 calls is not incidental — `ASSUMPTIONS.md`
for this run shows NVIDIA hit the *exact* pre-redesign failure mode twice,
live: "NVIDIA returned 504 on 3 consecutive attempts with an identical
payload" on a 3-scenario group and again on the resulting 2-scenario half.
Both times, `_review_group_with_splitting` caught the `PayloadTooHeavyError`,
split the group, retried, and succeeded — with the run still finishing
clean and every individual LLM call in the ledger showing `attempt: 1` and
`stop_reason: tool_calls` (no raw errors reached the pipeline). This is
Fixture F happening for real, not simulated. All 41 semantic findings are
`assumption` severity (invented endpoints, thresholds, timing values,
response fields not in the source requirement) — zero blockers, zero
contradictions found in this generation, and the fallback call (which did
find a real never-drop-style requirement, Req-3, to check against) reported
no violation. This run doesn't reproduce the earlier live-observed
contradiction bug (this generation simply didn't produce one), so it
doesn't re-confirm the fallback call catching a real violation — but it
does confirm the mechanism runs correctly end-to-end without error, and
grounding-check volume (41 flags across 53 cases) suggests that check is
doing real work, not rubber-stamping.

Groq's fill stage was separately confirmed clean (12/12 scenarios, 0
refills) on three attempts total before/around its free-tier 100k-token
daily quota (93,350 → 98,672 → 97,427 → 99,873 tokens used across the
day's testing, two attempts dying on the reviewer's first call 3,335
tokens short of the cap). Once the quota window rolled over, a full run
completed end-to-end on Groq too: run `20a0e981df7d`
(`llama-3.3-70b-versatile`) produced 12 scenarios, 48 test cases, and —
critically — **`Review Passed: False`**. `structural_findings: 0,
semantic_findings: 21, fallback_findings: 1`. That one fallback finding is
the exact bug class this whole redesign exists to catch, caught live and
unprompted: *"TC-26: The case directly contradicts the fallback rule by
not routing empty input to quick note and instead not saving any quick
note (requirements: Req-3) — quote: 'Unrecognised or ambiguous input
routes to quick note — never silently dropped'."* The per-group semantic
call also independently flagged a second blocker (TC-4, a scope-creep
contradiction on Req-1). This is the fallback-call mechanism working
exactly as designed against a real model's real output, not a fixture —
the generator produced a flaw, the redesigned reviewer caught it, and the
pipeline correctly reported `passed: False` rather than silently emitting
a broken suite as if it had been reviewed clean.

A same-day attempt to substitute a local 12B+ Ollama model (`qwen2.5:14b`)
confirmed a hardware ceiling instead: this machine's 7.8 GB total RAM (2.4
GB free) can't load a 14B model at all — Ollama's own server fails before
any request is sent, independent of client-side timeouts. Only
`llama3.2:3b` fits this machine's memory budget; this doesn't matter
further since NVIDIA and Groq both closed live verification directly.

**What this means honestly:** the reviewer redesign is now verified by
construction (21 mocked tests, all four requested fixtures) and live
against two real 70B-class models on two different providers. NVIDIA's run
confirmed the redesign resolves its 504-on-identical-payload failure mode
(the problem that motivated the split-call architecture in the first
place). Groq's run confirmed the other half: the fallback call catching a
real, unprompted never-drop-rule violation in a live model's own output,
exactly the bug class a human reviewer originally caught and the old
single-call reviewer missed. Both halves of the redesign are now
demonstrated working, not just theoretically addressed.

## Registry AC/NFR ingestion + fallback adjudication rule

The reviewer redesign's own live evidence above surfaced three further gaps,
verified by direct human review of live output: (1) the fallback check
scanned only requirement body text, missing a never-drop rule stated only
in an acceptance criterion (AC-12 in the golden PRD: "If no active block
matches the description, action saves as a note, NOT silently dropped" —
invisible to the old body-only scan); (2) grounding findings flagged
legitimately-specified timing values (e.g. "within 3 seconds") as invented,
because NFR/performance sections were never ingested into the registry at
all; (3) the same behavior class (empty input creating no entry) passed
review on one live run and was blocked on another — the fallback rule was
written for "unrecognised input" and the case concerned an adjacent,
unnamed class ("empty input"), and the old prompt gave no guidance on how
to adjudicate that ambiguity, so verdicts varied by which model happened to
run it.

Built: `AcceptanceCriterion`/`NonFunctionalRequirement`/`NfrScope` schemas;
`derive_ac_id`/`derive_nfr_id` (content-addressed the same way
`derive_requirement_id` already is); `RequirementRegistry.ingest()` now
backfills ACs onto a pre-existing requirement on re-ingestion and links
scoped NFRs via source-span matching, storing global NFRs once rather than
duplicating them onto every requirement; `_build_review_requirement`/
`_resolve_requirement_nfrs` in the orchestrator flatten body+AC+NFR into
what both reviewer prompts render; `_find_fallback_rule_requirements` now
scans AC text with equal weight to body text; `reviewer_fallback.md.j2`
gained an explicit adjudication rule — a case touching an input class the
rule's text doesn't explicitly name downgrades to `assumption` with an
ambiguity note, never a blocker, reserving blocker severity for
unambiguous rule-text contradictions. See `DESIGN_DECISIONS.md` for the
full design rationale.

**Code-path verification (mocked/unit, all passing):** 202 tests total (up
from 185) — Fixtures G (AC-only fallback rule reaches the fallback call and
its rendered prompt contains the AC text, via a captured-prompt assertion,
not just the Python-level resolver), H (an ambiguous-boundary case
downgrades to `assumption` with an ambiguity note and never flips
`passed` to `False`, encoding the empty-input/unrecognised-input
instability as a permanent regression test), and I (NFR text resolves into
a requirement's review view when present in the registry and is absent
when it isn't, asserted in both directions, plus an end-to-end check that
the NFR text reaches the actual rendered group-review prompt) — plus eight
new registry-level tests (AC-ID/NFR-ID stability across re-ingestion,
global-vs-scoped NFR linking, backfilling ACs onto a pre-existing
requirement, registry-version bump) and two extractor tests confirming ACs
and NFRs get the same verbatim-substring anti-hallucination check
`source_span` already gets.

**Live verification: partial, and it surfaced a real, separate finding
worth stating plainly.** Re-ingesting the golden PRD live against NVIDIA
confirmed the mechanism end-to-end once: the first attempt correctly
extracted 3 real global NFRs, including the exact "< 3s" end-to-end timing
constraint from the PRD's own NFR section — direct confirmation that
Problem #2 (NFR-blind grounding false positives) is fixable by this design
when extraction cooperates. But requirement-extraction breadth itself
degraded sharply on that same live attempt: only 2 of the PRD's ~12–14
requirements were extracted (the original 12 in the registry were
preserved untouched, exactly as the stability guarantee requires, but were
never re-matched or backfilled because the live candidate set didn't
overlap them), and the one AC captured was truncated to just its label
("AC-01 (R-02): " with no content after the colon) despite `stop_reason:
tool_calls` (a clean completion, not a length cutoff — `tokens_out: 3384`
for 2 requirements). A second attempt added a detailed worked example to
the extraction prompt's AC rule; result was byte-for-byte identical — same
2 requirements, same truncated AC. A third attempt trimmed the AC
instruction back down to roughly its original length, to test whether
prompt complexity itself was the cause; this attempt failed outright on
`NVIDIA returned 503 on 3 consecutive attempts` with the response body
`"ResourceExhausted: Worker local total request limit reached (32/32)"` —
NVIDIA's own shared free-tier worker pool being exhausted from the
preceding live calls this session, not a payload-size problem our
`PayloadTooHeavyError` splitting logic could do anything about. The
registry was restored to its clean pre-experiment state after each
attempt; no corrupted or partial data was left behind.

**What this means honestly:** the AC/NFR mechanism — registry storage, ID
stability, reviewer-prompt wiring, the adjudication rule — is verified
correct wherever it can be tested deterministically, and the NFR half is
now also confirmed live. The AC-extraction half surfaced a genuine,
separate problem: a single extraction call asked to find every requirement
*and* cross-reference every acceptance criterion back to the right one
appears to degrade in breadth on this model, the same failure shape that
motivated splitting fill generation and review into narrower per-item
calls earlier in this project. It wasn't resolved this round — the
evidence gathered (identical result before and after a wording change,
then an infrastructure-capacity failure on the next attempt) isn't enough
to say whether the fix is "simplify the wording further," "split AC
extraction into its own per-requirement pass the way fill generation was
split," or something else, and answering that needs a clear NVIDIA worker
queue and a few more controlled attempts, not more speculative retries
against an exhausted endpoint. (The extraction-breadth question was
subsequently resolved — deterministic AC parser, next section.)

**GATE item 3 follow-up: review-only reruns over saved cases, both halves
now executed.** The requirement: rerun ONLY the review stage over a
previous live run's saved cases — no regeneration — and confirm the
redesigned reviewer behaves sensibly on artifacts it didn't just produce.
Both reruns reconstruct `Scenario`/`TestCase` objects from the saved
rendered Markdown (the only surviving representation of a historical
run's cases), remapping `Req-N` display labels to real requirement IDs by
ordinal registry position — title-text matching was tried first and
failed, because titles are LLM paraphrases that vary between extraction
calls even when the content-addressed `requirement_id` is identical.
Reconstruction fidelity was verified before any LLM call each time: 12
scenarios, 48 cases, zero structural findings, full traceability
coverage.

- **NVIDIA half (2026-07-10):** executed over the saved NVIDIA cases; the
  review computed fully (the run's only defect was a cosmetic
  `UnicodeEncodeError` in the throwaway script's final terminal print
  loop, after all findings were already produced — not re-run, since a
  re-run would spend real quota to reproduce a non-deterministic result
  that was already substantially captured).
- **Groq half (2026-07-13, previously blocked on Groq's daily token
  quota):** over the 48 saved cases from run `20a0e981df7d` — the same
  run whose original review found the TC-26 never-drop blocker — the
  redesigned reviewer (with the adjudication rule now in place) returned
  0 structural, 12 semantic, 6 fallback, 0 coverage findings: **all 18
  `assumption` severity, `Review Passed: True`.** The headline is TC-26
  itself: the exact case the original review blocked ("not routing empty
  input to quick note") now correctly downgrades to `assumption`
  ("potentially contradicting the never-drop rule" — empty input being
  an adjacent-but-unnamed class relative to rule text written for
  "unrecognised input"), alongside five sibling empty-input boundary
  cases all consistently adjudicated the same way. This closes the
  TC-18/TC-26 instability item live on Groq: the same model family that
  produced both the blocker verdict and the pass verdict pre-rule now
  lands consistently on the rule's specified outcome. One honest scope
  note: this registry's requirements carry no AC/NFR data for this
  document (the registry was restored to its clean pre-AC state after
  the earlier duplication incident), so this rerun exercises the new
  review prompts and adjudication rule on body text only — the
  AC/NFR-grounded review path was verified live separately by the
  full-pipeline NVIDIA run `6ed8fb602b71` documented in the next
  section.

## 503-body classification fix + deterministic AC extraction

The AC-extraction-breadth question left open above turned out to have two
separate causes, both diagnosed from live evidence in this same session.
First: the third live re-ingestion attempt's `PayloadTooHeavyError` was
itself misdiagnosed. Its body — `"ResourceExhausted: Worker local total
request limit reached (32/32)"` — describes NVIDIA's shared worker pool
being full, not our payload being too large; the gateway-error handler
classified any 502/503/504 the same way regardless of body content, so it
would have prescribed splitting the payload and firing *more* requests at
an already-exhausted pool — the wrong remedy, and the reason chasing this
further live that round would only have made things worse. Second, and the
real fix for the AC-extraction breadth itself: the golden PRDs' acceptance
criteria are machine-parseable by construction (`AC-01 (R-02): ...`) and
never needed an LLM call at all — asking one extraction call to both find
every requirement and correctly cross-reference every AC back to the right
one was the actual defect, not a wording problem in the prompt (confirmed
by the earlier round's finding that a detailed worked example produced a
byte-for-byte identical, still-degraded result).

**Change 1 — gateway error classification inspects the body before
counting toward the payload-too-heavy threshold.** Both `nvidia_provider.py`
and `groq_provider.py` now check a 502/503/504 body against
`_SERVER_EXHAUSTION_PATTERNS` (`resourceexhausted`, `worker`, `request
limit`, `capacity`, `overloaded`, `quota`, case-insensitive) before the
existing gateway counter ever sees it. A match gets its own long,
patient backoff (30s doubling, capped at 5 minutes, 4 retries) and then a
hard `ServerExhaustedError` — never a split, never folded into
`PayloadTooHeavyError`. An empty or unrelated body still falls through to
the existing 2-retry `PayloadTooHeavyError` path unchanged (Fixture J's
second half, guarding against over-broadening the new classification to
cases where splitting genuinely is the right remedy). `run_agent` and
`extract_requirements` both log the matched pattern to
`ledger.record_assumption` before re-raising, so the classification
decision is evidence, not just a crash.

**Change 2 — a deterministic parser is now the primary AC-extraction
path, an LLM call only the fallback.** `ingestion/ac_parser.py` regex-
matches labeled ACs (`AC-01 (R-02): ...`, `AC-01:`, `**AC-01**`, and
variants), captures full multi-line text (never truncated to the label),
and links each to a requirement either via its inline `(R-xx)` reference
or, if none, the nearest preceding requirement-label line in the document
— both resolved by matching the document's own internal numbering scheme
against the LLM-extracted candidates' `source_span`, since requirement IDs
don't exist yet at this stage. This costs zero LLM calls for a
machine-labeled document. The extraction prompt itself was changed to
explicitly tell the model NOT to populate `acceptance_criteria` — that
job moved entirely out of the combined call. A per-requirement LLM
fallback (one small call per requirement: that requirement's body + the
AC section text) fires only when the parser finds an AC-like heading
section with zero parseable labels — never for a document that simply has
no ACs at all, which is a valid, common case that shouldn't spend LLM
calls. A requirement whose fallback call fails gets one retry; if that
also fails, it's marked `ac_extraction_failed` on the persisted
`Requirement` (distinguishing "extraction broke" from "genuinely has
none") and logged to `ASSUMPTIONS.md`. ACs the parser or fallback finds
but can't link to any requirement land in `unassigned_acs` on the registry
itself, never silently dropped.

**Code-path verification (mocked/unit, all passing):** 217 tests total (up
from 203) — Fixture J (a 503 body matching an exhaustion pattern
classifies as `ServerExhaustedError` with the matched pattern captured on
the exception, verified on both NVIDIA and Groq; a 504 with an empty body
still takes the unchanged `PayloadTooHeavyError` path, guarding against
over-broadening); Fixture K (a machine-labeled AC section — built from the
same structure as the golden PRD, including a multi-line-wrapped entry —
is fully extracted by the parser alone, zero extra LLM calls, full
multi-line text preserved exactly, correct requirement linkage via both
inline reference and nearest-preceding-heading grouping, an AC appearing
before any requirement heading correctly lands in `unassigned_acs`, and a
zero-padding mismatch between an inline reference and its requirement's
own label still resolves); Fixture L (an AC section with unlabeled prose
triggers the per-requirement LLM fallback; one requirement's call fails,
is retried once, fails again, and is recorded as `ac_extraction_failed`
and listed in `ASSUMPTIONS.md`, while a sibling requirement's call
succeeds normally in the same run).

**Live verification: succeeded, and it surfaced two more real bugs before
it did.** The first live re-ingestion attempt against NVIDIA, run with the
parser as originally written, extracted 12 requirements correctly but
found **zero** ACs anywhere despite the parser's own unit tests passing —
a live-only failure the mocked tests couldn't have caught, because they
never exercised the parser against the actual reconstructed
`document_text` `extract_requirements` builds. Direct debugging traced it
to two real bugs in how that text is assembled: `ingestion/parsers.py`
collapses each block's internal line breaks into a single space-joined
string (so an entire 14-entry "Acceptance Criteria" section arrives as one
line with no newlines between entries at all), and each block is prefixed
with `[anchor] ` before its own content (so even a heading like "##
Acceptance Criteria" doesn't start at column zero once assembled). The
parser's original line-anchored (`^...$`) regex matching only ever found
the first label in a block and only ever recognized a heading if nothing
preceded the `#` — both silent, complete misses once fed real block-
reconstructed text instead of a raw file. Rewritten to match by absolute
text position (`re.finditer`, no anchors) instead of by line, with
`\b`-bounded labels to avoid matching mid-word — a design that turns out
to be correct against both a raw file (this module's own tests) and the
actual assembled `document_text` (only provable by running the real
pipeline, not by unit-testing the parser in isolation against clean
strings).

With that fixed, a second live attempt on NVIDIA against the same golden
PRD: **12 of 12 requirements extracted, 14 of 14 acceptance criteria
found and correctly linked, 0 ambiguities, 1 total LLM call** (the
original requirement-extraction call — the AC-fallback path never fired,
because the deterministic parser succeeded on every entry). Requirement
IDs matched the registry's existing stable set exactly (the golden PRD's
own `R-01`–`R-12` labels, none renamed or dropped). Only 3 requirements
(`R-01`, `R-07`, `R-08`) have zero acceptance criteria — confirmed correct
against the source document itself, not a gap: those three genuinely have
none in the PRD's own Acceptance Criteria section. This is a direct,
complete reversal of the prior round's result (2 of ~14 requirements, one
truncated AC, then a capacity-exhaustion failure) — the same document, the
same provider, now costing one call instead of several failed ones.

(The registry was restored to its pre-experiment state afterward rather
than kept — the live run's extraction happened to quote a slightly
different `source_span` for each requirement than an earlier, unrelated
ingestion had, which would have left 24 duplicate-ish entries in the real
project's registry. That's a separate, pre-existing characteristic of LLM
extraction — how much of a line gets quoted verbatim isn't perfectly
deterministic across separate calls — not something this fix round
touched or needed to solve; the live evidence above stands regardless of
what became of that particular run's registry state.)

**Full-pipeline live confirmation, all three recent fix rounds together.**
With real AC/NFR data now in the registry, a full live `generate cases`
run on NVIDIA against the (temporarily 24-requirement, duplicate-inclusive)
registry (run `6ed8fb602b71`: 12 scenarios, 68 test cases,
`Review Passed: False`, `structural_findings: 0, semantic_findings: 48,
fallback_findings: 3`) exercised the AC/NFR-aware grounding check and the
fallback adjudication rule together, live, for the first time. Every
finding traces to a real, checkable reason:

- **Grounding correctly checks all three sources.** TC-6 through TC-13
  each correctly report that no confidence-score threshold "appears in
  Req-14, Req-2, their acceptance criteria, or applicable NFRs" — the
  grounding check is genuinely consulting body + AC + NFR, not just body
  text.
- **NFR-grounding is precise, not blanket.** TC-63 correctly identifies a
  2-second threshold as "a stricter, ungrounded invention" relative to the
  real NFR ("End-to-end voice flow < 3s on Groq p50") it's adjacent to —
  proximity to a real NFR doesn't launder an invented, different number.
- **AC-only fallback rules are caught live.** TC-17 and TC-47 both cite AC
  text directly as the violated rule — "Empty speech / mic timeout → no
  entry created, no crash, FAB resets" and "action saves as a note, NOT
  silently dropped" — the exact class of rule Problem #1 (two fix rounds
  ago) found invisible to a body-only fallback scan, now demonstrably
  caught.
- **The adjudication rule resolves the TC-18/TC-26 instability correctly,
  live, with three simultaneous examples.** TC-12 and TC-37 both concern
  "empty transcribed text" / "empty transcription" against a rule written
  for "empty speech" — the reviewer correctly downgrades both to
  `assumption` with an explicit ambiguity note ("an adjacent but distinct
  class... the rule's text does not explicitly address this boundary"),
  while TC-17 (an unambiguous violation of the same rule) stays a
  `blocker`. This is the exact instability the adjudication rule was built
  to fix, now proven resolving correctly on real model output rather than
  only in a mocked fixture.

## Requirement-to-scenario completeness enforcement

**The motivating finding.** A live full-pipeline run on Groq against the
official "PocketBudget" adversarial PRD (9 requirements, 11 acceptance
criteria) produced zero test scenarios or test cases for 3 of 9
requirements (R-07 Multi-Currency, R-08 Account Linking, R-09 Spending
Insights) — a ~33% miss rate. Every existing safety check reported clean:
`scenarios_failed: 0` (every scenario that WAS enumerated filled
successfully), no structural findings (no hallucinated IDs, no duplicate
case IDs), no semantic findings referencing those three requirements at
all, because nothing was ever generated for the reviewer to look at in the
first place. The gap was invisible to the entire pipeline, not just to one
check — it sat upstream of everything enumerate-then-fill's own
completeness guarantee was built to catch, which only ever verified "did
every ENUMERATED scenario get filled," never "did every requirement GET an
enumerated scenario."

**Change 1 — scoped refill at the enumeration layer.** After the initial
test-architect call, `_refill_missing_requirement_scenarios` diffs the
full requirement set against which requirements actually got a scenario
and, for anything missing, calls test-architect again — scoped to only the
missing requirements, up to 2 rounds, re-diffing between rounds.
**Change 2 — an independent, Python-enforced zero-coverage alarm as the
last step before rendering,** which scans the final traceability matrix
(zero LLM calls) and forces a blocker-severity finding — and `passed=False`
in code, through the same "any blocker anywhere" rule every other finding
source already uses — for any requirement with zero cases in the final
suite, regardless of why it's zero (enumeration never covering it, or a
downstream fill failure Change 1 has no visibility into).

**Code-path verification (mocked/unit, all passing):** 222 tests total (up
from 219), ruff clean — Fixture M (2 of 6 requirements missing at
enumeration time; the scoped refill call covers both in its first attempt;
`requirements_refilled: 2`, `requirements_failed: 0`); Fixture N (1
requirement never gets a scenario across the initial call or either of the
2 refill attempts; `requirements_refilled: 0`, `requirements_failed: 1`,
`"R-2 has no test scenarios"` present in `ASSUMPTIONS.md`); Fixture O (a
requirement's scenario IS successfully enumerated — Change 1 has nothing
to catch — but its fill call exhausts every retry and ends with zero
cases; the independent zero-coverage alarm still catches it, produces a
blocker finding naming the requirement with the description `"...has zero
test coverage in the final suite,"` and forces `result.review.passed is
False` even though no LLM reviewer call ever ran against that
requirement). Two pre-existing tests needed updating as a direct,
expected consequence of the new stage existing — not because they were
wrong: `test_functional_tester_prompt_only_includes_scenario_referenced_requirements`
deliberately never references its second requirement from any scenario
(that's the point of the test), which now legitimately triggers and
exhausts the scoped refill for that requirement; and the stage-progress
assertions in `test_actions.py` now correctly observe the new
`"test-architect (requirement coverage check)"` stage between the initial
architect call and the fill stage.

**Live verification: succeeded — after the first live run caught a real
bug in the fix itself.** The first PocketBudget rerun on NVIDIA (run
`c1c1268351e3`) showed the refill working exactly as designed
(`requirements_refilled: 2, requirements_failed: 0` — R-08 and R-09 were
missing from the initial enumeration and both got covered by one scoped
refill call) and the zero-coverage alarm firing correctly — but for 3
*different* requirements (R-01/R-02/R-03), which had been covered fine in
the original failing run. The ledger showed why: ~60 "requirement scope
violation" case drops, all against scenarios S-1 through S-9. Root cause:
each refill round is its own isolated test-architect call that numbers
its own scenarios starting from `S-1`, colliding with the initial
enumeration's real `S-1`..`S-9`; `_filter_cases_to_requirement_scope`'s
`scenario_id`-keyed dict let the refill's `S-1` silently replace the
original, so every case correctly filled for the original scenario was
validated against the wrong scenario's `requirement_ids` and dropped.
This is the exact class of bug `_renumber_case_ids` already existed to
prevent for case IDs — just never applied to scenario IDs, because until
this fix there was only ever one enumeration call and collision was
impossible. Fixed with `_renumber_scenario_ids` (Python reassigns all
merged scenario IDs sequentially before `generate_strategy_only`
returns), plus a regression test reproducing the collision (223 tests
total). Worth noting: the zero-coverage alarm (Change 2) is what caught
this — the bug produced no exception, no failed call, no structural
finding; without the independent final-coverage check it would have
shipped a suite silently missing 3 requirements *again*, which is
precisely the failure mode Change 2 was specified to make impossible to
miss.

The second PocketBudget rerun on NVIDIA (run `54ad2c2bcafa`), with the
collision fix in place: **9 of 9 requirements covered** — including R-07
Multi-Currency (4 cases), R-08 Account Linking (36 cases), and R-09
Spending Insights (12 cases), the exact three the original failing run
missed entirely. 25 scenarios, 144 test cases, `requirements_refilled: 2,
requirements_failed: 0`, zero scope-violation drops, `coverage_findings:
0` (the alarm correctly stays silent when the gap is fixed upstream).
`Review Passed: False` comes from 101 semantic findings — correct
behavior against an adversarial PRD full of planted ambiguities, not a
coverage failure.

Golden PRD regression check on NVIDIA (run `10e109f2293e`): **12 of 12
requirements covered**, 12 scenarios, 48 cases, `requirements_refilled:
0, requirements_failed: 0` (the initial enumeration covered everything,
so the refill correctly never fired and cost zero extra calls),
`coverage_findings: 0`. `Review Passed: False` from 4 semantic blockers
plus 2 fallback findings — all real content-quality findings on
generated cases (e.g. a case validating TTS-on-failure against an AC that
scopes TTS to successful actions only), unrelated to and unaffected by
the completeness step. No regression.

## PocketBudget answer-key scoring (durable rerun, Groq, 2026-07-13)

The PocketBudget PRD ships with a 9-item answer key (its "adversarial
damage log") of planted defects. The original scoring run's artifacts were
lost to session-scratchpad cleanup, so the pipeline was rerun into a
durable location (`evals/adversarial/pocketbudget_official/`, Groq
`llama-3.3-70b-versatile`, ingestion run `7496f587f761`, generation run
`06646550f826`: 9/9 requirements covered, 13 scenarios, 55 cases, all 11
ACs linked, all 5 NFRs ingested — every check had the complete data in
front of it). Each planted defect was scored against all three flag
surfaces: ingestion-time ambiguity flags, case-level ASSUMPTIONS.md
entries, and reviewer findings.

**Score: 0 of 9 fully caught; 4 of 9 partially surfaced (a flag lands on
the right requirement or even the right line, but names a weaker or
adjacent concern); 5 of 9 appear nowhere.** `Review Passed: True`.

| # | Planted defect | Verdict |
|---|---|---|
| 1 | Budget reset timezone (R-03 vs AC-05) | Partial — TC-24 flags "budget reset occurs at 12:00 AM — not specified" (right line, but flags *time* unspecified, never the *timezone* question or the R-03/AC-05 tension) |
| 2 | Alert dedup window conflict (R-04 vs AC-06) | Miss — nothing anywhere; fallback call scanned R-04 ("should not repeat") and found no case-level violation, but no stage compares a requirement against its own AC |
| 3 | Deleted-budget state unspecified (R-03) | Miss |
| 4 | AI scope: weak modal + no enforcement mechanism (R-05) | Partial — ingestion flagged "unclear about what questions the AI assistant answers" (right requirement, right general concern; modal and mechanism never named) |
| 5 | Export range: rolling-vs-calendar + new-user <12 months (R-06/R-09) | Partial — TC-36 flags "export with no data results in blank CSV or message — not specified" (the new-user half, in its extreme form); rolling-vs-calendar not flagged this run (the lost NVIDIA run's sibling had flagged it verbatim) |
| 6 | Unlink data retention vs GDPR NFR (AC-10 vs NFR) | Miss — despite the GDPR NFR being ingested and rendered into Req-8's review group |
| 7 | Export "amount" currency ambiguity (R-07 vs R-06) | Partial (weak) — ingestion flagged "does not specify the format of the CSV file" (right requirement, far vaguer than the planted currency-column gap) |
| 8 | Summary generation failure path (R-09) | Miss |
| 9 | Override conflict resolution + weak modal (R-02) | Miss — reviewer flagged 4 grounding assumptions on Req-2 categorisation, none touching repeated/conflicting overrides |

**The diagnostic pattern is architectural, not stochastic.** Every check
the pipeline currently runs compares a *test case* against *requirement
text* (grounding, scope, fallback rules). Seven of the nine plants are
*document-internal contradictions or gaps* — R vs its own AC (1, 2), R vs
another R (5, 7), R vs NFR (6), R vs its own silence (3, 8, 9). No stage
reads the requirement set against itself; the extraction prompt's
contradiction pass (which caught all 7 categories on the older
purpose-built adversarial PRD) surfaced only 3 vague gap-flags here,
plausibly because the planted conflicts require cross-referencing the AC
section — which the extraction call is now explicitly told to ignore
(ACs moved to the deterministic parser, which parses labels but checks
no semantics). The one-line conclusion: the pipeline is now strong at
"is this generated test grounded in the spec?" and still has no check
for "is the spec consistent with itself?" — a requirement-vs-AC/NFR/peer
consistency pass over the registry, which is precisely where 7 of these
9 planted defects live.

## Registry-level spec consistency pass (built in response to the scoring above)

New pipeline stage (`consistency/pairs.py`, `consistency/checker.py`, the
`consistency-checker` agent) runs after ingestion and before test-architect,
answering a question nothing in the pipeline previously asked: is the
requirement set consistent with itself. Python enumerates four pair
types — requirement vs its own AC, vs an applicable NFR, vs a
token-overlap-filtered peer requirement, vs its own silence on failure
handling — and a scoped LLM call classifies each as `contradiction`,
`threshold_mismatch`, `silence_gap`, `modal_ambiguity`, or `consistent`.
Full design rationale, the peer-pairing bound, and the modal-handling
split between this stage and the reviewer's own prompt are in
`DESIGN_DECISIONS.md`'s "Registry-level spec consistency pass" section.

**Code-path verification (mocked/unit, all passing):** 239 tests total
(up from 223), ruff clean. Fixtures P/Q/R/S/T cover exactly the five
verdict types end-to-end (contradiction and threshold_mismatch land as
blocker severity; silence_gap and modal_ambiguity as assumption; T
specifically guards against over-flagging — two requirements sharing
vocabulary but not actually conflicting must produce zero findings, not
a manufactured tension because a pair happened to get enumerated).
Separate unit tests cover the pair-enumeration functions directly
(token-overlap threshold, the top-5-neighbor cap that keeps peer pairing
sub-quadratic, NFR resolution matching the reviewer's own global/scoped
rule, and the silence-gap heuristic correctly crediting failure handling
specified via an applicable NFR rather than requiring it inline in the
requirement's own text) and both directions of the 200-pair budget guard
(declining aborts before any LLM call is issued — verified via an empty
mock-provider queue that would fail loudly if a call slipped through;
accepting proceeds and reports the exact enumerated count).

**Live verification: initially blocked, then completed on NVIDIA once a
working key was available.** Groq's daily quota (`tokens per day (TPD):
Limit 100000`) was exhausted from the same day's earlier live-verification
work and never recovered fast enough across two retry attempts (a bounded
hour-long retry loop measured only ~5,000 tokens freed per hour of a
genuinely rolling 24-hour window); the prior NVIDIA key separately
returned 403 "Authorization failed." Both live runs below used a
replacement NVIDIA key, `nvidia/nemotron-3-ultra-550b-a55b`.

**GATE 3 (golden PRD, no planted defects):** run `ae5a54a5ed92`, 10/10
requirements covered (no regression), all 55 consistency pairs completed
(14 req_vs_ac, 30 req_vs_nfr, 11 req_vs_peer; `req_vs_silence: 0` — the
golden PRD's requirements didn't trip the silence-gap heuristic).
**3 findings, not 0** — worth reporting precisely rather than as a clean
pass, because checking against the actual source document confirms all
three are real, not hallucinated:
- `R-ecd42d32` ("Confirmation Reply"): body says "after every action"
  (unconditional); its own AC says "every successful action" (narrower)
  and a second AC adds "does not play if device is on silent" (a
  condition absent from the body). Both a genuine `threshold_mismatch`
  and a genuine `contradiction` — the checker found real body-vs-AC drift
  that had never been checked for before.
- `R-8bd5fc12` ("FAB Auto-Close"): body says an exact "2.8 seconds"; its
  own AC says "2.8s (±0.3s)" — a real tolerance-band mismatch against an
  exact-value body.

This means GATE 3 as originally specified ("any finding is a false
positive, since the golden PRD has no planted defects") doesn't hold
in the strict pass/fail sense it was written in — but the underlying
intent (does the checker hallucinate tensions that aren't there) is
satisfied: zero of the 3 findings are fabricated. The finding here is
actually about the golden PRD itself, not the checker: a hand-authored
"golden" spec used as a clean baseline still has real body-vs-AC drift
nothing previously checked for, which is a legitimate, useful catch —
not something to explain away.

**GATE 2 (PocketBudget, scored against the 9-item answer key):** run
`87e209814654`, 9/9 requirements covered (no regression, `requirements_refilled:
2`), all 68 pairs completed (11 req_vs_ac, 45 req_vs_nfr, 12 req_vs_peer;
`req_vs_silence: 0` again — see the root-cause finding below). 4
consistency findings total.

| # | Damage | Verdict this run |
|---|---|---|
| 1 | Budget reset timezone (R-03 vs AC-05) | **Full** — `threshold_mismatch` on R-f082006a: "Text 1 states budgets reset 'on the 1st of each month' without specifying a time, while Text 2 specifies 'at midnight on the 1st — server time'" — names the exact unspecified-time-vs-server-time tension. |
| 2 | Alert dedup window (R-04 vs AC-06) | **Full** — `threshold_mismatch` on R-d62d369c: "Text 1 prevents duplicate notifications permanently... Text 2 limits duplicate prevention to the same calendar month" — an exact match to the planted permanent-vs-monthly conflict. |
| 3 | Deleted-budget state (R-03) | Miss — genuinely out of scope for all four pair types as designed; this is a CRUD-lifecycle gap ("what happens when a budget is deleted"), not a text-vs-text contradiction or an action-with-no-failure-path. |
| 4 | AI scope: weak modal + no mechanism (R-05) | Miss (one tangential assumption about prompt-injection defense surfaced, but doesn't touch the actual should-vs-must modal or the missing enforcement mechanism). |
| 5 | Export date range (R-06/R-09) | Miss this run — a req_vs_peer pair between R-06 and R-09 exists in the 12 enumerated peer pairs (both share "export"/"data"/"month" vocabulary), but this run's verdict for it was `consistent`. |
| 6 | Unlink vs GDPR (R-08 vs NFR) | Miss — the req_vs_nfr pair (R-08, GDPR NFR) genuinely was enumerated and checked (all 9 requirements × 5 global NFRs = 45 matches exactly), but the model's verdict for that specific pair was `consistent`. A model-quality miss on an existing check, not an architecture gap. |
| 7 | Multi-currency display (R-07 vs R-06) | **Partial** — a real `contradiction` was found, but framed differently than planted: "Text 1 states the app displays all amounts only in the user's home currency, while Text 2 requires foreign currency amounts to show both the original and converted amounts" is R-07's own body vs its own AC-09 (a req_vs_ac pair), not the R-06-export-column framing Damage 7 specifically planted. Same underlying currency-display looseness, different pair caught it. |
| 8 | Summary generation failure path (R-09) | Miss, and the root cause is now diagnosed: `req_vs_silence` enumerated **zero** pairs for the entire 9-requirement registry. `_has_silence_gap` checks the requirement's own text plus every applicable NFR for a failure keyword — but PocketBudget's one global NFR ("Sync failure: retry up to 3 times before surfacing error to user") applies to every requirement implicitly, so its "retry"/"error" keywords credit ALL nine requirements with "failure handling specified," including R-09 (Spending Insights), whose actual summary-generation action has nothing to do with sync. The heuristic is too coarse: it doesn't check that the failure text is actually about the same action, just that a failure keyword appears anywhere in scope. This is a real, live-discovered limitation, not a hypothetical — `tests/test_consistency_pairs.py`'s own `test_enumerate_req_vs_silence_pairs_credits_failure_handling_from_applicable_nfr` enshrines exactly this "credit from an applicable NFR" behavior as correct, which is right in principle (a global retry NFR SHOULD cover a sync action) but wrong in practice for an unrelated action sharing the same global NFR pool. |
| 9 | Override conflict resolution (R-02) | Miss — same class of out-of-scope gap as Damage 3: a lifecycle/state-machine question ("which override wins on conflict") that none of the four pair types are shaped to ask. |

**Score: 2 of 9 full, 1 of 9 partial, 6 of 9 miss** — up from the pre-fix
0 full / 4 partial, but short of the GATE's ≥7/9-with-at-least-partial
target. The two full catches are exactly the two defects the req_vs_ac
pair type was purpose-built for (a requirement's own body contradicting
its own AC), landing with near-verbatim precision. The diagnosis for the
shortfall is now precise rather than vague: three misses (3, 9, and
arguably 5's specific framing) are genuinely outside what the four pair
types were designed to check (lifecycle/state gaps, not textual
contradictions); one miss (6) is a live-confirmed model-verdict miss on
a pair that WAS correctly enumerated and checked; and one miss (8) is a
real, now-understood bug in the `req_vs_silence` heuristic's NFR-crediting
logic — worth a follow-up fix (scope failure-keyword credit to NFRs whose
own text shares vocabulary with the requirement's action, not any global
NFR regardless of topic) but out of this round's scope to change
mid-verification.

## R2 coverage-gap round: silence-fix + lifecycle pairs + verdict hardening

Three work items built in response to the 2-full/1-partial/6-miss GATE 2
score above (all three committed separately; 252 tests, ruff clean):
**(1)** the req_vs_silence global-NFR false-crediting fix — a global NFR's
failure keywords no longer silently credit every requirement; an
`nfr-applicability` call now asks, per (requirement, global NFR), whether
the NFR actually governs that requirement's action (scoped NFR links and
own-text failure wording still resolve for free); **(2)** a new
req_vs_lifecycle pair type — a one-time `lifecycle-entities` extraction
call links domain entities to requirements, then Python enumerates one
pair per (requirement, entity, state) across five fixed states (created,
modified, deleted, conflicting, expired_out_of_range), reusing the
existing verdict schema; **(3)** verdict hardening — a compliance-terms
rule in the checker prompt (exact-mechanism alignment for GDPR-class
vocabulary, not thematic similarity) plus optional self-consistency mode
(`--self-consistency-n N`, default off): N independent verdict calls per
pair, unanimity required, disagreement becomes a new `flagged_for_review`
verdict rather than a silent majority vote.

**A scoring correction first, against my own earlier table.** Re-checking
the baseline run's rendered report (not just ASSUMPTIONS.md, which is
what the earlier scoring grepped) shows the reviewer's modal-strength
check HAD already flagged Req-5's "should decline" (TC-57/TC-60) and
Req-2's "should remember" (TC-16) in the baseline. Damage 4 and Damage 9
were therefore already Partial at baseline, not Miss. Corrected baseline:
**2 full, 3 partial, 4 miss** (not 2/1/6).

**GATE 2 rerun (PocketBudget, NVIDIA, run `b20a4c367d69`,
`--self-consistency-n 3`): 5 full, 4 partial, 0 miss.**
164 pairs checked (11 req_vs_ac, 45 req_vs_nfr, 12 req_vs_peer, 6
req_vs_silence — up from the bug's 0 — and 90 req_vs_lifecycle), 7
applicability calls, 9/9 coverage retained.

| # | Damage | Verdict (vs corrected baseline) | Changed by |
|---|---|---|---|
| 1 | Budget reset timezone | Full — exact tension stated verbatim in 2 of 3 checks; surfaced as `flagged_for_review` because the third said `consistent` (was Full/blocker) | WI3 demoted severity, honestly |
| 2 | Alert dedup window | Full — unanimous, exact (unchanged) | — |
| 3 | Deleted-budget state | **Miss → Full** — lifecycle budget/deleted: "does not specify what happens when a budget is deleted or removed" | WI2 |
| 4 | AI scope modal + mechanism | Partial — modal half via reviewer (TC-48) as at corrected baseline; enforcement-mechanism half still unnamed | — |
| 5 | Export range | **Miss → Partial** — lifecycle export/expired_out_of_range names the 12-month boundary gap; rolling-vs-calendar and new-user framings still unnamed | WI2 |
| 6 | Unlink vs GDPR | **Miss → Partial** — lifecycle account/deleted: "describes unlinking an account but does not specify what happens when the account reaches a deleted state" — the planted unlink-vs-delete ambiguity, without naming GDPR. The req_vs_nfr (R-08, GDPR) pair STILL returned consistent despite the compliance-hardened prompt — recorded as a model-capability limit per the work item's own stop rule, not chased further | WI2 (partial); WI3 failed to flip the NFR verdict |
| 7 | Multi-currency display | Partial — same real contradiction, unanimous, same alternate framing (unchanged) | — |
| 8 | Summary generation failure | **Miss → Full** — req_vs_silence fired after the applicability call correctly refused sync-NFR credit: "generates a monthly spending summary but does not specify what happens if that generation action fails" | WI1 |
| 9 | Override conflict + modal | **Partial → Full** — lifecycle override/conflicting: "does not specify how the system resolves conflicting overrides for the same merchant" (exact), plus the modal half via reviewer and a flagged modal_ambiguity on the AC | WI2 |

Against the GATE target ("≥7/9 with at least partial coverage on all 9"):
all 9 damages now have at least partial coverage; 5 of 9 are full. If the
target reads "≥7 at least partially covered," it is met (9/9). If it
reads "≥7 FULL," it is not (5/9). Both readings reported; neither the
target nor the scoring was adjusted.

**GATE 3 rerun (golden PRD, NVIDIA, run `d464f89bd0ca`): the precision
regression is real and large — reported prominently, not buried.**
110 pairs (55 of them lifecycle), 60 findings vs the pre-round baseline
of 3. Classified against source text:
- **Real, unchanged:** the two R-ecd42d32 body-vs-AC findings (TTS
  every-vs-successful, TTS-vs-silent) still land unanimously; the
  R-8bd5fc12 2.8s-vs-±0.3s finding still surfaces (now
  `flagged_for_review`, 2 of 3 agreeing).
- **Real, new, correctly quarantined:** R-795ecde3 flagged — 1 of 3
  checks called the never-drop-rule-vs-"empty speech → no entry created"
  tension a contradiction. This is the SAME ambiguity that produced the
  live TC-26 blocker/pass verdict instability two rounds ago —
  self-consistency surfacing it as "genuinely ambiguous, human decides"
  instead of coin-flipping is exactly the designed behavior.
- **False positives, en masse: 48 lifecycle silence_gaps** (plus several
  lifecycle-flagged). Nearly EVERY (entity, state) lifecycle pair on this
  clean, defect-free PRD was flagged — e.g. "does not specify what
  happens when a meal_log reaches the 'created' state" on a requirement
  whose entire text IS the creation spec, and workout_log/modified,
  note/expired_out_of_range, etc. for every entity. The check as designed
  asks "does this text address state S" and almost any real-world spec
  text fails a pedantic reading of most states. The same shotgun fired on
  PocketBudget: its 2 exact lifecycle catches (Damage 3, 9) came bundled
  with ~80 sibling noise findings. Recall was bought with precision:
  lifecycle-pair specificity on a clean spec is roughly 0 of 48+.

**Verdict-hardening outcome (Work Item 3), stated per its own stop rule:**
self-consistency worked as designed (10 golden / 6 PocketBudget unstable
verdicts quarantined as `flagged_for_review` instead of silently asserted
or majority-voted, including one real known-ambiguous case); the
compliance-terms prompt rule did NOT flip the unlink-vs-GDPR req_vs_nfr
verdict — it returned `consistent` in all runs — and this is now
documented as a known model-capability limit, not chased further. Damage
6's partial credit comes from the lifecycle pair type instead.

**Net honest position:** recall target substantially reached (0 complete
misses, up from 4), driven mostly by the lifecycle pair type — but that
same pair type is currently an over-flagging instrument whose findings
are assumption-severity noise ~95% of the time on a clean spec. Before
the lifecycle check can be considered production-quality, it needs a
precision pass (e.g. only flag states the entity's own text makes
reachable, or require the state to be implied by some requirement before
asking about it) — an explicitly open item, out of this round's scope.

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

**Silent under-extraction, undetected for the whole session (found 2026-07-05):**
every run against the golden PRD up to this point reported "11 requirements,
0 ambiguities" and was treated as a clean, complete extraction. The PRD
actually contains 12 (R-01 through R-12) — one was being silently dropped
every single time, across multiple different models (Groq 8B, Groq 70B,
Ollama 3B), with no ambiguity ever flagged for the gap. This went unnoticed
because "0 ambiguities" reads as a success signal, and nobody had
cross-checked the extracted count against a manual count of the source
document until a registry reset forced a fresh look. A later run (after the
same reset) correctly extracted all 12. This is not attributed to a specific
prompt change — it's plausibly just run-to-run model variance on a
borderline-short requirement (R-05, "FAB is hidden on the Schedule screen,"
one line, easy to fold into an adjacent requirement) — but the real lesson
is methodological: requirement *recall* needs an explicit check against the
source document's actual requirement count, not just an absence of flagged
ambiguities, which only catches problems the extractor itself notices.
