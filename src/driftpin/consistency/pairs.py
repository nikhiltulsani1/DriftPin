"""Enumeration of spec-internal comparison pairs.

Every other check in this pipeline compares a generated test case against
the spec. This module answers a different question -- is the spec
consistent with ITSELF -- by enumerating kinds of pairs a human reviewer
would naturally compare: a requirement against its own acceptance
criteria, against its applicable NFRs, against a peer requirement sharing
a domain entity, and against its own silence on failure handling. Python
owns which pairs get checked; the LLM in `checker.py` only ever judges
pairs it's handed, never decides what's worth comparing.

Three of the four pair types (req_vs_ac, req_vs_nfr, req_vs_peer) are
fully deterministic and enumerated here with zero LLM calls. The fourth
(req_vs_silence) is only PARTIALLY deterministic: this module identifies
*candidates* (an action-describing requirement whose own text says
nothing about failure handling) and, where a candidate's only apparent
failure-handling coverage comes from a GLOBAL NFR (one that applies to
every requirement by keyword-blind default), flags that NFR as needing
an applicability judgment rather than crediting it automatically. A
requirement's own text or an explicit SCOPED NFR link still resolves the
candidate here, with zero LLM calls -- those are unambiguous. Resolving
the remaining candidates into actual pairs (or into "not a gap after
all") is `checker.py`'s job, via `driftpin.agents.runtime.run_agent`
against the `nfr-applicability` agent -- this module never calls a
provider directly. See `checker.py`'s module docstring for why keyword
presence alone was found to be too permissive live (PocketBudget's one
global "sync failure: retry..." NFR silently "covered" all nine
requirements, including ones with nothing to do with syncing).

Peer-requirement pairing is bounded by a token-overlap kNN filter, not
exhaustive N-choose-2 pairing: an N-requirement PRD would otherwise
produce O(N^2) peer pairs, most of which share no vocabulary at all and
would just be `consistent` no-ops burning tokens. Instead, each
requirement keeps only its `_MAX_PEERS_PER_REQUIREMENT` highest-overlap
neighbors (by shared distinctive token count), and the final pair set is
the union of both requirements' neighbor lists -- bounded to at most
`_MAX_PEERS_PER_REQUIREMENT * N` directed edges (deduplicated into fewer
undirected pairs in practice), instead of `N * (N-1) / 2`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from driftpin.schemas.consistency import ConsistencyPair, PairType
from driftpin.schemas.requirements import NfrScope, NonFunctionalRequirement, Requirement

_TOKEN_RE = re.compile(r"[a-z]+")
_MANDATORY_RE = re.compile(r"\bmust\b|\bshall\b", re.IGNORECASE)
_RECOMMENDED_RE = re.compile(r"\bshould\b", re.IGNORECASE)
_OPTIONAL_RE = re.compile(r"\bmay\b|\bcan\b", re.IGNORECASE)

# Generic spec vocabulary excluded from token-overlap scoring so two
# unrelated requirements don't "share a domain entity" merely because both
# say "the user must" or "the system should" -- only distinctive nouns and
# domain verbs count toward the shared-token threshold.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "not", "no", "is", "are", "was",
        "were", "be", "been", "being", "to", "of", "in", "on", "at", "for",
        "with", "by", "from", "as", "that", "this", "these", "those", "it",
        "its", "their", "user", "users", "app", "system", "must", "shall",
        "should", "may", "can", "will", "each", "every", "any", "all", "if",
        "then", "when", "than", "into", "up", "per", "also", "which", "who",
        "does", "doesn", "specified", "provide", "provides",
    }
)

_ACTION_KEYWORDS = (
    "generat", "send", "sync", "export", "delet", "creat", "process",
    "convert", "link", "unlink", "notif", "categoris", "categoriz",
)
_FAILURE_KEYWORDS = (
    "fail", "error", "retry", "unavailable", "cannot", "invalid",
    "timeout", "unable", "reject", "declin",
)

_MIN_SHARED_TOKENS = 2
_MAX_PEERS_PER_REQUIREMENT = 5


def extract_modal(text: str) -> str | None:
    """Strongest modal strength found in `text`: mandatory (must/shall)
    beats recommended (should) beats optional (may/can). `None` if no
    modal verb appears at all."""
    if _MANDATORY_RE.search(text):
        return "mandatory"
    if _RECOMMENDED_RE.search(text):
        return "recommended"
    if _OPTIONAL_RE.search(text):
        return "optional"
    return None


def _tokenize(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 2 and t not in _STOPWORDS}


def requirement_full_text(requirement: Requirement) -> str:
    parts = [requirement.description, *(ac.text for ac in requirement.acceptance_criteria)]
    return " ".join(parts)


def resolve_applicable_nfrs(
    requirement: Requirement, nfrs_by_id: dict[str, NonFunctionalRequirement]
) -> list[NonFunctionalRequirement]:
    """Global NFRs apply to every requirement implicitly; scoped NFRs apply
    only where `requirement.nfr_ids` links to them. Mirrors the resolution
    rule the reviewer prompts already use, kept as a small local copy here
    rather than importing the orchestrator's private helper, so this
    module has no dependency on the pipeline stage it runs ahead of."""
    applicable = [nfr for nfr in nfrs_by_id.values() if nfr.scope == NfrScope.GLOBAL]
    applicable += [nfrs_by_id[nid] for nid in requirement.nfr_ids if nid in nfrs_by_id]
    return applicable


def _has_action(requirement: Requirement) -> bool:
    return any(kw in requirement_full_text(requirement).lower() for kw in _ACTION_KEYWORDS)


def _has_failure_keyword(text: str) -> bool:
    return any(kw in text.lower() for kw in _FAILURE_KEYWORDS)


def build_silence_gap_pair(requirement: Requirement) -> ConsistencyPair:
    """The pair a resolved silence-gap candidate becomes. Kept here (not
    inline in `checker.py`) so the pair's shape stays defined next to the
    other pair-construction logic, even though deciding WHETHER to build
    one is `checker.py`'s job now."""
    return ConsistencyPair(
        pair_type=PairType.REQ_VS_SILENCE,
        req_id_1=requirement.requirement_id,
        req_id_2_or_ac_id_or_nfr_id=None,
        text_1=requirement_full_text(requirement),
        text_2="",
    )


@dataclass
class SilenceGapCandidate:
    """A requirement whose own text describes an action but never
    addresses failure handling for it, and whose only apparent coverage
    (if any) comes from GLOBAL NFRs whose applicability to THIS
    requirement's action hasn't been judged yet. `candidate_global_nfrs`
    is empty when nothing in scope even claims to cover failure handling
    -- that case needs no LLM judgment at all; the candidate is a
    definite gap. Non-empty means `checker.py` must ask, per NFR, "does
    this actually govern this requirement's failure modes" before
    deciding."""

    requirement: Requirement
    candidate_global_nfrs: list[NonFunctionalRequirement] = field(default_factory=list)


def find_silence_gap_candidates(
    requirements: list[Requirement], nfrs_by_id: dict[str, NonFunctionalRequirement]
) -> list[SilenceGapCandidate]:
    """Zero LLM calls. Filters to requirements that need a silence-gap
    judgment at all, and resolves as many as possible without a model:
    a requirement's own text (description + ACs) mentioning a failure
    keyword resolves it immediately (no gap), and so does an explicit
    SCOPED NFR link with a failure keyword -- a scoped link is a specific,
    human-curated association with this requirement, unlike a GLOBAL NFR,
    which applies to every requirement by default regardless of topic and
    therefore can't be trusted on keyword presence alone (see this
    module's docstring and `checker.py`'s for the live PocketBudget bug
    this replaces)."""
    candidates: list[SilenceGapCandidate] = []
    for requirement in requirements:
        if not _has_action(requirement):
            continue
        if _has_failure_keyword(requirement_full_text(requirement)):
            continue

        applicable_nfrs = resolve_applicable_nfrs(requirement, nfrs_by_id)
        scoped_ids = set(requirement.nfr_ids)
        scoped_failure_nfrs = [
            nfr for nfr in applicable_nfrs if nfr.nfr_id in scoped_ids and _has_failure_keyword(nfr.text)
        ]
        if scoped_failure_nfrs:
            continue

        global_failure_nfrs = [
            nfr
            for nfr in applicable_nfrs
            if nfr.scope == NfrScope.GLOBAL and _has_failure_keyword(nfr.text)
        ]
        candidates.append(SilenceGapCandidate(requirement=requirement, candidate_global_nfrs=global_failure_nfrs))
    return candidates


def enumerate_req_vs_ac_pairs(requirements: list[Requirement]) -> list[ConsistencyPair]:
    return [
        ConsistencyPair(
            pair_type=PairType.REQ_VS_AC,
            req_id_1=requirement.requirement_id,
            req_id_2_or_ac_id_or_nfr_id=ac.ac_id,
            text_1=requirement.description,
            text_2=ac.text,
        )
        for requirement in requirements
        for ac in requirement.acceptance_criteria
    ]


def enumerate_req_vs_nfr_pairs(
    requirements: list[Requirement], nfrs_by_id: dict[str, NonFunctionalRequirement]
) -> list[ConsistencyPair]:
    pairs: list[ConsistencyPair] = []
    for requirement in requirements:
        for nfr in resolve_applicable_nfrs(requirement, nfrs_by_id):
            pairs.append(
                ConsistencyPair(
                    pair_type=PairType.REQ_VS_NFR,
                    req_id_1=requirement.requirement_id,
                    req_id_2_or_ac_id_or_nfr_id=nfr.nfr_id,
                    text_1=requirement.description,
                    text_2=nfr.text,
                )
            )
    return pairs


def enumerate_req_vs_peer_pairs(requirements: list[Requirement]) -> list[ConsistencyPair]:
    if len(requirements) < 2:
        return []

    full_text_by_id = {r.requirement_id: requirement_full_text(r) for r in requirements}
    tokens_by_id = {rid: _tokenize(text) for rid, text in full_text_by_id.items()}

    edges: set[tuple[str, str]] = set()
    for req_id, tokens in tokens_by_id.items():
        scored: list[tuple[str, int]] = []
        for other_id, other_tokens in tokens_by_id.items():
            if other_id == req_id:
                continue
            overlap = len(tokens & other_tokens)
            if overlap >= _MIN_SHARED_TOKENS:
                scored.append((other_id, overlap))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        for other_id, _overlap in scored[:_MAX_PEERS_PER_REQUIREMENT]:
            edges.add(tuple(sorted((req_id, other_id))))  # type: ignore[arg-type]

    return [
        ConsistencyPair(
            pair_type=PairType.REQ_VS_PEER,
            req_id_1=id_a,
            req_id_2_or_ac_id_or_nfr_id=id_b,
            text_1=full_text_by_id[id_a],
            text_2=full_text_by_id[id_b],
        )
        for id_a, id_b in sorted(edges)
    ]


def enumerate_consistency_pairs(
    requirements: list[Requirement], nfrs: list[NonFunctionalRequirement]
) -> list[ConsistencyPair]:
    """The three fully-deterministic pair types only (req_vs_ac, req_vs_nfr,
    req_vs_peer). req_vs_silence pairs are NOT included here -- resolving a
    silence-gap candidate can require an LLM applicability judgment (see
    `find_silence_gap_candidates`), so `checker.py` computes those
    separately and merges them into its own final pair list."""
    nfrs_by_id = {nfr.nfr_id: nfr for nfr in nfrs}
    return [
        *enumerate_req_vs_ac_pairs(requirements),
        *enumerate_req_vs_nfr_pairs(requirements, nfrs_by_id),
        *enumerate_req_vs_peer_pairs(requirements),
    ]
