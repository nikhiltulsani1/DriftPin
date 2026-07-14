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

## Enumerate-then-fill: generation is two-stage with code-enforced completeness

Single-call generation of a full test suite is forbidden — not a stylistic
preference, a direct response to a measured failure mode.
`functional-tester`'s original design asked one call to enumerate a full
scenario list *and* fill every scenario with concrete, rubric-quality test
cases in the same response. Live testing against PRD 1 (12 requirements, a
stronger 70B-class reasoning model, three different configurations — the
default token budget, a doubled token budget, and a prompt rewritten to
force scenario breadth before case depth) converged on the same ceiling
every time: roughly 8 of 12 scenarios filled, 67% coverage, regardless of
budget or instruction wording. Doubling the token budget didn't help — it
made things worse, because the model spent the extra room going deeper on
one high-risk scenario instead of covering more of the list. Reordering the
instructions to demand breadth first got the depth/breadth balance right
but didn't move the ceiling. Three independent live configurations landing
on the same number is strong evidence this is a genuine pacing/attention
limit of asking one call to enumerate-and-fill a long structured array, not
a prompt-wording problem prompt engineering can solve. This is the same
reason the industry Planner/Generator (or plan-then-execute) split exists
for long-horizon agent tasks generally, applied here at the test-case layer
specifically.

Implemented in `agents/orchestrator.py`:

- **Stage 1 — Enumeration is test-architect's own scenario list, not a
  second call.** The plan as originally proposed called for a dedicated
  enumeration call returning `{ scenario_id, title, requirement_ids,
  risk_tier, execution_recommendation }`. `TestStrategy.scenarios` already
  carries every one of those fields — re-deriving an identical checklist
  with a second LLM call would be pure redundancy with no upside. Python
  uses `strategy.scenarios` directly as the authoritative checklist Stage 2
  fills against.
- **Stage 2 — Fill, one call per scenario** (`_fill_scenario`,
  `_fill_all_scenarios`). Each call receives only that scenario, the full
  text of *only* its linked requirements (never the whole PRD), the
  step-quality rubric, and the contradiction guard
  (`prompts/functional_tester.md.j2`). A call this narrow structurally
  cannot "run out of attention" partway through 12 scenarios, because it
  only ever sees one. Calls run sequentially with a configurable inter-call
  delay (`fill_call_delay_seconds`, default 2s, `DEFAULT_FILL_CALL_DELAY_SECONDS`);
  grouping into batches of ≤3 or parallel/async fills stayed unbuilt —
  per-scenario is the strongest completeness guarantee available and
  nothing has yet demanded the extra throughput of either alternative.
- **Python owns test-case IDs.** The model returns placeholder IDs in a
  `FillResult` (`schemas/test_cases.py`); `_renumber_case_ids` assigns final
  sequential `TC-N` IDs across the whole suite at merge time, the same way
  requirement IDs are never trusted to the extracting LLM.
  `scenario_id`/`owning_agent`/`execution_recommendation` are forced onto
  every returned case from the scenario Python already knows it called for
  — also never trusted to the model's own echo.
- **Completeness is enforced by a diff, not assumed from model stamina.**
  A scenario whose fill call returns zero cases is retried in place, up to
  `_MAX_SCENARIO_REFILL_ATTEMPTS` (2) more times. Still empty after that,
  its `scenario_id` is recorded in `PipelineResult.failed_scenario_ids`,
  logged to `ASSUMPTIONS.md`, and rendered as a distinct `GENERATION_FAILED`
  section/sheet in both the Markdown and Excel reports — deliberately not
  left to look like an ordinary zero-coverage row. The 67%-ceiling finding
  above is exactly why this can't be "ask the model nicely to cover
  everything" — coverage has to be a property Python's diff guarantees, not
  a hope.
- **Scale guard** (`MAX_SCENARIOS_WITHOUT_CONFIRMATION = 100`,
  `TooManyScenariosError`). More than 100 scenarios from the architect
  raises unless a caller-supplied `on_scenario_count_check(count) -> bool`
  hook confirms proceeding — the CLI and REPL wire this to a `Confirm.ask`
  prompt (or an automatic "yes" under `--yes`/CI). The check runs inline,
  after the strategy already exists in memory, specifically so a confirmed
  "yes" never has to re-run test-architect and re-spend its tokens just to
  get back to the same strategy. Hierarchical enumeration or parallel fills
  aren't built preemptively for a document size nothing in this project has
  actually hit.

**Two of the three provider-layer error classes are implemented; the third
is deliberately partial.** A response with `finish_reason == "length"` means
truncation — `providers/structured.py`'s retry loop now detects this
distinctly and asks for the same content again "more concisely," instead of
feeding back a confusing schema-validation message for JSON that was simply
never finished. Because Stage 2 calls are already batch-size 1 (the smallest
possible unit), "retry with a smaller batch" doesn't apply the way it would
for a multi-scenario call; the concise-retry message is the mitigation
instead. An HTTP "too large" response (413, or a 400 naming a context/length
ceiling) is now raised as its own `RequestTooLargeError`
(`providers/base.py`, detected in both `groq_provider.py` and
`nvidia_provider.py`) rather than looking like any other HTTP failure that
might get blind-retried — but automatic input-splitting isn't implemented,
since a single scenario plus its few linked requirements is already close
to the smallest unit there is to split further; there's no evidence this
error has actually occurred at Stage 2's new granularity. A 429 rate-limit
is the case where retrying the unchanged request after a backoff is
correct, already implemented in both providers before this revision.
Conflating all three into one generic "retry on failure" path is exactly
how a length-truncation bug would look identical to a transient rate limit
and get the wrong fix applied.

## Reviewer redesign: structural review in Python, semantic review split into groups plus one fallback call

Enumerate-then-fill fixed coverage; it didn't fix content correctness. A
real human review of generated output (not the tool scoring itself) found
the single-call reviewer passing suites that contained a case asserting two
mutually exclusive outcomes in one step, and a case asserting a rejection
response for exactly the input category a requirement's own never-drop
rule protects — both invisible to a reviewer that only checks structure
(IDs, coverage counts, `owning_agent`) and never semantics (does the
asserted outcome actually match what the requirement says). Separately,
once the reviewer's single call grew to include full requirement text plus
three semantic checks, NVIDIA's endpoint started returning 504 on 3
consecutive attempts with an *identical* payload — not a transient blip,
evidence the single-call reviewer design was "dead at 12 scenarios" the
same way single-call `functional-tester` generation was dead at the same
scale, for the same underlying reason: one call carrying too much for a
model or gateway to reliably finish.

The redesign, implemented in `agents/orchestrator.py`:

- **Structural review moved entirely into Python — zero LLM calls**
  (`_run_structural_review`): hallucinated requirement IDs, unknown
  scenario IDs, duplicate case IDs, `owning_agent` consistency, and
  traceability coverage-count accounting are all facts code can check
  exactly. A blocker finding here means the suite is structurally broken
  and semantic review never runs — there's no point spending tokens
  auditing content quality on a suite that's already known-broken. In
  practice, under `run_pipeline`'s existing guard rails (the scope filter,
  sequential ID renumbering), these specific conditions can't actually
  occur live — this is a safety net for a future bug, verified directly
  against a hand-built broken suite in tests, not something `run_pipeline`
  itself can currently trigger.
- **Semantic review splits into per-group calls** (`_run_semantic_group_review`,
  `_review_group_with_splitting`; group size 3, `_REVIEW_GROUP_SIZE`). Each
  call receives only that group's cases and the deduped full text of only
  the requirements those cases link to — never the whole suite or
  registry. A group requirement dict is keyed by `requirement_id` so a
  requirement shared by two scenarios in the same group appears once, not
  once per scenario. On `PayloadTooHeavyError` the group is split in half
  and each half retried, recursively; a group already at size 1 that still
  fails is logged (`ASSUMPTIONS.md`, an `info` finding) and skipped rather
  than crashing the run — there's nothing smaller left to try.
- **One dedicated suite-wide fallback-rule call** (`_run_fallback_review`),
  separate from the per-group calls because a fallback/never-drop
  violation can be linked to a *different* requirement than the one
  defining the rule — exactly the live-observed bug class, which no
  per-group call (scoped to one small set of scenarios) could ever catch
  on its own. Input is small by construction: only the requirements a
  keyword heuristic (`_find_fallback_rule_requirements`, scanning for
  "never"/"must not"/"should not"/"cannot") identifies as defining a
  prohibition, plus a one-line `{case_id, title, expected_outcome}` summary
  per case — never full case objects. Skipped entirely (0 calls) when no
  requirement in the registry defines such a rule.
- **Python assembles the final review, never trusts a model's own
  bookkeeping for it** (`_compose_review_report`). No single LLM call
  returns a `ReviewReport` anymore — `GroupReviewResult` (just a
  `findings` list) is all any review call returns. `review_id`,
  `target_run_id`, `passed`, and `summary` are all computed by Python from
  the merged structural + semantic + fallback findings; `passed` is false
  if and only if any merged finding is blocker-severity. This removes the
  earlier "force `passed` to false if the model's own claim contradicts its
  findings" guard rail entirely — there's no model-reported `passed` left
  to contradict.
- **Ledger**: each group call, the fallback call, and a per-run summary
  (`groups_reviewed`, `group_calls`, `fallback_calls`,
  `structural_findings`, `semantic_findings`, `fallback_findings`) are all
  recorded, the same evidentiary standard every other stage in this system
  already meets.

**Provider-layer 502/503/504 handling** (`groq_provider.py`,
`nvidia_provider.py`): capped at 2 retries with exponential backoff before
raising `PayloadTooHeavyError`, distinct from 429 (rate limit), which keeps
its existing, more generous retry budget since waiting genuinely helps
there in a way it doesn't for a gateway timing out on an identical
payload's size. This is what `_review_group_with_splitting` catches to
decide whether to split.

**Verification status, stated honestly:** all four regression fixtures
requested for this change pass (mutually-exclusive-outcome contradiction →
blocker; the exact live-observed "Unrecognised Input Silently Dropped" case
caught by the fallback call even when the per-group call misses it;
invented endpoint/error code → `assumption`, not blocker; a fully grounded
group → zero findings), plus a hand-built structurally-broken-suite test
and a `PayloadTooHeavyError`-triggers-a-split test — 21 orchestrator tests
total, all passing against mocked providers. This is now also confirmed
live, on two providers. NVIDIA (`nemotron-3-ultra-550b-a55b`, run
`96d1dcff9eff`) hit its exact pre-redesign 504-on-identical-payload failure
twice mid-run — once on a 3-scenario group, once on the resulting
2-scenario half — and `_review_group_with_splitting` caught both, split,
retried, and finished the run clean (12 scenarios, 53 cases, review
passed). Groq (`llama-3.3-70b-versatile`, run `20a0e981df7d`) generated a
suite where the fallback call caught a real, unprompted never-drop-rule
violation — a case that silently dropped empty input instead of routing it
to quick note, directly contradicting the requirement's "never silently
dropped" clause — and correctly reported `passed: False`. That's the
fallback-call half of the redesign doing exactly the job it was built for,
against a real model's real output, not a fixture. This machine's RAM (7.8
GB total) still can't run a 12B+ local Ollama model as an alternative
verification path, but that's no longer relevant now that both cloud
providers verified directly. See `EVALS.md`'s "Reviewer redesign" section
for the full account.

## Registry AC/NFR ingestion: "grounded" means body + acceptance criteria + applicable NFRs, not body alone

The reviewer redesign's own live evidence produced three further findings.
A never-drop rule can be stated only in an acceptance criterion, not a
requirement's body — the golden PRD's own AC-12 ("If no active block
matches the description, action saves as a note, NOT silently dropped")
is invisible to a fallback check that only scans `description`/
`source_span`. A grounding check that never sees NFR/performance text
flags legitimately-specified values (a stated "< 3s" response time) as
invented, a false positive with a real cost — it teaches a reader to
distrust flagged findings generally. And the same behavior class (empty
input creating no entry) passed review on one live run and failed on
another, because the fallback rule was written for "unrecognised input"
and the case concerned an adjacent, unnamed class ("empty input") — an
honest spec ambiguity that the old prompt gave the model no instruction on
how to adjudicate, so the verdict depended on which model happened to run
it rather than on the rule's own text.

The fix extends the registry, not just the reviewer. `AcceptanceCriterion`
and `NonFunctionalRequirement` are new first-class schema objects;
`derive_ac_id`/`derive_nfr_id` are content-addressed the same way
`derive_requirement_id` already is (`sha256(parent_id : normalized_text)`),
so re-ingesting an unchanged document produces identical IDs, same as
requirements themselves. `RequirementRegistry.ingest()` backfills ACs onto
a pre-existing requirement when a re-ingestion candidate now supplies them
and the existing record doesn't have any yet, rather than requiring a full
registry reset to adopt the new schema. NFRs are either `global` (apply to
every requirement implicitly) or `scoped` (linked via `nfr_ids`, resolved
against requirement source-span matching, the same content-addressed way
requirement IDs are matched) — global NFRs are stored once in
`RegistryFile.nfrs`, never duplicated onto every requirement's own record;
`orchestrator._resolve_requirement_nfrs`/`_build_review_requirement`
resolve the applicable set at review-input-build time, when the reviewer
prompts are actually rendered, not at ingest time.

`_find_fallback_rule_requirements` — the Python heuristic that decides
which requirements are cheap enough to send to the dedicated fallback call
— now scans acceptance-criteria text with equal weight to body text.
`reviewer_fallback.md.j2` gained an explicit adjudication rule: a
potential violation touching an input class the rule's own text doesn't
explicitly name must be emitted as `assumption`-severity with an
ambiguity note naming both the rule's text and the open boundary question
— never a blocker. Blocker severity is reserved for cases that contradict
the rule's own unambiguous text. This directly encodes the empty-input
instability as a permanent rule rather than leaving it to vary by model.

**Verification status, stated honestly:** the mechanism — schema, ID
stability, registry linking, reviewer-prompt wiring, the adjudication rule
— is verified via 203 passing tests (18 new: three end-to-end fixtures
plus supporting Python-level unit tests, since a mocked LLM can prove the
mechanism carries the right data to the right place but can't demonstrate
live reasoning). Live re-ingestion against the golden PRD on NVIDIA
confirmed the NFR half directly — a first attempt correctly extracted 3
real global NFRs including the exact "< 3s" constraint from Problem #2's
own motivating example. It also surfaced a genuine, separate problem: AC
extraction breadth degraded sharply on the same live attempt (only 2 of
~12–14 requirements extracted, one AC captured but truncated to just its
label). Two follow-up attempts — one adding a detailed worked example to
the extraction prompt, one trimming it back down — produced first an
identical result and then an infrastructure failure (NVIDIA's own worker
pool exhausted, `503` / "Worker local total request limit reached
(32/32)," unrelated to payload size). This looks like the same underlying
failure shape that motivated splitting fill generation and review into
narrower per-item calls elsewhere in this project — one call being asked
to both find every requirement *and* correctly cross-reference every
acceptance criterion back to the right one — but that wasn't resolved this
round; it needs a clear provider queue and further controlled attempts,
not more retries against an exhausted endpoint. See `EVALS.md`'s "Registry
AC/NFR ingestion" section for the full account, including why GATE items
3–5 (rerun review against saved live-run cases, record token deltas) are
deferred until this is resolved.

## 503-body classification + deterministic AC extraction: two separate bugs, not one

The AC-extraction-breadth question was left open above. It turned out to
be two bugs, both diagnosed from the live evidence already gathered: an
error-classification defect that made the third live attempt look like a
payload-size problem when it wasn't, and an architectural one — asking a
single extraction call to both find every requirement and correctly
cross-reference every acceptance criterion — that a wording fix could
never have solved, because the golden PRDs' ACs never needed a model at
all.

**The classification bug.** NVIDIA's 503 body — `"ResourceExhausted:
Worker local total request limit reached (32/32)"` — describes its own
shared worker pool being full. The gateway-error handler treated every
502/503/504 identically regardless of body content, so this fell into the
same path as a genuinely-oversized-payload timeout and would have
prescribed splitting the request in half and firing two calls instead of
one — exactly backwards for capacity exhaustion, which needs fewer
concurrent requests, not more. Fixed by inspecting the body for exhaustion
patterns (`resourceexhausted`, `worker`, `request limit`, `capacity`,
`overloaded`, `quota`) BEFORE the existing payload-too-heavy counter ever
sees the response — a match gets a long, patient backoff (30s doubling,
capped at 5 minutes, 4 retries) and a distinct `ServerExhaustedError`,
never a split. An empty or unrelated 502/503/504 body still takes the
unchanged 2-retry `PayloadTooHeavyError` path — the fix is a body-content
gate in front of the existing logic, not a replacement of it, so the
502/503/504-is-systematic-not-transient reasoning from the reviewer
redesign stays exactly as it was for the cases it was built for.

**The AC-extraction bug.** The previous round's evidence already ruled out
"the prompt's wording was unclear" — a detailed worked example produced a
byte-for-byte identical, still-degraded result. The actual defect was
architectural: the golden PRDs number their acceptance criteria in a
predictable way (`AC-01 (R-02): ...`), machine-parseable at zero cost, and
the extraction call was never asked to do less than requirement bodies +
NFRs + acceptance criteria + correct cross-referencing all in one JSON
payload — the same shape of failure that motivated splitting fill
generation and review into narrower per-item calls elsewhere in this
project, just not recognized as the same failure shape until this round.
The fix: `ingestion/ac_parser.py`, a regex-based parser that is now the
PRIMARY path, handling `AC-01 (R-02): ...`, `AC-01:`, and `**AC-01**`-style
labels, multi-line entries (captured to the next label/heading/blank line,
never truncated), and linking each AC to its requirement either via an
explicit `(R-xx)` reference or, absent one, the nearest preceding
requirement-label line in the document — both resolved by matching the
document's own internal numbering against the already-extracted
candidates' `source_span`, since registry IDs don't exist yet at this
extraction stage. The main extraction prompt now explicitly tells the
model NOT to populate `acceptance_criteria` at all. A per-requirement LLM
fallback exists as the SECONDARY path — one small call per requirement
(that requirement's body + the AC section text, never the whole document)
— firing only when the parser finds an AC-like heading section with zero
parseable labels; a document with no AC section at all is treated as
genuinely having none, not a parsing failure, so ordinary PRDs without
acceptance criteria never spend an LLM call on this at all. A requirement
whose fallback call fails gets one retry; if that also fails it's marked
`ac_extraction_failed` on the persisted `Requirement` — distinguishing
"extraction broke" from "this requirement genuinely has no ACs," which
look identical as an empty list otherwise — and logged to
`ASSUMPTIONS.md`. ACs found but unlinkable to any requirement land in a
new `unassigned_acs` list on the registry itself, never silently dropped.

**Verification status:** Fixtures J (both halves — an exhaustion-pattern
body classifies as `ServerExhaustedError`; a plain empty-body 504 still
takes the unchanged `PayloadTooHeavyError` path, guarding against
over-broadening the new classification), K (a machine-labeled AC section
extracts fully via the parser alone — zero LLM calls, full multi-line text,
correct linkage via both inline reference and nearest-preceding-heading
grouping), and L (an unlabeled AC section correctly triggers the
per-requirement fallback, including the retry-then-`ac_extraction_failed`
path) all pass, 217 tests total. Live verification succeeded on NVIDIA —
but only after finding a third bug the mocked tests couldn't reach: the
parser's original line-anchored regex matching worked against a raw file
and against every hand-built test string, but silently found nothing
against the real `document_text` `extract_requirements` assembles, because
`ingestion/parsers.py` collapses each block's internal newlines into a
single space-joined line and prefixes it with `[anchor] ` — an entire
14-entry AC section arrives as one un-broken line with a bracket before
its own heading's `#`. Rewritten to match by absolute text position
(`re.finditer`, `\b`-bounded labels, no line anchoring) rather than by
line — a reminder that a parser's own unit tests passing proves it's
correct against the strings you thought to write, not against what the
real pipeline actually hands it; the second one needs an actual run. With
that fixed, live re-ingestion of the golden PRD on NVIDIA extracted 12/12
requirements and 14/14 acceptance criteria, correctly linked, in exactly 1
LLM call — a direct reversal of the previous round's 2/14-and-truncated
result, same document, same provider. See `EVALS.md`'s "503-body
classification fix + deterministic AC extraction" section for the full
account.

## Requirement-to-scenario completeness enforcement: closing the gap enumerate-then-fill never checked

Enumerate-then-fill's own completeness guarantee only ever checked one
thing: did every scenario test-architect enumerated get filled with cases.
It never checked whether test-architect's single enumeration call actually
produced a scenario for every requirement in the registry in the first
place — a live PocketBudget run measured 3 of 9 requirements (a ~33% miss
rate) getting zero scenarios, with `scenarios_failed` staying 0 the entire
time, because every scenario that WAS enumerated filled successfully. The
gap was upstream of every existing safety check. This is the exact same
"one call, whole list, model stamina runs out" failure shape that already
motivated splitting fill generation and semantic review into narrower
per-item calls — just discovered one layer higher, at enumeration itself,
where nothing had been watching for it yet.

**Change 1: scoped refill at the enumeration layer.** After the initial
test-architect call and the existing hallucinated-ID filter,
`_refill_missing_requirement_scenarios` diffs the full requirement registry
against `_requirement_ids_with_scenarios(scenarios)` and, for anything
missing, calls test-architect again — scoped to ONLY the missing
requirements' body, ACs, and NFRs, never the whole registry — up to
`_MAX_REQUIREMENT_REFILL_ATTEMPTS` (2) rounds, re-diffing after each round
so a requirement covered on attempt 1 doesn't get asked for again on
attempt 2. `test_architect.md.j2` needed no changes: its own instructions
("the requirements below," "every requirement below must be covered")
were already scoped to whatever's handed to it, never assuming totality of
the whole system, so the same prompt and agent definition serve both the
full-registry call and every scoped refill call unmodified. A requirement
still missing after all attempts is logged to `ASSUMPTIONS.md`
(`"{requirement_id} has no test scenarios — human attention required."`)
and to the ledger's `test-architect` agent-step metadata
(`requirements_total`, `requirements_with_scenarios`, `requirements_refilled`,
`requirements_failed`) — reported, never silently dropped.

**Change 2: an independent, Python-enforced zero-coverage alarm as the
last step before rendering.** Change 1 closes the gap at enumeration time,
but it doesn't cover every way a requirement could still end up with zero
cases in the FINAL suite — a scenario's own fill call can still exhaust its
retries and come back empty (the pre-existing `GENERATION_FAILED` path),
downstream filtering can still drop a case, or some as-yet-unknown failure
mode could reintroduce the gap some other way. `_check_zero_coverage_requirements`
scans the already-computed traceability matrix — zero LLM calls, since
Python already has `coverage_count` for free — and turns any zero-coverage
requirement into a blocker-severity `ReviewerFinding`
(`"{requirement_id} has zero test coverage in the final suite."`). It runs
unconditionally in `run_pipeline`, even when a structural blocker skipped
semantic and fallback review entirely, because it costs nothing either way
and a structural blocker says nothing about coverage. `_compose_review_report`
merges these into the same finding list as every other source; `passed`
is computed by the same "any blocker anywhere" rule that already governs
structural/semantic/fallback findings — no special-cased override, no
LLM-delegated judgment call. The CLI's `generate cases` command surfaces
it a second time as a standalone red warning line naming the affected
requirement IDs, not buried in a findings table a user might not scroll to.

**Python owns scenario IDs too — a bug the first live run of this very
fix surfaced.** Each refill round is its own isolated test-architect call,
and the model numbers that round's scenarios starting from `S-1` with no
visibility into what the initial enumeration already used. The first live
PocketBudget rerun on NVIDIA showed the consequence: a refill round's
`S-1` collided with the initial enumeration's real `S-1` inside
`_filter_cases_to_requirement_scope`'s `scenario_id`-keyed dict, silently
replacing it — so every case correctly filled for the original scenario
got validated against the wrong scenario's `requirement_ids` and dropped
as a "scope violation," zeroing out 3 requirements' coverage that had
been fine before the fix. `_renumber_case_ids` already existed for
exactly this reason at the case level; the same rule now applies one
level up via `_renumber_scenario_ids`, applied to the merged list before
`generate_strategy_only` returns, so no downstream `scenario_id`-keyed
lookup can ever collide. Notably, it was Change 2's independent
zero-coverage alarm that caught this — the bug raised no exception and
produced no structural finding, and without that final Python-enforced
check the broken suite would have rendered silently.

**Verification status:** Fixtures M (2 of 6 requirements missing at
enumeration time, refill covers both in its first attempt,
`requirements_refilled: 2`), N (1 requirement never gets a scenario across
the initial call or either refill attempt, `requirements_failed: 1`,
logged to `ASSUMPTIONS.md`), and O (a scenario's fill exhausts every retry
downstream of a successful enumeration — Change 1 has nothing to catch,
since the requirement DID get a scenario — and the zero-coverage alarm
forces `passed=False` in code with a blocker finding naming the
requirement, even though the LLM reviewer never ran against it) all pass,
plus a regression test reproducing the scenario-ID collision — 223 tests
total, ruff clean. Two pre-existing tests
(`test_functional_tester_prompt_only_includes_scenario_referenced_requirements`,
`test_run_pipeline`/`test_run_generate_cases`/`test_run_generate_strategy`
stage-progress assertions) needed updating, not because they were wrong,
but because they now correctly observe the new
`"test-architect (requirement coverage check)"` stage and, in one case, a
legitimately-fired refill call for a requirement the fixture deliberately
never referenced from any scenario. Live verification on NVIDIA
succeeded on both gate targets — PocketBudget: 9/9 requirements covered
(the original run's three misses now carry 4, 36, and 12 cases
respectively), refill fired once for 2 requirements and covered both;
golden PRD: 12/12 coverage retained with the refill correctly never
firing. Full account in `EVALS.md`'s "Requirement-to-scenario
completeness enforcement" section.

## Registry-level spec consistency pass: checking the spec against itself, not against the tests

Scoring the PocketBudget PRD against its own 9-item answer key found a
structural gap in what the pipeline checks at all: 0 of 9 planted defects
were fully caught, and the pattern was not random. Every existing check —
grounding, fallback-rule scanning, coverage, the reviewer's semantic
pass — compares a *generated test case* against the *spec*. Seven of the
nine plants are the spec contradicting *itself*: a requirement against
its own acceptance criterion (budget-reset timezone, alert-dedup window),
a requirement against a peer requirement (export column ambiguity,
new-user date-range gap), a requirement against an applicable NFR
(unlink-vs-GDPR retention), and a requirement against its own silence
(deleted-budget state, summary-generation failure path, override-conflict
resolution). No pipeline stage had ever read the registry against itself.

**New stage, same enumerate-and-check shape as everything else in this
project.** `consistency/pairs.py` deterministically enumerates four kinds
of comparison pairs from the already-ingested registry — zero LLM calls,
the same "Python owns the checklist" pattern as scenario/requirement
completeness enforcement:
- **req_vs_ac**: every requirement paired with each of its own acceptance
  criteria.
- **req_vs_nfr**: every requirement paired with every NFR that applies to
  it (global NFRs implicit to all; scoped NFRs via `nfr_ids`, mirroring
  the resolution rule the reviewer prompts already use).
- **req_vs_peer**: two requirements paired only when their combined
  body+AC text shares at least 2 distinctive tokens (stopwords and
  generic spec vocabulary excluded), and only among each requirement's
  top-5 highest-overlap neighbors. This is a bounded kNN filter, not
  exhaustive N-choose-2 pairing — an N-requirement PRD produces at most
  `5 * N` directed edges (fewer after deduplication into undirected
  pairs), not `N * (N-1) / 2`. Most requirement pairs share no
  distinctive vocabulary at all and would just be `consistent` no-ops
  burning tokens; the filter targets pairs a human reviewer would
  naturally think to compare.
- **req_vs_silence**: a Python keyword heuristic (does the requirement's
  own text, plus its applicable NFRs, name a state-changing action —
  generate/send/sync/export/delete/create/etc. — with no failure/error
  keyword anywhere in that combined text) flags candidates for a
  silence-gap check with no second text to compare against.

Each enumerated pair gets its own scoped LLM call (`consistency-checker`
agent, `prompts/consistency_checker.md.j2`) classifying it as
`contradiction`, `threshold_mismatch`, `silence_gap`, `modal_ambiguity`,
or `consistent` — never asking one call to hold the whole registry in
its head at once, the same reasoning that motivated splitting fill
generation, review, and scenario enumeration elsewhere in this codebase.
Contradictions and threshold mismatches are blocker-severity; silence
gaps and modal ambiguities are assumption-severity. All findings land in
`ASSUMPTIONS.md`; a pair-count budget guard (200 pairs) warns before
spending tokens on an unusually cross-referenced PRD, defaulting to
proceed since it exists for cost visibility, not as a hard limit.

**Where it runs, and why the CLI gets a new confirmation gate.** The
stage runs in `cli/actions.py` — after the registry is opened (every
requirement, AC, and NFR already ingested) and before
`generate_strategy_only`/`run_pipeline` ever calls test-architect —
rather than inside `agents/orchestrator.py`. This is a deliberately
separate question ("is the spec consistent with itself?") from
everything the orchestrator's pipeline stages check ("is this generated
test grounded in the spec?"), and keeping it in its own module means
`orchestrator.py` needed zero changes. The CLI shows a summary ("Found N
spec issue(s)...") and, if any exist, asks whether to proceed — declining
raises `GenerationAbortedError` before a single scenario is generated,
never silently swallowed.

**Modal-verb handling is split across two places, deliberately.**
Cross-text modal mismatches (a requirement says "should", its own AC says
"must" for what reads as the same behavior) are one of the five verdicts
the consistency-checker call can return, covering Damage 1/2-shaped
cross-text tensions. But Damage 4 and Damage 9 are a different shape
entirely — a *single* requirement using a weak modal ("should decline",
"should remember") with no second text to compare against at all. That
case is handled where it actually matters: the reviewer's own semantic
pass gained a third check (`prompts/reviewer.md.j2`) that flags a test
case as `assumption` when it treats a "should"-worded requirement's
behavior as mandatory — a judgment that can only be made once a
generated test case exists to check against the modal, which the
registry-level pass alone can't see.

**Verification status:** 239 tests total (up from 223), ruff clean.
Fixtures P (two peer requirements with mutually exclusive mandates on the
same entity → `contradiction`, blocker), Q (a requirement's own body says
"zero," its AC says "below ₹1" → `threshold_mismatch`, blocker), R (an
action with no failure/error handling anywhere → `silence_gap`,
assumption, no second text needed), S (a requirement says "should," its
own AC says "must" for the same behavior → `modal_ambiguity`, assumption),
and T (two requirements sharing a domain entity but not actually
conflicting → `consistent`, zero findings — the over-flagging guard) all
pass, plus dedicated pair-enumeration unit tests (token-overlap
threshold, top-5 neighbor capping, NFR resolution, silence-gap credit
from an applicable NFR) and both budget-guard directions (declining
aborts before any LLM call; accepting proceeds and reports the exact
pair count).

**Live verification: completed on NVIDIA after Groq's daily quota proved
too tight for two full pipeline runs plus a 55-68-pair consistency pass
on the same day's budget.** (A bounded hour-long retry loop confirmed
Groq's quota is a genuinely rolling 24-hour window, draining only ~5k
tokens/hour — real headroom was hours away, not minutes. Per this
project's standing rule against substituting an easier win for a blocked
gate item, this was reported as an open blocker rather than resolved
with a downgraded local-model run, until a replacement NVIDIA key
resolved it directly.)

**GATE 3 (golden PRD) result: not a clean zero, and that turned out to be
the interesting finding.** 10/10 requirement coverage held (no
regression), all 55 pairs completed, but 3 consistency findings came
back, not 0. Checked directly against the source document: all three are
real. `R-ecd42d32`'s body says TTS confirmation plays "after every
action" while its own AC narrows that to "successful actions" and adds a
silent-mode exception the body never mentions — genuine drift. `R-8bd5fc12`'s
body states an exact "2.8 seconds" while its own AC allows "2.8s (±0.3s)"
— a real tolerance-vs-exact-value mismatch. GATE 3's original framing
("any finding is a false positive, since the golden PRD has none")
doesn't survive contact with a real hand-authored spec: "golden" meant
well-specified and non-adversarial when it was written, not
adversarially checked for its own body-vs-AC consistency, which nothing
in this pipeline had ever done before this fix. Zero of the three
findings are fabricated — the checker's actual claim (no hallucinated
tensions) holds; the test's premise (a hand-authored spec has zero
internal drift) was the part that turned out to be wrong.

**GATE 2 (PocketBudget) result: 2 of 9 full, 1 of 9 partial, 6 of 9
miss** — real improvement over the pre-fix 0 full / 4 partial, short of
the ≥7/9 target. The two full catches (Damage 1: budget-reset timezone,
Damage 2: alert-dedup window) are exactly the req_vs_ac pair type's
purpose, landing with near-verbatim precision on both. One partial
(Damage 7, multi-currency) is a real contradiction caught via a different
pair (R-07's own body vs its own AC) than the one planted (R-06 vs R-07).
Of the six misses, the shortfall now has a precise diagnosis rather than
a vague one: three (3, 9, and 5's specific framing) are lifecycle/state
gaps genuinely outside what any of the four pair types were designed to
ask; one (6, unlink-vs-GDPR) is a confirmed model-verdict miss on a pair
that WAS correctly enumerated and checked (all 9 requirements × 5 global
NFRs = 45, matching exactly); and one (8, summary-generation failure
path) is a real, now-diagnosed bug: `req_vs_silence` enumerated **zero**
candidates across the entire 9-requirement registry, because
`_has_silence_gap` credits a requirement with "failure handling
specified" if a failure keyword appears ANYWHERE in its applicable NFR
text — and PocketBudget's one global NFR ("Sync failure: retry up to 3
times...") applies to every requirement implicitly, so its "retry"/"error"
keywords silently satisfied the check for R-09's summary-generation
action too, which has nothing to do with sync. The heuristic checks that
a failure keyword exists in scope, not that it's actually about the same
action — worth a follow-up fix (scope credit to NFRs sharing vocabulary
with the requirement's own action, not any global NFR regardless of
topic), explicitly out of this round's scope to change mid-verification.
Full scoring table in `EVALS.md`'s "Registry-level spec consistency
pass" section.

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

**Forward note (Release 2 plan revision):** `driftpin chat`'s slash-command
dispatch is planned to be replaced by `driftpin run` as the primary pipeline
entry point once Release 2 lands — see "Three honest CLI entry points"
below. `chat`'s underlying action layer (`cli/actions.py`) doesn't change;
what changes is the front-end shape and the addition of natural-language
routing ahead of it.

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

## Selenium added alongside Playwright as an automation target (Release 2)

The original plan scoped `automation-engineer` to Playwright only (Python or
TS, user's choice). Selenium is added as a second supported target at
explicit request. This is a scope amendment to Release 2, which isn't built
yet — no automation-engineer code exists at the time of this entry. The
framework choice becomes a third dimension the agent's config needs to carry
(alongside language and sink), the same way provider choice already is: one
more generation target, not a different architecture. Page Object Model,
the self-heal loop, and healing provenance apply identically regardless of
which framework produced the script — those are properties of the loop, not
of Playwright specifically. When automation-engineer is actually built, the
framework selection belongs in the same place execution_recommendation
already lives on a test case, so a case tagged `automate` carries which
framework it targets.

## Three honest CLI entry points, no mode-switching (Release 2 plan revision)

Release 2's original scope (self-healing automation, requirement triage) is
unchanged in substance but is now preceded by a restructuring of the CLI
itself, recorded here before any of it is built:

```
driftpin init  → setup wizard (already built, Release 1)
driftpin run   → transactional pipeline, deterministic stages, human controls sequencing
driftpin edit  → post-generation artifact mutation session, natural language in, modified artifacts out
```

`driftpin run` takes over as the primary pipeline entry point, replacing
`driftpin chat`'s slash-command dispatch for that job. On completion it asks
"Enter edit session? [y/N]" — accepting hands off directly to `driftpin
edit` with the current run's artifacts loaded; `--yes` skips the prompt for
CI. The three commands are three different *kinds* of interaction — setup,
pipeline execution, artifact mutation — not three flavors of the same loop
with different prompts. Naming them honestly (rather than folding
everything into one "smart" `chat` command that secretly branches on intent)
is deliberate: a user should be able to predict what a command does from its
name alone.

**Natural language intent routing** sits ahead of dispatch in interactive
mode, replacing the need to remember slash commands, but it is a
classifier, not a reasoning step: it returns `{action, confidence}` and nothing
else. Below a confidence threshold (0.75) it asks "Did you mean: run, edit,
status, export?" rather than guessing — silently routing a low-confidence
interpretation to the wrong action is worse than asking. Slash commands skip
classification entirely, so an interactive power-user typing `/cases` is
never slowed down by a routing step they didn't ask for. The classifier
runs on the cheapest available model (e.g. Groq `llama-3.1-8b-instant`) and
never shares call budget or ledger accounting with the pipeline agents —
routing "what did the user mean" is a fundamentally cheaper, different task
than generating a test strategy, and conflating their cost tracking would
make the ledger's token/cost numbers misleading.

## `driftpin edit` is an artifact mutation session, not an agentic loop (Release 2)

The model receives the current requirement registry, the current test case
list, and a natural-language instruction; it returns one structured
`EditOperation` (add/modify/remove, with `requirement_ids`, `steps`,
`execution_rec`, and a `reason`); Python validates it against the registry
and applies it. The user sees exactly what changed before it's written, and
approves every removal explicitly.

This is deliberate: an agent that autonomously rewrites test cases without
human checkpoints would be untraceable and unauditable for a QA tool — the
whole point of the requirement registry and the run ledger is that every
artifact's provenance is reconstructable, and an unsupervised agentic
edit loop breaks that the moment it decides on its own to add, remove, or
reinterpret a case. Natural language input does not imply agentic
behavior — those are different properties, and this system deliberately
has the first without the second. Concretely: unknown requirement IDs are
rejected before anything is applied, a removal always asks for confirmation
naming what it removes and which requirement it covered, an edit that would
leave any requirement at zero cases warns before applying, and a single
edit turn is capped at 10 operations — a mutation big enough to exceed that
gets broken into multiple instructions instead of one large, harder-to-audit
turn. Every edit turn — instruction text, operations applied, requirement
IDs touched, tokens used, model — lands in the ledger exactly like a
pipeline run does.

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
