# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""
Governance Mandate Guard
========================

Purpose
-------
On-chain governance decides by token weight. Token weight measures popularity,
not legality inside the protocol's own framework. A proposal can win a vote and
still violate the charter the DAO ratified, or contradict a ruling the same DAO
issued six months earlier. Voters rarely have the time to cross-reference a
forty-page constitution and two years of governance history before a snapshot
closes, so the violation is usually discovered after execution.

This contract is the gate that runs before the vote. A DAO registers its
constitution once. Every proposal is then submitted here first, and GenLayer
validators independently fetch that constitution, read the DAO's own accumulated
case law, and rule on the proposal:

    COMPLIANT            the proposal may proceed to a token vote
    NON_COMPLIANT        the proposal conflicts with the charter or precedent
    AMENDMENT_REQUIRED   the intent is admissible but the text must be changed

The DAO's voting contract calls `is_compliant(proposal_id)` before it opens a
ballot. Only proposals that cleared the gate can be voted on.

The part that compounds is the precedent registry. Every adjudicated proposal,
accepted or rejected, is written into the DAO's precedent registry with a
one-line statement of the principle the ruling turned on. Later compliance
checks are handed that growing body of rulings as binding context. The registry
is per DAO, so each DAO accumulates its own case law, and a ruling made in
month two constrains the reading of a similar proposal in month twenty. That is
institutional memory a token vote cannot produce on its own.

Why GenLayer?
-------------
The question "does this proposal violate our constitution?" is not computable
by a deterministic EVM contract. The constitution is prose. The proposal is
prose. Deciding whether "the treasury may not allocate more than 10% of
reserves to a single counterparty in any quarter" is breached by "authorize a
strategic partnership tranche of 1.2M USDC to Acme Labs" requires reading both,
knowing the current reserve balance is discussed in the charter's definitions
section, and knowing whether an earlier ruling already interpreted "tranche" as
an allocation. A single oracle making that call is a single point of capture,
which is exactly what a constitutional gate cannot be.

GenLayer's Optimistic Democracy runs the reading on many validators at once.
Each validator fetches the constitution itself, receives the identical precedent
corpus from contract storage, and reaches its own conclusion. The contract's
equivalence principle decides whether those conclusions agree well enough to be
binding. No single validator's reading is authoritative, and a validator that
returns a different ruling forces the appeal path rather than silently passing.

Consensus Model
---------------
Two separate nondeterministic operations, each with a purpose-built
leader/validator pair through `gl.vm.run_nondet_unsafe`.

1. Charter pinning (`register_charter`, `ratify_amendment`)

   Leader: fetches the constitution URL, normalizes the document (markup
   stripped, whitespace collapsed, case folded) and returns the Keccak-256
   fingerprint of that normalized text plus its length.

   Validator: fetches the same URL itself and recomputes the fingerprint from
   its own copy. It agrees only on an exact fingerprint match.

   Exact-match is deliberate. The whole guarantee of the primitive is that a
   specific constitutional text was pinned and that every later ruling was made
   against that text. A URL whose bytes are not stable across a handful of
   near-simultaneous fetches is not a usable constitution source, and failing
   loudly at registration is better than failing silently at adjudication. DAOs
   should pin an immutable source: IPFS, Arweave, or a commit-pinned raw file.

2. Adjudication (`submit_proposal`, `appeal_ruling`)

   Leader: fetches the live constitution, compares its fingerprint against the
   pinned one to detect drift, then asks an LLM to rule on the proposal against
   the constitution text and the precedent corpus that deterministic code
   selected before the nondeterministic block began.

   Validator: reruns that entire function. It does not inspect the shape of the
   leader's answer or ask an LLM whether the leader looked reasonable. It
   performs its own fetch and its own reasoning and then compares decisions.

   Agreement requires all four of:

       ruling               COMPLIANT | NON_COMPLIANT | AMENDMENT_REQUIRED
       mandate_class        the charter provision the ruling turned on
       principle            the rule the ruling establishes as precedent
       constitution_drift   whether the live document still matches the pin

   `ruling` is what `is_compliant` returns, so it must be agreed. The mandate
   class is the ground of the decision and becomes the precedent's index, so
   two validators that reject a proposal for unrelated reasons are not treated
   as agreeing. Drift is agreed as a boolean rather than as a hash, because the
   boolean is stable even when a page carries a trivial dynamic element.

   The mandate class is drawn from a fixed eight-value taxonomy and is
   normalized on both sides through the same deterministic mapping, so a
   difference in phrasing never registers as a difference in judgment. When the
   ruling is COMPLIANT the class is forced to `none` on both sides: a passing
   proposal implicates no provision, and requiring agreement on a field that
   carries no decision would manufacture disagreement.

What later panels are allowed to read
------------------------------------
An adjudication also returns one prose sentence, the ruling principle, stating
the rule the panel understood its decision to turn on. That sentence is kept on
the ruling for the people who read the registry. It is not case law, and no
later panel is ever shown it.

The reason is measured rather than assumed. Five independent panels were given
the identical prompt, the identical pinned constitution and the identical
proposal. All five returned the same ruling and the same ground of decision,
and all five wrote a different rule: one capped a single grant, one capped a
calendar quarter, one added a review period as a condition, one dispensed with
an authorisation another required, and one named no figure at all. A per-grant
cap and a per-quarter cap decide five grants of 40,000 differently, so those
are different rules, not different wordings of one rule.

That rules out both ways of binding the sentence:

   1. Comparing the leader's sentence against one the validator authored
      itself. Honest panels disagree on the rule, so a majority never forms.
      Measured on chain: 1 agreement in 5 on every adjudication, while the
      methods that do not compare prose reached majority normally.

   2. Having each validator check the leader's sentence against the
      constitution it fetched itself. Each of those five rules is individually
      defensible against the text, so such a check admits any of them and the
      leader still chooses which one becomes binding. That is the same hole,
      reached by a different route.

Generalizing a rule from one decided case is underdetermined, which is why
courts argue about holding and dicta. So the case law is not authored at all.
`_render_holding` derives each precedent's holding from the three
consensus-bound fields plus deterministic on-chain data: the proposal's own
title and the charter version. Every validator would render byte-identical
text, and `principle_digest` commits to it and is mixed into the corpus digest.

A later panel therefore receives, for each prior case, the proposal that was
decided, the ruling it received and the ground it was decided on, and reasons
from that case to the one in front of it. That is what citing a precedent is,
and it carries no unchecked input.

The prose that is not bound is the prose no later panel reads. The ruling
`principle`, `rationale`, `constitution_clause`, `required_amendments` and
`cited_precedents` are reported for human review, never enter a corpus and
never gate anything, so validators may word them freely. `_corpus_entry` is the
complete list of what a later panel consumes, and a test holds it to being
bound or derived field by field.

Determinism boundary
--------------------
Everything that decides what the validators are asked is computed before the
nondeterministic block and captured in the closure that GenVM ships to every
validator: the proposal text, the pinned fingerprint, and the precedent corpus
with its own Keccak-256 digest. Validators cannot be handed a different corpus
than the leader, and the corpus digest is stored on the proposal so any later
reviewer can confirm which body of case law produced the ruling.

Nothing a panel reads is leader-authored and unchecked. Every field in a corpus
entry is either deterministic (the ids, the charter version, the landmark flag,
the pinned clock), consensus-bound (the ruling and the mandate class), or
derived by the contract from those two plus the proposal's own title (the
holding and its digest), so the case law that constrains a ruling in month
twenty was agreed by the panels that sat in month two.

Precedent selection is deterministic and bounded. Landmark rulings are taken
first, most recent first, then ordinary non-overruled rulings, most recent
first, up to a fixed corpus size. The backward scan is capped so that a DAO
with thousands of rulings still adjudicates in bounded gas, and landmarks are
kept in their own index so an important early ruling is never scrolled out of
reach by volume.

Contract Lifecycle
------------------
    register_charter      steward pins the constitution, DAO is now gated
    submit_proposal       adjudication runs, ruling stored, precedent written
    is_compliant          the DAO's voting contract reads the gate
    appeal_ruling         submitter contests once, re-adjudication supersedes
    mark_landmark         steward elevates a ruling to binding precedent
    overrule_precedent    steward retires a ruling from the corpus, on record
    ratify_amendment      steward re-pins after the constitution changes

The registry is multi-tenant. Any DAO on any chain registers its own charter and
gets its own isolated precedent registry, stewards, allowlist and counters.
"""

import json
import re
import typing

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from genlayer import *


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

# Unit separator. Never appears in URLs or human prose, so digest parts cannot
# be made to collide by moving text across a boundary.
DIGEST_SEP = "\x1f"

# Error classification prefixes. Validators compare leader failures against
# their own failures using these, so a business-logic rejection is agreed on
# while a model failure always forces validator rotation.
ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_TRANSIENT = "[TRANSIENT]"
ERROR_LLM = "[LLM_ERROR]"

# Three-valued ruling. Stored as str: storage cannot hold an Enum.
RULING_COMPLIANT = "COMPLIANT"
RULING_NON_COMPLIANT = "NON_COMPLIANT"
RULING_AMENDMENT_REQUIRED = "AMENDMENT_REQUIRED"
RULINGS = (RULING_COMPLIANT, RULING_NON_COMPLIANT, RULING_AMENDMENT_REQUIRED)

# Closed taxonomy of charter provisions a ruling can turn on. Listed in
# adjudication priority order: when a normalized reply matches more than one
# class, the earlier entry wins, so the mapping is a total function and both
# sides of the consensus derive the same class from the same reply.
MANDATE_PRECEDENT_CONFLICT = "precedent_conflict"
MANDATE_AMENDMENT = "amendment_mandate"
MANDATE_AUTHORITY = "authority_mandate"
MANDATE_TREASURY = "treasury_mandate"
MANDATE_RIGHTS = "rights_mandate"
MANDATE_SCOPE = "scope_mandate"
MANDATE_PROCEDURAL = "procedural_mandate"
MANDATE_NONE = "none"

MANDATE_CLASSES = (
    MANDATE_PRECEDENT_CONFLICT,
    MANDATE_AMENDMENT,
    MANDATE_AUTHORITY,
    MANDATE_TREASURY,
    MANDATE_RIGHTS,
    MANDATE_SCOPE,
    MANDATE_PROCEDURAL,
    MANDATE_NONE,
)

# Keyword routes for normalizing a free-text class name onto the taxonomy.
# Evaluated in MANDATE_CLASSES order, so the result never depends on dict order.
MANDATE_KEYWORDS: dict[str, tuple[str, ...]] = {
    MANDATE_PRECEDENT_CONFLICT: (
        "precedent", "prior ruling", "case law", "stare decisis", "contradict",
        "inconsistent with previous", "earlier decision",
    ),
    MANDATE_AMENDMENT: (
        "amendment", "amend the constitution", "constitutional change",
        "supermajority", "ratification", "charter change",
    ),
    MANDATE_AUTHORITY: (
        "authority", "authorisation", "authorization", "mandate exceeded",
        "ultra vires", "permission", "role", "delegation", "council power",
        "multisig", "signer",
    ),
    MANDATE_TREASURY: (
        "treasury", "budget", "spend", "allocation", "funding", "reserve",
        "grant", "disbursement", "financial", "emission", "buyback",
    ),
    MANDATE_RIGHTS: (
        "rights", "minority", "holder protection", "dilution", "expropriation",
        "vested", "redemption", "fair treatment",
    ),
    MANDATE_SCOPE: (
        "scope", "remit", "purpose", "mission", "out of scope", "charter purpose",
        "objectives",
    ),
    MANDATE_PROCEDURAL: (
        "procedural", "procedure", "process", "notice", "quorum", "timelock",
        "delay", "review period", "discussion period", "format", "template",
    ),
    MANDATE_NONE: ("none", "not applicable", "n/a", "no mandate", ""),
}

# Input bounds. Every one of these is enforced deterministically before any
# nondeterministic work is scheduled, so malformed input never costs an LLM call.
MAX_DAO_ID_LEN = 64
MAX_NAME_LEN = 120
MAX_URL_LEN = 512
MAX_TITLE_LEN = 200
MAX_BODY_LEN = 6000
MIN_BODY_LEN = 40
MAX_NOTE_LEN = 400
MAX_GROUNDS_LEN = 1200

# Cooldown bounds, in seconds. Zero is allowed and means no cooldown.
MAX_COOLDOWN_SECS = 2_592_000  # 30 days

# Precedent corpus shape.
MAX_CORPUS = 12          # rulings handed to validators for a single adjudication
MAX_CORPUS_LANDMARKS = 6 # of those, at most this many come from the landmark index
MAX_CORPUS_SCAN = 64     # bounded backward scan over ordinary precedents
MAX_LANDMARK_SCAN = 32   # bounded backward scan over the landmark index

# Field caps applied to model output before it reaches storage.
MAX_PRINCIPLE_LEN = 240
MAX_RATIONALE_LEN = 900
MAX_AMENDMENTS_LEN = 900
MAX_CLAUSE_LEN = 300
MAX_CITATIONS = 8

# Ruling principle bounds. The principle is the prose sentence a panel records
# beside its ruling. It is reported for human review and never read into a
# precedent corpus, so it is not consensus-bound; these bounds only keep the
# registry legible.
#
# A principle shorter than this cannot state a rule. "n/a", "none" and "ok" are
# the replies this rejects, and rejecting them is what keeps an empty line out
# of the registry.
MIN_PRINCIPLE_LEN = 12

# Document handling.
MAX_DOC_CHARS = 20000    # constitution text passed to the model
MAX_FETCH_BYTES = 400000 # hard cap on a fetched body before normalization

# A submitter may appeal a ruling on their own proposal once.
MAX_REVISIONS = 2


# ---------------------------------------------------------------------------
# Storage records
# ---------------------------------------------------------------------------

@allow_storage
@dataclass
class Charter:
    """
    A registered DAO and the constitution its proposals are measured against.

    `doc_fingerprint` is the Keccak-256 of the normalized constitution text as
    it stood when the charter was registered or last amended. It is what makes
    a ruling auditable: anyone can refetch the URL, normalize it the same way,
    and confirm whether the document a ruling was made against is still live.

    `version` starts at 1 and increments on every ratified amendment. It scopes
    the replay index, so a proposal rejected under version 3 may be resubmitted
    once the charter reaches version 4, which is the correct behaviour when the
    provision that rejected it has itself been changed.
    """
    dao_id: str
    display_name: str
    steward: Address
    registered_by: Address
    constitution_url: str
    url_digest: str
    doc_fingerprint: str
    doc_chars: u32
    version: u32
    ratified_at: u64
    amended_at: u64
    active: bool
    # When True, a live document that no longer matches the pin blocks
    # adjudication outright. When False, drift is recorded on the ruling and
    # surfaced through the views but does not stop the gate.
    enforce_pinning: bool
    # When True, only addresses on the charter's allowlist may submit.
    restrict_proposers: bool
    submission_cooldown_secs: u64
    proposal_total: u32
    precedent_total: u32
    landmark_total: u32
    compliant_total: u32
    non_compliant_total: u32
    amendment_required_total: u32


@allow_storage
@dataclass
class Proposal:
    """
    A proposal submitted to the gate, with the exact inputs its ruling used.

    `corpus_digest` and `corpus_size` record which precedents were in front of
    the validators. Together with `charter_version` they let a reviewer
    reconstruct the decision context of any past ruling.
    """
    proposal_id: str
    dao_id: str
    submitter: Address
    title: str
    body: str
    body_digest: str
    charter_version: u32
    submitted_at: u64
    corpus_digest: str
    corpus_size: u32
    revision: u32
    appealed: bool
    appeal_grounds: str
    appealed_at: u64


@allow_storage
@dataclass
class Ruling:
    """
    The adjudicated outcome of record for a proposal.

    `ruling`, `mandate_class` and `constitution_drift` are the consensus-bound
    fields: every validator that accepted this transaction independently
    reached the same ruling on the same ground against its own fetch of the
    constitution.

    `principle` is the sentence the panel recorded as the rule its decision
    turned on. It is the leader's, reported for human review. It is not bound
    and it is never read into a precedent corpus, so two validators may word it
    completely differently. `rationale`, `constitution_clause`,
    `required_amendments` and `cited_precedents` are unbound for the same
    reason.

    `principle_digest` commits to the derived holding this ruling generated,
    which is the text that did enter the registry, so a reviewer can tie a
    ruling to its case law without trusting either record separately.
    """
    proposal_id: str
    dao_id: str
    ruling: str
    mandate_class: str
    principle: str
    rationale: str
    required_amendments: str
    constitution_clause: str
    cited_precedents: str
    constitution_drift: bool
    live_fingerprint: str
    pinned_fingerprint: str
    charter_version: u32
    corpus_digest: str
    adjudicated_at: u64
    revision: u32
    precedent_id: str
    principle_digest: str


@allow_storage
@dataclass
class Precedent:
    """
    One entry in a DAO's accumulated case law.

    Every adjudication writes one of these, accepted or rejected, because a
    ruling that a proposal was permissible is as much a precedent as a ruling
    that it was not.

    This record is what later adjudications read, so nothing in it is authored
    by a leader. `principle` holds the derived holding `_render_holding` built
    out of the consensus-bound ruling, ground and drift flag plus the
    proposal's own title and the charter version, so every validator would
    render the identical string. The sentence a panel wrote in its own words
    stays on the `Ruling` record and is never copied here.

    `principle_digest` commits to that derived text and is mixed into the
    corpus digest, so the case law a past ruling was decided under is
    verifiable rather than asserted.

    Every other field is deterministic: the ids come from the DAO id and a
    counter, `ruling` and `mandate_class` are consensus-bound, the version
    comes from the charter, the timestamp from the pinned transaction clock,
    and the landmark and overrule fields only ever change through a steward
    call.
    """
    precedent_id: str
    dao_id: str
    proposal_id: str
    ruling: str
    mandate_class: str
    principle: str
    charter_version: u32
    created_at: u64
    landmark: bool
    overruled: bool
    overruled_reason: str
    overruled_at: u64
    superseded_by: str
    principle_digest: str


@allow_storage
@dataclass
class Amendment:
    """
    One ratified change to a DAO's constitution, kept as an audit trail so the
    document a historical ruling was made against can always be identified.
    """
    dao_id: str
    version: u32
    previous_url: str
    previous_fingerprint: str
    new_url: str
    new_fingerprint: str
    note: str
    ratified_at: u64
    ratified_by: Address


# ---------------------------------------------------------------------------
# Defensive parsing of model output
# ---------------------------------------------------------------------------

def _clean_json(raw: typing.Any) -> dict:
    """
    Coerce a model reply into a dict, tolerating the usual damage: a prose
    preamble, a fenced code block, or a trailing comma before a closing brace.

    A reply that cannot be recovered raises a classified LLM error rather than
    letting a `json` exception escape. That classification is what the validator
    error handler reads, and an unclassified exception would be compared as a VM
    error instead of forcing the validator rotation a bad reply must force.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        txt = raw.strip()
        first = txt.find("{")
        last = txt.rfind("}")
        if first >= 0 and last > first:
            txt = txt[first : last + 1]
        txt = re.sub(r",(?!\s*?[\{\[\"\'\w])", "", txt)
        try:
            loaded = json.loads(txt)
        except Exception:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Adjudication reply could not be parsed as JSON"
            )
        if isinstance(loaded, dict):
            return loaded
    raise gl.vm.UserError(f"{ERROR_LLM} Adjudication reply was not a JSON object")


def _first_present(payload: dict, *names: str) -> typing.Any:
    """
    Read the first key that is actually present. Models rename fields between
    runs, so every field read goes through an alias list rather than one key.
    """
    for name in names:
        if name in payload:
            return payload[name]
    return None


def _as_text(raw: typing.Any, limit: int) -> str:
    """Flatten a model field to a bounded single-line string."""
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(item) for item in raw)
    elif isinstance(raw, dict):
        raw = json.dumps(raw, sort_keys=True, ensure_ascii=False)
    return re.sub(r"\s+", " ", str(raw).strip())[:limit]


def _coerce_bool(raw: typing.Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    txt = str(raw).strip().lower()
    if txt in ("true", "yes", "y", "1", "compliant", "pass", "allowed"):
        return True
    return False


def _normalize_ruling(raw: typing.Any) -> str:
    """
    Map a model's verdict onto the three-valued ruling.

    Accepts the canonical values and the phrasings models actually emit. An
    unrecognized verdict is an LLM error rather than a silent default, because
    defaulting either way would decide a constitutional question by accident.
    """
    txt = re.sub(r"[^a-z_ ]+", " ", str(raw).strip().lower())
    txt = re.sub(r"\s+", " ", txt).strip()
    collapsed = txt.replace(" ", "_")

    if collapsed in ("compliant", "complies", "compliant_", "pass", "passes",
                     "permitted", "permissible", "allowed", "approve", "approved",
                     "no_violation", "consistent"):
        return RULING_COMPLIANT
    if collapsed in ("non_compliant", "noncompliant", "not_compliant", "violates",
                     "violation", "reject", "rejected", "prohibited", "forbidden",
                     "incompatible", "fail", "fails", "inconsistent"):
        return RULING_NON_COMPLIANT
    if collapsed in ("amendment_required", "amendments_required", "amend",
                     "amend_required", "needs_amendment", "requires_amendment",
                     "conditional", "conditionally_compliant", "revise",
                     "revision_required", "modify"):
        return RULING_AMENDMENT_REQUIRED

    # Fall back to substring routing before giving up, most specific first.
    if "amend" in collapsed or "revis" in collapsed or "conditional" in collapsed:
        return RULING_AMENDMENT_REQUIRED
    if "non" in collapsed or "not" in collapsed or "violat" in collapsed or "reject" in collapsed:
        return RULING_NON_COMPLIANT
    if "compliant" in collapsed or "permit" in collapsed or "approv" in collapsed:
        return RULING_COMPLIANT

    raise gl.vm.UserError(f"{ERROR_LLM} Adjudication returned an unrecognized ruling")


def _normalize_mandate_class(raw: typing.Any, ruling: str) -> str:
    """
    Map a free-text ground of decision onto the closed taxonomy.

    A COMPLIANT ruling always yields `none`. A compliant proposal implicates no
    provision, and forcing the class on both sides removes a field that carries
    no decision but could still disagree. For the two rejecting rulings the
    class is the actual ground and is consensus-bound.

    The scan runs in MANDATE_CLASSES order, which is priority order, so a reply
    mentioning both a treasury cap and a prior ruling classes as a precedent
    conflict on the leader and on every validator alike.
    """
    if ruling == RULING_COMPLIANT:
        return MANDATE_NONE

    txt = re.sub(r"\s+", " ", str(raw).strip().lower())

    for name in MANDATE_CLASSES:
        if txt == name:
            return name

    for name in MANDATE_CLASSES:
        if name == MANDATE_NONE:
            continue
        for keyword in MANDATE_KEYWORDS[name]:
            if keyword and keyword in txt:
                return name

    # A rejecting ruling with no identifiable ground defaults to scope: the
    # proposal was found outside what the charter permits, without a narrower
    # provision being named. Deterministic on both sides.
    return MANDATE_SCOPE


def _decision_fields(verdict: dict) -> tuple[str, str, bool]:
    """
    Extract the three closed-vocabulary consensus-bound fields.

    This is the single definition of "the outcome" and is called on the leader's
    reply and on the validator's own reply, so both sides are compared through
    identical normalization.

    These three are the whole of what consensus binds. Everything a later panel
    reads is either one of them or is derived from them by `_render_holding`,
    which is why the prose a panel writes never needs to be compared.
    """
    ruling = _normalize_ruling(
        _first_present(verdict, "ruling", "verdict", "decision", "result", "status")
    )
    mandate_class = _normalize_mandate_class(
        _first_present(verdict, "mandate_class", "mandate", "provision_class",
                       "ground", "category", "violation_class"),
        ruling,
    )
    drift = _coerce_bool(
        _first_present(verdict, "constitution_drift", "drift", "document_changed")
    )
    return ruling, mandate_class, drift


# ---------------------------------------------------------------------------
# Deterministic digests and normalization
# ---------------------------------------------------------------------------

def _digest(parts: list[str]) -> str:
    """
    Keccak-256 over unit-separator-joined parts. Every validator derives the
    same digest from the same inputs, which is what makes the replay index and
    the corpus commitment verifiable rather than advisory.
    """
    hasher = Keccak256()
    hasher.update(DIGEST_SEP.join(parts).encode("utf-8"))
    return hasher.hexdigest()


def _normalize_url(raw: str) -> str:
    """
    Canonical URL form, so cosmetic mutation cannot register the same document
    twice or evade the pinned-document check. Scheme and host are lower-cased,
    the fragment is dropped, a trailing slash is removed. The query is kept
    because it often selects the document version.
    """
    parts = urlsplit(raw.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _normalize_text(raw: str) -> str:
    """Case-folded, whitespace-collapsed text for replay digests."""
    return re.sub(r"\s+", " ", str(raw).strip().lower())


# ---------------------------------------------------------------------------
# The ruling principle, and what later panels are allowed to read
# ---------------------------------------------------------------------------
#
# Adjudication produces one prose sentence, the ruling principle, stating the
# rule the panel thought its decision turned on. It is recorded on the ruling
# because it is useful to a person reading the registry. It is not case law,
# and nothing in this contract treats it as case law.
#
# That is a measured decision, not an assumed one. Five independent panels were
# given the identical adjudication prompt, the identical pinned constitution
# and the identical proposal. All five returned the same ruling and the same
# ground of decision, and all five wrote a different rule: one capped a single
# grant, one capped a calendar quarter, one added a review period as a
# condition, one dispensed with an authorisation another required, and one
# named no figure at all. A per-grant cap and a per-quarter cap decide five
# grants of 40,000 differently, so those are different rules rather than
# different wordings of one rule.
#
# Generalizing a rule from one decided case is underdetermined, which is why
# courts argue about holding and dicta. A panel therefore cannot be asked to
# agree on the sentence. Nor can a validator be asked to approve the leader's
# sentence against the constitution: each of those five rules is individually
# defensible against the text, so a plausibility check admits any of them and
# the leader still chooses which one becomes binding. Both routes leave the
# same hole, which is a leader legislating alone.
#
# What later panels read is therefore derived, never authored. `_render_holding`
# builds a precedent's holding out of consensus-bound output and deterministic
# on-chain data only, so any two validators render byte-identical text from the
# same accepted payload. A later panel receives the proposal's own title as the
# facts and the bound ruling and ground as the outcome, and applies them by
# analogy. That is what citing a precedent is.


def _fold_principle(raw: typing.Any) -> str:
    """Lowercased, whitespace-collapsed text, punctuation intact."""
    return re.sub(r"\s+", " ", str(raw).strip().lower())


def _canonical_principle(raw: typing.Any) -> str:
    """
    The form of a principle that is stored and displayed.

    Whitespace is collapsed, wrapping quotes are removed and the result is
    bounded. Case and sentence punctuation are preserved, because the registry
    is read by people: a rule should end in a full stop.
    """
    text = _as_text(raw, MAX_PRINCIPLE_LEN)
    return text.strip().strip('"').strip("'").strip()


def _principle_key(raw: typing.Any) -> str:
    """
    Case folded, every run of non alphanumeric characters reduced to one space.

    This is what a stored digest commits to, so a reviewer can recompute the
    digest from the registry text without having to reproduce its punctuation.
    """
    folded = _fold_principle(raw)
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


def _principle_digest(raw: typing.Any) -> str:
    """Commitment to a canonical text."""
    return _digest(["principle", _principle_key(raw)])


def _render_holding(
    ruling: str,
    mandate_class: str,
    drift: bool,
    title: str,
    charter_version: int,
) -> str:
    """
    A precedent's holding, derived rather than authored.

    Every input is either consensus-bound or deterministic. `ruling`,
    `mandate_class` and `drift` are the three fields every validator compared
    exactly before voting to accept. `title` is the proposer's own text, already
    on chain and already bounded. `charter_version` is read from the charter.

    Nothing a leader wrote freely reaches this string, so two validators
    rendering it from the same accepted payload produce identical bytes, and the
    case law a later panel reads carries no unchecked input. This is the whole
    point of the function: it is the only source of a corpus holding.
    """
    ground = "" if mandate_class == MANDATE_NONE else f", on the ground of {mandate_class}"
    drift_note = ""
    if drift:
        drift_note = (" The live constitution had drifted from the ratified text"
                      " when this was decided.")
    clean_title = _as_text(title, MAX_TITLE_LEN) or "untitled proposal"
    return (f"Under charter v{int(charter_version)}, a proposal titled "
            f"\"{clean_title}\" was ruled {ruling}{ground}.{drift_note}")



def _normalize_document(raw: str) -> str:
    """
    Canonical form of a fetched constitution.

    Scripts and styles are removed whole, HTML comments and tags are stripped,
    the handful of entities that survive plain-text extraction are decoded,
    every whitespace run collapses to one space and the result is case folded.
    Two validators fetching the same served document therefore reach identical
    bytes even when the transport differs in line endings or indentation.
    """
    text = str(raw)
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _fingerprint_document(raw: str) -> tuple[str, int, str]:
    """
    Return (fingerprint, normalized length, normalized text) for a document.

    The fingerprint covers the normalized text and its length, so truncation
    at the transport layer cannot produce a matching fingerprint for a shorter
    document.
    """
    normalized = _normalize_document(raw)
    if not normalized:
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} Constitution document is empty after normalization")
    fingerprint = _digest(["constitution", str(len(normalized)), normalized])
    return fingerprint, len(normalized), normalized


def _fetch_document(url: str) -> str:
    """
    Fetch a document and classify every failure mode so the validator error
    handler can decide whether a leader failure is agreeable.

    A 4xx is the server's settled answer and is EXTERNAL: every validator will
    see it, so it is compared exactly. A 5xx or a transport failure is
    TRANSIENT: validators agree that the source was unreachable without
    requiring identical text, which is the correct treatment for an outage.
    """
    try:
        response = gl.nondet.web.get(url)
    except Exception as exc:
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} Constitution source unreachable: {type(exc).__name__}")

    status = int(getattr(response, "status", 0) or 0)
    if 400 <= status < 500:
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} Constitution source returned HTTP {status}")
    if status >= 500 or status == 0:
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} Constitution source returned HTTP {status}")

    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        text = body[:MAX_FETCH_BYTES].decode("utf-8", errors="replace")
    else:
        text = str(body)[:MAX_FETCH_BYTES]
    if not text.strip():
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} Constitution source returned an empty body")
    return text


def _handle_leader_error(leaders_res: typing.Any, leader_fn: typing.Callable[[], typing.Any]) -> bool:
    """
    Decide whether a validator agrees with a leader that failed.

    The validator runs the same work and compares failures by class. Business
    logic and settled external answers must match exactly. Two transient
    failures agree without matching text, because an outage does not produce
    identical messages everywhere. A model failure never agrees, which forces
    validator rotation instead of freezing a bad reply into a ruling.
    """
    leader_msg = getattr(leaders_res, "message", "") or ""
    try:
        leader_fn()
        return False
    except gl.vm.UserError as exc:
        validator_msg = getattr(exc, "message", None) or str(exc)
        if validator_msg.startswith(ERROR_EXPECTED) or validator_msg.startswith(ERROR_EXTERNAL):
            return validator_msg == leader_msg
        if validator_msg.startswith(ERROR_TRANSIENT) and leader_msg.startswith(ERROR_TRANSIENT):
            return True
        return False
    except Exception:
        return False


def _unwrap_nondet(result: typing.Any) -> dict:
    """
    Normalize the return of `run_nondet_unsafe` into a dict.

    The call yields a Lazy under the GenVM runner and the decoded value under
    some hosts, so both shapes are handled rather than assumed.
    """
    value = result
    if hasattr(value, "get") and not isinstance(value, dict):
        try:
            value = value.get()
        except TypeError:
            pass
    if isinstance(value, dict):
        return value
    return _clean_json(value)


def _leader_payload(leaders_res: typing.Any) -> dict | None:
    """Decode the leader's successful reply, or None if it is unusable."""
    if not isinstance(leaders_res, gl.vm.Return):
        return None
    raw = getattr(leaders_res, "calldata", None)
    if isinstance(raw, dict):
        return raw
    try:
        return _clean_json(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main contract
# ---------------------------------------------------------------------------

class GovernanceMandateGuard(gl.Contract):
    """
    Governance Mandate Guard, a pre-vote constitutional gate with a precedent
    registry. See the module docstring for the full design.

    Every index is a flat TreeMap under a composite string key rather than a
    nested collection, so every lookup is a single keyed read and no method
    scans the registry. The only bounded scans in the contract are the two
    precedent selection walks, whose limits are fixed constants.
    """

    # dao_id -> Charter
    charters: TreeMap[str, Charter]
    # str(index) -> dao_id, for enumeration without scanning
    charter_at: TreeMap[str, str]
    charter_total: u32

    # proposal_id -> Proposal
    proposals: TreeMap[str, Proposal]
    # proposal_id -> Ruling of record
    rulings: TreeMap[str, Ruling]
    # "<proposal_id>|<revision>" -> superseded Ruling, kept after an appeal
    ruling_history: TreeMap[str, Ruling]

    # precedent_id -> Precedent
    precedents: TreeMap[str, Precedent]

    # "<dao_id>|<index>" -> proposal_id
    dao_proposal_at: TreeMap[str, str]
    # "<dao_id>|<index>" -> precedent_id
    dao_precedent_at: TreeMap[str, str]
    # "<dao_id>|<index>" -> precedent_id, landmarks only
    dao_landmark_at: TreeMap[str, str]
    # "<dao_id>|<version>" -> Amendment
    dao_amendment_at: TreeMap[str, Amendment]

    # "<dao_id>|<version>|<body digest>" -> proposal_id, the replay index
    adjudicated_digest: TreeMap[str, str]
    # "<dao_id>|<lowercase address>" -> allowlisted
    proposer_allowed: TreeMap[str, bool]
    # "<dao_id>|<lowercase address>" -> last submission timestamp
    last_submission_at: TreeMap[str, u64]

    registry_name: str
    deployer: Address
    deployed_at: u64
    proposal_total: u32
    precedent_total: u32

    def __init__(self, registry_name: str) -> None:
        name = _as_text(registry_name, MAX_NAME_LEN)
        if not name:
            name = "Governance Mandate Guard"
        self.registry_name = name
        self.deployer = gl.message.sender_address
        self.deployed_at = self._now()
        self.charter_total = u32(0)
        self.proposal_total = u32(0)
        self.precedent_total = u32(0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise gl.vm.UserError(message)

    def _now(self) -> u64:
        """
        Transaction time as a Unix timestamp.

        GenVM pins the clock to the transaction's datetime, so this returns the
        same value for the leader and for every validator re-executing the
        method. That is what lets the cooldown gate be deterministic instead of
        a source of consensus failure. `gl.message_raw["datetime"]` carries the
        same value and is consulted only if the pinned clock is unreadable.
        """
        try:
            return u64(int(datetime.now(timezone.utc).timestamp()))
        except Exception:
            pass
        try:
            if hasattr(gl, "message_raw") and isinstance(gl.message_raw, dict) and "datetime" in gl.message_raw:
                txt = str(gl.message_raw["datetime"]).strip()
                if txt:
                    if txt.endswith("Z"):
                        txt = txt[:-1] + "+00:00"
                    parsed = datetime.fromisoformat(txt)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return u64(int(parsed.timestamp()))
        except Exception:
            pass
        # Fail closed. Timestamp 0 leaves every cooldown unexpired, so no
        # proposal is admitted on an unreadable clock.
        return u64(0)

    def _addr_key(self, address: Address) -> str:
        """Lower-case hex form of an address, for use inside composite keys."""
        return "0x" + address.as_bytes.hex()

    def _charter(self, dao_id: str) -> Charter:
        key = self._dao_key(dao_id)
        self._require(key in self.charters, f"{ERROR_EXPECTED} Unknown DAO: {key}")
        return self.charters[key]

    def _dao_key(self, dao_id: str) -> str:
        """
        Canonical DAO identifier: trimmed, lower-cased, whitespace collapsed to
        single hyphens. Registering "Acme DAO" and looking up "acme-dao" must
        resolve to the same charter.
        """
        text = re.sub(r"\s+", "-", str(dao_id).strip().lower())
        return re.sub(r"-{2,}", "-", text).strip("-")

    def _proposal_id(self, dao_id: str, index: int) -> str:
        return f"{dao_id}#p{index}"

    def _precedent_id(self, dao_id: str, index: int) -> str:
        return f"{dao_id}#r{index}"

    def _is_steward(self, charter: Charter) -> bool:
        return gl.message.sender_address == charter.steward

    def _require_steward(self, charter: Charter) -> None:
        self._require(
            self._is_steward(charter),
            f"{ERROR_EXPECTED} Only the charter steward may perform this action",
        )

    def _select_corpus(self, charter: Charter) -> list[dict]:
        """
        Choose the precedents this adjudication is bound by.

        Deterministic, bounded and run before any nondeterministic work, so the
        leader and every validator are handed byte-identical case law through
        the closure GenVM ships to them.

        Landmarks come first, newest first, up to MAX_CORPUS_LANDMARKS. The
        remaining slots are filled from ordinary precedents, newest first.
        Overruled entries are skipped everywhere. Both walks are capped, so a
        DAO with thousands of rulings still adjudicates in bounded gas, and the
        separate landmark index means an important early ruling is never pushed
        out of reach by later volume.
        """
        dao_id = str(charter.dao_id)
        selected: list[dict] = []
        seen: set[str] = set()

        landmark_total = int(charter.landmark_total)
        scanned = 0
        index = landmark_total - 1
        while index >= 0 and scanned < MAX_LANDMARK_SCAN and len(selected) < MAX_CORPUS_LANDMARKS:
            scanned += 1
            slot = f"{dao_id}|{index}"
            index -= 1
            if slot not in self.dao_landmark_at:
                continue
            precedent_id = str(self.dao_landmark_at[slot])
            if precedent_id in seen or precedent_id not in self.precedents:
                continue
            record = self.precedents[precedent_id]
            if bool(record.overruled) or not bool(record.landmark):
                continue
            seen.add(precedent_id)
            selected.append(self._corpus_entry(record, True))

        precedent_total = int(charter.precedent_total)
        scanned = 0
        index = precedent_total - 1
        while index >= 0 and scanned < MAX_CORPUS_SCAN and len(selected) < MAX_CORPUS:
            scanned += 1
            slot = f"{dao_id}|{index}"
            index -= 1
            if slot not in self.dao_precedent_at:
                continue
            precedent_id = str(self.dao_precedent_at[slot])
            if precedent_id in seen or precedent_id not in self.precedents:
                continue
            record = self.precedents[precedent_id]
            if bool(record.overruled):
                continue
            seen.add(precedent_id)
            selected.append(self._corpus_entry(record, bool(record.landmark)))

        return selected

    def _corpus_entry(self, record: Precedent, landmark: bool) -> dict:
        """
        One precedent as the adjudication sees it.

        This dict is the complete set of fields a later panel consumes, so every
        key in it must be consensus-bound or deterministic, with nothing
        leader-authored passing through unchecked:

            precedent_id      deterministic, DAO id plus a counter
            proposal_id       deterministic, DAO id plus a counter
            ruling            consensus-bound, exact
            mandate_class     consensus-bound, exact
            holding           derived by `_render_holding` from the bound
                              ruling, ground and drift flag plus the proposal's
                              own title and the charter version
            holding_digest    deterministic from the derived holding
            charter_version   deterministic, read from the charter
            landmark          deterministic, set only by a steward call
            decided_at        deterministic, the pinned transaction clock

        No key here carries prose a panel wrote freely. The sentence a panel
        records as its ruling principle lives on `Ruling` and stops there.

        `tests/test_consensus.py::test_every_corpus_field_is_bound_or_deterministic`
        holds this list to that promise, so a field added here later cannot
        quietly reintroduce an unbound input to future adjudications.
        """
        return {
            "precedent_id": str(record.precedent_id),
            "proposal_id": str(record.proposal_id),
            "ruling": str(record.ruling),
            "mandate_class": str(record.mandate_class),
            "holding": str(record.principle),
            "holding_digest": str(record.principle_digest),
            "charter_version": int(record.charter_version),
            "landmark": landmark,
            "decided_at": int(record.created_at),
        }

    def _corpus_digest(self, entries: list[dict], charter: Charter) -> str:
        """
        Commitment to the exact case law used by an adjudication.

        Stored on the proposal, so a reviewer reading a two-year-old ruling can
        confirm which precedents were in front of the validators rather than
        inferring it from the registry's current contents.
        """
        parts = [str(charter.dao_id), str(charter.version), str(len(entries))]
        for entry in entries:
            parts.append(entry["precedent_id"])
            parts.append(entry["ruling"])
            parts.append(entry["mandate_class"])
            parts.append(_normalize_text(entry["holding"]))
            # The digest of the derived holding, so the commitment covers the
            # case law as it was rendered and not only its current text.
            parts.append(entry["holding_digest"])
        return _digest(parts)

    def _render_corpus(self, entries: list[dict], current_version: int) -> str:
        """
        Render the corpus as the numbered case list the adjudication reads.

        Every line is derived: the ids, the bound ruling and ground, and the
        holding `_render_holding` built from them. No sentence a panel wrote
        freely appears here, which is what makes this text identical on every
        validator.

        Rulings made under a superseded charter version are labelled as such,
        because a precedent decided against different constitutional text is
        persuasive rather than binding, and the adjudication is told to weigh
        it that way.
        """
        if not entries:
            return ("No prior rulings exist for this DAO. This is the first proposal "
                    "adjudicated under its charter, so there is no case law to apply.")
        lines = []
        for position, entry in enumerate(entries, start=1):
            tags = []
            if entry["landmark"]:
                tags.append("LANDMARK, binding")
            if entry["charter_version"] != current_version:
                tags.append(f"decided under charter v{entry['charter_version']}, persuasive only")
            suffix = f" [{'; '.join(tags)}]" if tags else ""
            lines.append(
                f"{position}. {entry['precedent_id']} ruled {entry['ruling']}"
                f" on {entry['mandate_class']}{suffix}\n"
                f"   Holding: {entry['holding']}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Nondeterministic: pin a constitution document
    # ------------------------------------------------------------------

    def _pin_document(self, url: str) -> tuple[str, int]:
        """
        Fetch the constitution and agree its fingerprint across validators.

        Leader: fetches the URL and returns the Keccak-256 of the normalized
        document plus the normalized length.

        Validator: fetches the same URL itself and recomputes the fingerprint
        from its own copy. It agrees only on an exact match of both fields. It
        never inspects the leader's answer for plausibility, because a
        plausibility check would let a leader pin a document no other validator
        ever saw.

        Exact agreement is the point of the primitive. A constitution whose
        bytes are not stable across simultaneous fetches cannot support a
        verifiable claim that a ruling was made against a known text, and
        failing here is better than discovering it during an adjudication.
        """
        url_mem = str(url)

        def leader_fn() -> dict:
            fingerprint, length, _text = _fingerprint_document(_fetch_document(url_mem))
            return {"fingerprint": fingerprint, "doc_chars": length}

        def validator_fn(leaders_res: typing.Any) -> bool:
            payload = _leader_payload(leaders_res)
            if payload is None:
                return _handle_leader_error(leaders_res, leader_fn)
            try:
                mine = leader_fn()
            except Exception:
                return False
            return (
                str(payload.get("fingerprint", "")) == mine["fingerprint"]
                and int(payload.get("doc_chars", -1)) == mine["doc_chars"]
            )

        pinned = _unwrap_nondet(gl.vm.run_nondet_unsafe(leader_fn, validator_fn))

        fingerprint = str(pinned.get("fingerprint", "")).strip()
        self._require(
            len(fingerprint) == 64,
            f"{ERROR_EXPECTED} Constitution fingerprint was not produced",
        )
        try:
            doc_chars = int(pinned.get("doc_chars", 0))
        except Exception:
            doc_chars = 0
        self._require(
            doc_chars > 0,
            f"{ERROR_EXPECTED} Constitution document has no readable content",
        )
        return fingerprint, doc_chars

    def _validate_url(self, url: str) -> str:
        """Check and canonicalize a constitution URL before any fetch."""
        raw = str(url).strip()
        self._require(
            0 < len(raw) <= MAX_URL_LEN,
            f"{ERROR_EXPECTED} constitution_url must be 1 to {MAX_URL_LEN} characters",
        )
        normalized = _normalize_url(raw)
        parts = urlsplit(normalized)
        self._require(
            parts.scheme in ("http", "https"),
            f"{ERROR_EXPECTED} constitution_url must be an http or https URL",
        )
        self._require(
            bool(parts.netloc),
            f"{ERROR_EXPECTED} constitution_url must include a host",
        )
        return normalized

    # ------------------------------------------------------------------
    # Charter registration and maintenance
    # ------------------------------------------------------------------

    @gl.public.write
    def register_charter(
        self,
        dao_id: str,
        display_name: str,
        constitution_url: str,
        submission_cooldown_secs: int,
        restrict_proposers: bool,
        enforce_pinning: bool,
    ) -> str:
        """
        Register a DAO and pin the constitution its proposals are measured
        against. The caller becomes the charter steward.

        The document is fetched and fingerprinted under consensus during this
        call, so a charter cannot be registered against a URL that validators
        cannot agree on. Registration is the right place for that failure.

        `submission_cooldown_secs` throttles submissions per address, which
        matters because every submission spends an LLM call on every validator.
        `restrict_proposers` limits submissions to an allowlist the steward
        maintains. `enforce_pinning` decides whether a live document that has
        drifted from the pin blocks adjudication or is merely recorded.

        Returns a JSON object describing the registered charter.
        """
        key = self._dao_key(dao_id)
        self._require(
            0 < len(key) <= MAX_DAO_ID_LEN,
            f"{ERROR_EXPECTED} dao_id must be 1 to {MAX_DAO_ID_LEN} characters after normalization",
        )
        self._require(
            key not in self.charters,
            f"{ERROR_EXPECTED} DAO already registered: {key}",
        )

        name = _as_text(display_name, MAX_NAME_LEN)
        self._require(bool(name), f"{ERROR_EXPECTED} display_name must not be empty")

        url = self._validate_url(constitution_url)

        cooldown = int(submission_cooldown_secs)
        self._require(
            0 <= cooldown <= MAX_COOLDOWN_SECS,
            f"{ERROR_EXPECTED} submission_cooldown_secs must be between 0 and {MAX_COOLDOWN_SECS}",
        )

        sender = gl.message.sender_address
        now_ts = int(self._now())

        fingerprint, doc_chars = self._pin_document(url)

        index = int(self.charter_total)
        charter = Charter(
            dao_id=key,
            display_name=name,
            steward=sender,
            registered_by=sender,
            constitution_url=url,
            url_digest=_digest(["url", url]),
            doc_fingerprint=fingerprint,
            doc_chars=u32(doc_chars),
            version=u32(1),
            ratified_at=u64(now_ts),
            amended_at=u64(now_ts),
            active=True,
            enforce_pinning=bool(enforce_pinning),
            restrict_proposers=bool(restrict_proposers),
            submission_cooldown_secs=u64(cooldown),
            proposal_total=u32(0),
            precedent_total=u32(0),
            landmark_total=u32(0),
            compliant_total=u32(0),
            non_compliant_total=u32(0),
            amendment_required_total=u32(0),
        )
        self.charters[key] = charter
        self.charter_at[str(index)] = key
        self.charter_total = u32(index + 1)

        return json.dumps(
            {
                "dao_id": key,
                "display_name": name,
                "steward": self._addr_key(sender),
                "constitution_url": url,
                "doc_fingerprint": fingerprint,
                "doc_chars": doc_chars,
                "version": 1,
                "ratified_at": now_ts,
                "enforce_pinning": bool(enforce_pinning),
                "restrict_proposers": bool(restrict_proposers),
                "submission_cooldown_secs": cooldown,
                "charter_index": index,
            },
            sort_keys=True,
        )

    @gl.public.write
    def ratify_amendment(self, dao_id: str, constitution_url: str, note: str) -> str:
        """
        Re-pin the charter after the constitution has changed.

        The new document is fetched and fingerprinted under consensus exactly
        as at registration. The previous URL and fingerprint are written to the
        amendment log, so the text any historical ruling was made against stays
        identifiable forever.

        The charter version increments. Existing precedents are kept and stay
        readable, but they are labelled in later corpora as decided under a
        superseded version, and the replay index is scoped by version so a
        proposal rejected under the old text may be resubmitted under the new.

        Re-pinning the identical document is rejected: an amendment that
        changes nothing would rotate the version and silently reopen every
        previously rejected proposal.
        """
        charter = self._charter(dao_id)
        self._require_steward(charter)

        url = self._validate_url(constitution_url)
        note_text = _as_text(note, MAX_NOTE_LEN)
        self._require(
            bool(note_text),
            f"{ERROR_EXPECTED} An amendment note is required for the audit trail",
        )

        previous_url = str(charter.constitution_url)
        previous_fingerprint = str(charter.doc_fingerprint)

        fingerprint, doc_chars = self._pin_document(url)
        self._require(
            fingerprint != previous_fingerprint,
            f"{ERROR_EXPECTED} Document is identical to the current pin, nothing to ratify",
        )

        now_ts = int(self._now())
        new_version = int(charter.version) + 1

        charter.constitution_url = url
        charter.url_digest = _digest(["url", url])
        charter.doc_fingerprint = fingerprint
        charter.doc_chars = u32(doc_chars)
        charter.version = u32(new_version)
        charter.amended_at = u64(now_ts)
        self.charters[str(charter.dao_id)] = charter

        amendment = Amendment(
            dao_id=str(charter.dao_id),
            version=u32(new_version),
            previous_url=previous_url,
            previous_fingerprint=previous_fingerprint,
            new_url=url,
            new_fingerprint=fingerprint,
            note=note_text,
            ratified_at=u64(now_ts),
            ratified_by=gl.message.sender_address,
        )
        self.dao_amendment_at[f"{charter.dao_id}|{new_version}"] = amendment

        return json.dumps(
            {
                "dao_id": str(charter.dao_id),
                "version": new_version,
                "previous_url": previous_url,
                "previous_fingerprint": previous_fingerprint,
                "new_url": url,
                "new_fingerprint": fingerprint,
                "doc_chars": doc_chars,
                "note": note_text,
                "ratified_at": now_ts,
            },
            sort_keys=True,
        )

    @gl.public.write
    def transfer_stewardship(self, dao_id: str, new_steward: str) -> str:
        """
        Hand charter administration to another address. The steward controls
        amendments, the proposer allowlist, landmarking and overruling, so this
        is the single most consequential administrative call in the contract.
        """
        charter = self._charter(dao_id)
        self._require_steward(charter)

        try:
            target = Address(str(new_steward).strip())
        except Exception:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} new_steward is not a valid address")

        previous = self._addr_key(charter.steward)
        self._require(
            self._addr_key(target) != previous,
            f"{ERROR_EXPECTED} new_steward is already the steward",
        )

        charter.steward = target
        self.charters[str(charter.dao_id)] = charter

        return json.dumps(
            {
                "dao_id": str(charter.dao_id),
                "previous_steward": previous,
                "new_steward": self._addr_key(target),
            },
            sort_keys=True,
        )

    @gl.public.write
    def set_charter_active(self, dao_id: str, active: bool) -> str:
        """
        Suspend or resume the gate for a DAO.

        A suspended charter refuses new submissions and appeals. Rulings
        already issued stay readable and `is_compliant` keeps answering for
        them, because suspending the gate must not retroactively invalidate
        proposals that already cleared it.
        """
        charter = self._charter(dao_id)
        self._require_steward(charter)
        charter.active = bool(active)
        self.charters[str(charter.dao_id)] = charter
        return json.dumps(
            {"dao_id": str(charter.dao_id), "active": bool(active)}, sort_keys=True
        )

    @gl.public.write
    def set_proposer_allowed(self, dao_id: str, proposer: str, allowed: bool) -> str:
        """
        Add or remove an address from the charter's proposer allowlist. The
        allowlist is only consulted when the charter was registered with
        `restrict_proposers` set, but it can be populated beforehand.
        """
        charter = self._charter(dao_id)
        self._require_steward(charter)

        try:
            target = Address(str(proposer).strip())
        except Exception:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} proposer is not a valid address")

        slot = f"{charter.dao_id}|{self._addr_key(target)}"
        if allowed:
            self.proposer_allowed[slot] = True
        else:
            self.proposer_allowed[slot] = False

        return json.dumps(
            {
                "dao_id": str(charter.dao_id),
                "proposer": self._addr_key(target),
                "allowed": bool(allowed),
                "restrict_proposers": bool(charter.restrict_proposers),
            },
            sort_keys=True,
        )

    # ------------------------------------------------------------------
    # Nondeterministic: constitutional adjudication
    # ------------------------------------------------------------------

    def _adjudicate(
        self,
        charter: Charter,
        title: str,
        body: str,
        corpus_text: str,
        appeal_grounds: str,
    ) -> dict:
        """
        Rule on a proposal against the charter and the DAO's own case law.

        Everything the panel sees is captured into locals first, so the closure
        GenVM ships to every validator carries the identical constitution URL,
        pinned fingerprint, proposal text and precedent corpus. Validators
        cannot be handed different case law than the leader.

        Leader: fetches the live constitution, compares its fingerprint against
        the pin to detect drift, and asks an LLM to rule.

        Validator: reruns that whole function, including its own fetch and its
        own reasoning, then compares decisions. It never checks the shape of
        the leader's answer and never asks a model whether the leader looked
        reasonable, because either would let one validator's reading stand
        unchecked.

        Agreement requires three things to match exactly: the ruling, the
        mandate class and the drift flag.

        The ruling is what the gate returns. The mandate class is the ground of
        decision and becomes the precedent's index, so two validators rejecting
        a proposal for unrelated reasons do not count as agreeing. Drift is
        compared as a boolean rather than as a hash, which stays stable when a
        page carries a trivial dynamic element.

        Those three are enough to bind the case law because the case law is
        derived from them. `_render_holding` builds what later panels read out
        of this tuple plus deterministic on-chain data, so a leader cannot put
        a rule of its own into the registry: there is no field it authors that
        a later panel ever reads. See the commentary above `_render_holding`
        for the measurement that led here.

        The prose that is not bound is the prose no later panel reads. The
        ruling `principle`, `rationale`, `constitution_clause`,
        `required_amendments` and `cited_precedents` are reported for human
        review and never enter a corpus, so validators may word them freely.
        """
        dao_key_mem = str(charter.dao_id)
        dao_name_mem = str(charter.display_name)
        url_mem = str(charter.constitution_url)
        pinned_mem = str(charter.doc_fingerprint)
        version_mem = int(charter.version)
        enforce_mem = bool(charter.enforce_pinning)
        title_mem = str(title)
        body_mem = str(body)
        corpus_mem = str(corpus_text)
        grounds_mem = str(appeal_grounds)
        classes_mem = ", ".join(MANDATE_CLASSES)

        def leader_fn() -> dict:
            document = _fetch_document(url_mem)
            live_fingerprint, live_chars, live_text = _fingerprint_document(document)
            drift = live_fingerprint != pinned_mem

            if drift and enforce_mem:
                # Fail closed. A ruling made against a document that is no
                # longer the ratified text is not a constitutional ruling, so
                # the gate refuses rather than certifying against drift. The
                # message carries no fingerprint, because the DAO identifier is
                # enough to act on and every validator produces it identically,
                # which is what lets the error be agreed on.
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} Constitution at the registered URL no longer matches "
                    f"the pinned document for {dao_key_mem}; the steward must ratify an "
                    f"amendment before further proposals can be adjudicated"
                )

            constitution = live_text[:MAX_DOC_CHARS]
            drift_note = ""
            if drift:
                drift_note = (
                    "\nWARNING: the live document no longer matches the text this DAO "
                    "ratified. This charter permits adjudication to continue, and the "
                    "text above is the live document. Note any provision you rely on "
                    "that may post-date ratification.\n"
                )
            appeal_block = ""
            if grounds_mem:
                appeal_block = (
                    "APPEAL GROUNDS SUBMITTED BY THE PROPOSER\n"
                    "The proposer contests an earlier ruling on this proposal on the\n"
                    "following grounds. Weigh them on the merits. They are argument,\n"
                    "not evidence, and they do not bind you.\n"
                    f"{grounds_mem}\n"
                )

            prompt = f"""You are sitting as the constitutional review panel for {dao_name_mem}.

You are not deciding whether this proposal is a good idea, whether it is popular, or whether you agree with it. You are deciding one question: is this proposal permissible under the DAO's ratified constitution, as that constitution has been interpreted by the DAO's own prior rulings?
{drift_note}
CONSTITUTION (ratified text, charter version {version_mem}):
{constitution}

BINDING CASE LAW (this DAO's own prior rulings, most authoritative first):
{corpus_mem}

PROPOSAL UNDER REVIEW
Title: {title_mem}
Body:
{body_mem}

{appeal_block}
HOW TO DECIDE
1. Identify every constitutional provision the proposal touches. Quote the operative one.
2. Apply the case law. Each prior entry gives you the proposal that was decided, the ruling it received and the ground it was decided on. An entry marked LANDMARK is binding: if this proposal presents materially the same question as that one, rule the same way. An entry decided under a superseded charter version is persuasive only, so follow it unless the amended text now says otherwise. Reason from the decided proposal to this one; the entries state outcomes, not rules, and it is for you to say whether the question is the same.
3. Choose exactly one ruling:
   COMPLIANT - neither the constitution nor the case law prohibits this proposal. It may proceed to a token vote.
   NON_COMPLIANT - the proposal conflicts with a provision or with binding precedent, and rewording could not cure it because the substance itself is not permitted.
   AMENDMENT_REQUIRED - the substance is permissible, but the proposal as written breaches a provision that a change of wording would satisfy. You must then state the specific amendments.
4. Name the ground of decision, choosing exactly one value from this closed list: {classes_mem}. Use "none" only when the ruling is COMPLIANT.
5. State the ruling principle in one sentence: the rule you understand your decision to turn on. This is recorded beside your ruling for the people who read this DAO's registry. It is an explanatory note, not case law, and no later panel is shown it, so state it plainly and do not labour over the wording.

WHAT BINDS, so you know what to spend your care on
Your ruling and your ground of decision are compared against every other validator's, character for character, and the proposal is only decided if they match. Those two answers, and the drift flag, are the entire decision. The DAO's case law is generated from them by the contract itself, which is why your principle sentence does not need to match anyone else's and is not checked against anyone else's. Spend your care on getting the ruling and the ground right.
- Write the principle as one declarative sentence about the rule, not about this proposal.
- Do not hedge and do not give alternatives. One rule.

Rules of construction:
- The constitution governs. Where the proposal and the constitution conflict, the constitution wins.
- Silence is permission. If the constitution does not restrict something, the proposal is COMPLIANT. Do not infer restrictions from general principle or from what a prudent DAO would do.
- Prefer AMENDMENT_REQUIRED over NON_COMPLIANT whenever the defect is curable by wording.
- A proposal that would itself change the constitution is permissible only through the amendment procedure the constitution sets out. Judge it against that procedure.
- Quantities matter. If a provision sets a numeric limit, compare it against the number in the proposal and say which is larger.

Respond with a JSON object with exactly these fields:
{{
  "ruling": "COMPLIANT" or "NON_COMPLIANT" or "AMENDMENT_REQUIRED",
  "mandate_class": one value from the closed list above,
  "principle": "one sentence, at most 200 characters, phrased as a reusable rule",
  "constitution_clause": "the operative provision, quoted or closely paraphrased",
  "rationale": "concise reasoning, at most 200 words",
  "required_amendments": "the specific changes required, or an empty string unless the ruling is AMENDMENT_REQUIRED",
  "cited_precedents": ["the precedent ids you actually relied on"]
}}
"""

            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            parsed = _clean_json(raw)
            # Drift is contract-computed, never model-reported. Writing it into
            # the reply is what lets the validator compare its own fetch result
            # against the leader's through the same extraction path.
            parsed["constitution_drift"] = drift

            ruling, mandate_class, _drift = _decision_fields(parsed)

            # The principle is a consensus-bound field, so it is canonicalized
            # here, on both sides, before it is ever compared or stored.
            principle = _canonical_principle(
                _first_present(parsed, "principle", "ruling_principle", "holding", "rule")
            )
            if not principle:
                raise gl.vm.UserError(
                    f"{ERROR_LLM} Adjudication returned no ruling principle"
                )
            if len(principle) < MIN_PRINCIPLE_LEN or not _principle_key(principle):
                raise gl.vm.UserError(
                    f"{ERROR_LLM} Adjudication returned a ruling principle too short "
                    f"to state a rule"
                )

            required = _as_text(
                _first_present(parsed, "required_amendments", "amendments", "remedy"),
                MAX_AMENDMENTS_LEN,
            )
            if ruling == RULING_AMENDMENT_REQUIRED and not required:
                raise gl.vm.UserError(
                    f"{ERROR_LLM} An AMENDMENT_REQUIRED ruling must state the required amendments"
                )
            if ruling != RULING_AMENDMENT_REQUIRED:
                required = ""

            citations_raw = _first_present(parsed, "cited_precedents", "precedents", "citations")
            citations: list[str] = []
            if isinstance(citations_raw, (list, tuple)):
                for item in citations_raw:
                    text = _as_text(item, 64)
                    if text and text not in citations:
                        citations.append(text)
            elif citations_raw is not None:
                for item in str(citations_raw).split(","):
                    text = _as_text(item, 64)
                    if text and text not in citations:
                        citations.append(text)

            return {
                "ruling": ruling,
                "mandate_class": mandate_class,
                "constitution_drift": drift,
                "principle": principle,
                "rationale": _as_text(
                    _first_present(parsed, "rationale", "reasoning", "explanation"),
                    MAX_RATIONALE_LEN,
                ),
                "required_amendments": required,
                "constitution_clause": _as_text(
                    _first_present(parsed, "constitution_clause", "clause", "provision"),
                    MAX_CLAUSE_LEN,
                ),
                "cited_precedents": citations[:MAX_CITATIONS],
                "live_fingerprint": live_fingerprint,
                "live_chars": live_chars,
            }

        def validator_fn(leaders_res: typing.Any) -> bool:
            payload = _leader_payload(leaders_res)
            if payload is None:
                return _handle_leader_error(leaders_res, leader_fn)
            try:
                leader_decision = _decision_fields(payload)
            except Exception:
                return False
            try:
                own_decision = _decision_fields(leader_fn())
            except Exception:
                return False
            # The whole of the binding. Everything a later panel reads is
            # derived from this tuple by `_render_holding`, so agreeing on it
            # is agreeing on the case law.
            return leader_decision == own_decision

        verdict = _unwrap_nondet(gl.vm.run_nondet_unsafe(leader_fn, validator_fn))

        # Re-derive the consensus-bound fields deterministically from the
        # agreed reply, so what reaches storage is the normalized decision
        # rather than whatever shape the reply happened to have.
        ruling, mandate_class, drift = _decision_fields(verdict)
        self._require(
            ruling in RULINGS,
            f"{ERROR_EXPECTED} Adjudication produced an invalid ruling",
        )
        self._require(
            mandate_class in MANDATE_CLASSES,
            f"{ERROR_EXPECTED} Adjudication produced an invalid mandate class",
        )

        # Re-derive the principle through the same canonicalization both sides
        # ran, so what reaches the registry is the agreed rule in canonical
        # form rather than whatever shape the reply happened to arrive in. The
        # digest is recomputed here too, so the stored commitment is taken over
        # the stored text and a reviewer can check one against the other.
        principle = _canonical_principle(verdict.get("principle"))
        self._require(
            bool(principle),
            f"{ERROR_EXPECTED} Adjudication produced no ruling principle",
        )
        self._require(
            len(principle) >= MIN_PRINCIPLE_LEN and bool(_principle_key(principle)),
            f"{ERROR_EXPECTED} Adjudication produced a ruling principle too short "
            f"to state a rule",
        )

        citations = verdict.get("cited_precedents")
        if isinstance(citations, (list, tuple)):
            citation_text = ",".join(_as_text(item, 64) for item in citations if _as_text(item, 64))
        else:
            citation_text = _as_text(citations, 256)

        live_fingerprint = _as_text(verdict.get("live_fingerprint"), 64)

        # The case law, derived here from the three bound fields plus the
        # proposal's own title and the charter version. This runs outside the
        # nondeterministic block, on inputs every node holds identically, so it
        # is the same string everywhere. It is the only text a later panel
        # reads, which is what keeps leader prose out of the registry's
        # operative content.
        holding = _render_holding(ruling, mandate_class, bool(drift), title, charter.version)

        return {
            "ruling": ruling,
            "mandate_class": mandate_class,
            "constitution_drift": bool(drift),
            "holding": holding,
            "holding_digest": _principle_digest(holding),
            "principle": principle,
            "rationale": _as_text(verdict.get("rationale"), MAX_RATIONALE_LEN),
            "required_amendments": (
                _as_text(verdict.get("required_amendments"), MAX_AMENDMENTS_LEN)
                if ruling == RULING_AMENDMENT_REQUIRED
                else ""
            ),
            "constitution_clause": _as_text(verdict.get("constitution_clause"), MAX_CLAUSE_LEN),
            "cited_precedents": citation_text[:512],
            "live_fingerprint": live_fingerprint,
        }

    def _tally(self, charter: Charter, ruling: str, delta: int) -> None:
        """Adjust the charter's per-ruling counters. Used with -1 on appeal."""
        if ruling == RULING_COMPLIANT:
            charter.compliant_total = u32(max(0, int(charter.compliant_total) + delta))
        elif ruling == RULING_NON_COMPLIANT:
            charter.non_compliant_total = u32(max(0, int(charter.non_compliant_total) + delta))
        elif ruling == RULING_AMENDMENT_REQUIRED:
            charter.amendment_required_total = u32(
                max(0, int(charter.amendment_required_total) + delta)
            )

    def _write_precedent(
        self,
        charter: Charter,
        proposal_id: str,
        verdict: dict,
        now_ts: int,
    ) -> str:
        """
        Record the ruling in the DAO's precedent registry.

        Every adjudication writes one entry, whichever way it went. A ruling
        that a proposal was permissible constrains later readings exactly as a
        rejection does, and a registry that only kept rejections would teach
        future panels that nothing has ever been allowed.

        `principle` here is the derived holding, not the sentence the panel
        wrote. The panel's sentence stays on the `Ruling` record, which is the
        only place it is kept, because this record is what later panels read.
        """
        dao_key = str(charter.dao_id)
        index = int(charter.precedent_total)
        precedent_id = self._precedent_id(dao_key, index)

        record = Precedent(
            precedent_id=precedent_id,
            dao_id=dao_key,
            proposal_id=proposal_id,
            ruling=str(verdict["ruling"]),
            mandate_class=str(verdict["mandate_class"]),
            principle=str(verdict["holding"]),
            charter_version=u32(int(charter.version)),
            created_at=u64(now_ts),
            landmark=False,
            overruled=False,
            overruled_reason="",
            overruled_at=u64(0),
            superseded_by="",
            principle_digest=str(verdict["holding_digest"]),
        )
        self.precedents[precedent_id] = record
        self.dao_precedent_at[f"{dao_key}|{index}"] = precedent_id
        charter.precedent_total = u32(index + 1)
        self.precedent_total = u32(int(self.precedent_total) + 1)
        return precedent_id

    # ------------------------------------------------------------------
    # The gate: submit a proposal for constitutional review
    # ------------------------------------------------------------------

    @gl.public.write
    def submit_proposal(self, dao_id: str, title: str, body: str) -> str:
        """
        Submit a proposal for constitutional review before it goes to a vote.

        Order of operations matters. Every deterministic check runs first, so a
        malformed, duplicated or rate-limited submission is rejected without
        spending an LLM call on every validator. Only then is the precedent
        corpus selected and the adjudication scheduled.

        The ruling is stored, the precedent is written, and the DAO's voting
        contract can now call `is_compliant(proposal_id)` before opening a
        ballot.

        Returns a JSON object with the proposal id, the ruling, the mandate
        class, the precedent id and the reasoning.
        """
        charter = self._charter(dao_id)
        dao_key = str(charter.dao_id)
        self._require(
            bool(charter.active),
            f"{ERROR_EXPECTED} Charter is suspended for {dao_key}",
        )

        sender = gl.message.sender_address
        sender_key = self._addr_key(sender)

        if bool(charter.restrict_proposers):
            slot = f"{dao_key}|{sender_key}"
            allowed = bool(self.proposer_allowed[slot]) if slot in self.proposer_allowed else False
            self._require(
                allowed,
                f"{ERROR_EXPECTED} Address is not on the proposer allowlist for {dao_key}",
            )

        title_text = _as_text(title, MAX_TITLE_LEN)
        self._require(
            bool(title_text),
            f"{ERROR_EXPECTED} title must not be empty",
        )

        body_text = str(body).strip()
        self._require(
            len(body_text) >= MIN_BODY_LEN,
            f"{ERROR_EXPECTED} body must be at least {MIN_BODY_LEN} characters",
        )
        self._require(
            len(body_text) <= MAX_BODY_LEN,
            f"{ERROR_EXPECTED} body must be at most {MAX_BODY_LEN} characters",
        )

        now_ts = int(self._now())
        cooldown = int(charter.submission_cooldown_secs)
        if cooldown > 0:
            cooldown_slot = f"{dao_key}|{sender_key}"
            if cooldown_slot in self.last_submission_at:
                last = int(self.last_submission_at[cooldown_slot])
                self._require(
                    now_ts >= last + cooldown,
                    f"{ERROR_EXPECTED} Submission cooldown of {cooldown}s has not elapsed for this address",
                )

        # Replay index. Scoped by charter version, because a proposal rejected
        # under one constitutional text is a genuinely new question once that
        # text has been amended.
        body_digest = _digest(["proposal", _normalize_text(title_text), _normalize_text(body_text)])
        digest_slot = f"{dao_key}|{int(charter.version)}|{body_digest}"
        if digest_slot in self.adjudicated_digest:
            existing = str(self.adjudicated_digest[digest_slot])
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} An identical proposal was already adjudicated "
                f"under charter version {int(charter.version)}: {existing}"
            )

        corpus_entries = self._select_corpus(charter)
        corpus_digest = self._corpus_digest(corpus_entries, charter)
        corpus_text = self._render_corpus(corpus_entries, int(charter.version))

        verdict = self._adjudicate(charter, title_text, body_text, corpus_text, "")

        index = int(charter.proposal_total)
        proposal_id = self._proposal_id(dao_key, index)

        proposal = Proposal(
            proposal_id=proposal_id,
            dao_id=dao_key,
            submitter=sender,
            title=title_text,
            body=body_text,
            body_digest=body_digest,
            charter_version=u32(int(charter.version)),
            submitted_at=u64(now_ts),
            corpus_digest=corpus_digest,
            corpus_size=u32(len(corpus_entries)),
            revision=u32(1),
            appealed=False,
            appeal_grounds="",
            appealed_at=u64(0),
        )
        self.proposals[proposal_id] = proposal
        self.dao_proposal_at[f"{dao_key}|{index}"] = proposal_id
        charter.proposal_total = u32(index + 1)
        self.proposal_total = u32(int(self.proposal_total) + 1)

        precedent_id = self._write_precedent(charter, proposal_id, verdict, now_ts)

        ruling = Ruling(
            proposal_id=proposal_id,
            dao_id=dao_key,
            ruling=str(verdict["ruling"]),
            mandate_class=str(verdict["mandate_class"]),
            principle=str(verdict["principle"]),
            rationale=str(verdict["rationale"]),
            required_amendments=str(verdict["required_amendments"]),
            constitution_clause=str(verdict["constitution_clause"]),
            cited_precedents=str(verdict["cited_precedents"]),
            constitution_drift=bool(verdict["constitution_drift"]),
            live_fingerprint=str(verdict["live_fingerprint"]),
            pinned_fingerprint=str(charter.doc_fingerprint),
            charter_version=u32(int(charter.version)),
            corpus_digest=corpus_digest,
            adjudicated_at=u64(now_ts),
            revision=u32(1),
            precedent_id=precedent_id,
            principle_digest=str(verdict["holding_digest"]),
        )
        self.rulings[proposal_id] = ruling

        self._tally(charter, str(verdict["ruling"]), 1)
        self.charters[dao_key] = charter

        self.adjudicated_digest[digest_slot] = proposal_id
        self.last_submission_at[f"{dao_key}|{sender_key}"] = u64(now_ts)

        return json.dumps(
            {
                "proposal_id": proposal_id,
                "dao_id": dao_key,
                "submitter": sender_key,
                "ruling": str(verdict["ruling"]),
                "is_compliant": str(verdict["ruling"]) == RULING_COMPLIANT,
                "mandate_class": str(verdict["mandate_class"]),
                "principle": str(verdict["principle"]),
                "holding": str(verdict["holding"]),
                "holding_digest": str(verdict["holding_digest"]),
                "rationale": str(verdict["rationale"]),
                "required_amendments": str(verdict["required_amendments"]),
                "constitution_clause": str(verdict["constitution_clause"]),
                "cited_precedents": str(verdict["cited_precedents"]),
                "constitution_drift": bool(verdict["constitution_drift"]),
                "precedent_id": precedent_id,
                "charter_version": int(charter.version),
                "corpus_digest": corpus_digest,
                "corpus_size": len(corpus_entries),
                "body_digest": body_digest,
                "revision": 1,
                "adjudicated_at": now_ts,
            },
            sort_keys=True,
        )

    # ------------------------------------------------------------------
    # Appeal: one re-adjudication, on the proposer's stated grounds
    # ------------------------------------------------------------------

    @gl.public.write
    def appeal_ruling(self, proposal_id: str, grounds: str) -> str:
        """
        Contest the ruling on your own proposal once.

        The appeal is a full re-adjudication rather than a review of the first
        one. The proposer's grounds are put to the panel as argument, and the
        panel fetches the constitution and reasons again from scratch.

        The precedent written by the original ruling is retired before the new
        corpus is selected, so an appeal is never bound by the very ruling it
        contests. The original ruling is kept in `ruling_history`, the retired
        precedent stays readable, and both record what superseded them.

        Only the original submitter may appeal, and only once.
        """
        pid = str(proposal_id).strip()
        self._require(
            pid in self.proposals,
            f"{ERROR_EXPECTED} Unknown proposal: {pid}",
        )
        proposal = self.proposals[pid]
        self._require(
            gl.message.sender_address == proposal.submitter,
            f"{ERROR_EXPECTED} Only the original submitter may appeal this ruling",
        )
        self._require(
            not bool(proposal.appealed),
            f"{ERROR_EXPECTED} This ruling has already been appealed",
        )
        self._require(
            int(proposal.revision) < MAX_REVISIONS,
            f"{ERROR_EXPECTED} This proposal has exhausted its appeals",
        )

        grounds_text = _as_text(grounds, MAX_GROUNDS_LEN)
        self._require(
            len(grounds_text) >= 20,
            f"{ERROR_EXPECTED} Appeal grounds must be at least 20 characters",
        )

        charter = self._charter(str(proposal.dao_id))
        dao_key = str(charter.dao_id)
        self._require(
            bool(charter.active),
            f"{ERROR_EXPECTED} Charter is suspended for {dao_key}",
        )
        self._require(
            pid in self.rulings,
            f"{ERROR_EXPECTED} Proposal has no ruling of record: {pid}",
        )

        previous = self.rulings[pid]
        previous_revision = int(previous.revision)
        previous_ruling = str(previous.ruling)
        previous_precedent_id = str(previous.precedent_id)
        now_ts = int(self._now())

        # Keep the superseded ruling addressable before it is replaced.
        self.ruling_history[f"{pid}|{previous_revision}"] = previous

        # Retire the precedent the contested ruling created, before the corpus
        # for the re-hearing is selected.
        if previous_precedent_id in self.precedents:
            retired = self.precedents[previous_precedent_id]
            retired.overruled = True
            retired.overruled_reason = "Superseded on appeal by the proposer"
            retired.overruled_at = u64(now_ts)
            self.precedents[previous_precedent_id] = retired

        corpus_entries = self._select_corpus(charter)
        corpus_digest = self._corpus_digest(corpus_entries, charter)
        corpus_text = self._render_corpus(corpus_entries, int(charter.version))

        verdict = self._adjudicate(
            charter,
            str(proposal.title),
            str(proposal.body),
            corpus_text,
            grounds_text,
        )

        new_revision = previous_revision + 1
        precedent_id = self._write_precedent(charter, pid, verdict, now_ts)

        if previous_precedent_id in self.precedents:
            retired = self.precedents[previous_precedent_id]
            retired.superseded_by = precedent_id
            self.precedents[previous_precedent_id] = retired

        ruling = Ruling(
            proposal_id=pid,
            dao_id=dao_key,
            ruling=str(verdict["ruling"]),
            mandate_class=str(verdict["mandate_class"]),
            principle=str(verdict["principle"]),
            rationale=str(verdict["rationale"]),
            required_amendments=str(verdict["required_amendments"]),
            constitution_clause=str(verdict["constitution_clause"]),
            cited_precedents=str(verdict["cited_precedents"]),
            constitution_drift=bool(verdict["constitution_drift"]),
            live_fingerprint=str(verdict["live_fingerprint"]),
            pinned_fingerprint=str(charter.doc_fingerprint),
            charter_version=u32(int(charter.version)),
            corpus_digest=corpus_digest,
            adjudicated_at=u64(now_ts),
            revision=u32(new_revision),
            precedent_id=precedent_id,
            principle_digest=str(verdict["holding_digest"]),
        )
        self.rulings[pid] = ruling

        proposal.appealed = True
        proposal.appeal_grounds = grounds_text
        proposal.appealed_at = u64(now_ts)
        proposal.revision = u32(new_revision)
        proposal.corpus_digest = corpus_digest
        proposal.corpus_size = u32(len(corpus_entries))
        self.proposals[pid] = proposal

        # The appeal replaces the original outcome, so the original is removed
        # from the tally and the new one is counted.
        self._tally(charter, previous_ruling, -1)
        self._tally(charter, str(verdict["ruling"]), 1)
        self.charters[dao_key] = charter

        return json.dumps(
            {
                "proposal_id": pid,
                "dao_id": dao_key,
                "revision": new_revision,
                "previous_ruling": previous_ruling,
                "previous_precedent_id": previous_precedent_id,
                "ruling": str(verdict["ruling"]),
                "is_compliant": str(verdict["ruling"]) == RULING_COMPLIANT,
                "ruling_changed": str(verdict["ruling"]) != previous_ruling,
                "mandate_class": str(verdict["mandate_class"]),
                "principle": str(verdict["principle"]),
                "holding": str(verdict["holding"]),
                "holding_digest": str(verdict["holding_digest"]),
                "rationale": str(verdict["rationale"]),
                "required_amendments": str(verdict["required_amendments"]),
                "constitution_clause": str(verdict["constitution_clause"]),
                "cited_precedents": str(verdict["cited_precedents"]),
                "constitution_drift": bool(verdict["constitution_drift"]),
                "precedent_id": precedent_id,
                "charter_version": int(charter.version),
                "corpus_digest": corpus_digest,
                "corpus_size": len(corpus_entries),
                "grounds": grounds_text,
                "adjudicated_at": now_ts,
            },
            sort_keys=True,
        )

    # ------------------------------------------------------------------
    # Precedent registry maintenance
    # ------------------------------------------------------------------

    @gl.public.write
    def mark_landmark(self, precedent_id: str) -> str:
        """
        Elevate a ruling to landmark status.

        Landmarks are taken into every later corpus ahead of ordinary rulings
        and are labelled binding when the panel reads them. They live in their
        own index, so a foundational early ruling is never pushed out of the
        corpus by the volume of later routine decisions.
        """
        key = str(precedent_id).strip()
        self._require(
            key in self.precedents,
            f"{ERROR_EXPECTED} Unknown precedent: {key}",
        )
        record = self.precedents[key]
        charter = self._charter(str(record.dao_id))
        self._require_steward(charter)
        self._require(
            not bool(record.overruled),
            f"{ERROR_EXPECTED} An overruled precedent cannot be made a landmark",
        )
        self._require(
            not bool(record.landmark),
            f"{ERROR_EXPECTED} Precedent is already a landmark: {key}",
        )

        record.landmark = True
        self.precedents[key] = record

        dao_key = str(charter.dao_id)
        index = int(charter.landmark_total)
        self.dao_landmark_at[f"{dao_key}|{index}"] = key
        charter.landmark_total = u32(index + 1)
        self.charters[dao_key] = charter

        return json.dumps(
            {
                "precedent_id": key,
                "dao_id": dao_key,
                "landmark": True,
                "landmark_index": index,
                "landmark_total": index + 1,
            },
            sort_keys=True,
        )

    @gl.public.write
    def overrule_precedent(self, precedent_id: str, reason: str) -> str:
        """
        Retire a ruling from the DAO's active case law, on the record.

        An overruled precedent is excluded from every later corpus but is never
        deleted: the entry, the reason and the timestamp stay readable, because
        a constitutional court that could erase its own history would not be
        one. The proposal and the ruling it came from are untouched, so
        `is_compliant` keeps answering for a proposal whose precedent has since
        been retired.
        """
        key = str(precedent_id).strip()
        self._require(
            key in self.precedents,
            f"{ERROR_EXPECTED} Unknown precedent: {key}",
        )
        record = self.precedents[key]
        charter = self._charter(str(record.dao_id))
        self._require_steward(charter)
        self._require(
            not bool(record.overruled),
            f"{ERROR_EXPECTED} Precedent is already overruled: {key}",
        )

        reason_text = _as_text(reason, MAX_NOTE_LEN)
        self._require(
            bool(reason_text),
            f"{ERROR_EXPECTED} A reason is required to overrule a precedent",
        )

        now_ts = int(self._now())
        record.overruled = True
        record.overruled_reason = reason_text
        record.overruled_at = u64(now_ts)
        self.precedents[key] = record

        return json.dumps(
            {
                "precedent_id": key,
                "dao_id": str(record.dao_id),
                "overruled": True,
                "reason": reason_text,
                "overruled_at": now_ts,
                "was_landmark": bool(record.landmark),
            },
            sort_keys=True,
        )

    # ------------------------------------------------------------------
    # Integration surface: what a DAO's voting contract calls
    # ------------------------------------------------------------------

    @gl.public.view
    def is_compliant(self, proposal_id: str) -> bool:
        """
        The gate. True only if the proposal's ruling of record is COMPLIANT.

        This is the one call a DAO's voting contract needs:

            guard = gl.get_contract_at(Address(GUARD_ADDRESS))
            if not guard.view().is_compliant(proposal_id):
                raise gl.vm.UserError("proposal did not clear constitutional review")

        An unknown proposal is False, not an error, so a voting contract can
        call this on any identifier without needing to handle a revert. Both
        rejecting rulings are False, including AMENDMENT_REQUIRED: a proposal
        that still needs changes has not cleared the gate, and it should be
        amended and resubmitted rather than voted on.
        """
        pid = str(proposal_id).strip()
        if pid not in self.rulings:
            return False
        return str(self.rulings[pid].ruling) == RULING_COMPLIANT

    @gl.public.view
    def require_compliant(self, proposal_id: str) -> bool:
        """
        Same gate, but reverts instead of returning False, with the reason.

        Useful for a voting contract that wants the rejection reason to surface
        in its own revert rather than having to read the ruling separately.
        """
        pid = str(proposal_id).strip()
        self._require(
            pid in self.rulings,
            f"{ERROR_EXPECTED} Proposal has not been adjudicated: {pid}",
        )
        record = self.rulings[pid]
        ruling = str(record.ruling)
        if ruling == RULING_COMPLIANT:
            return True
        if ruling == RULING_AMENDMENT_REQUIRED:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Proposal requires amendment before a vote "
                f"({str(record.mandate_class)}): {str(record.required_amendments)}"
            )
        raise gl.vm.UserError(
            f"{ERROR_EXPECTED} Proposal is non-compliant with the charter "
            f"({str(record.mandate_class)}): {str(record.principle)}"
        )

    @gl.public.view
    def compliance_status(self, proposal_id: str) -> str:
        """
        Everything a front end or a voting contract needs about one proposal,
        as a single JSON object. `adjudicated` is False for an unknown id
        rather than raising, so a caller can poll one endpoint.
        """
        pid = str(proposal_id).strip()
        if pid not in self.rulings:
            return json.dumps(
                {"proposal_id": pid, "adjudicated": False, "is_compliant": False},
                sort_keys=True,
            )
        record = self.rulings[pid]
        proposal = self.proposals[pid] if pid in self.proposals else None
        return json.dumps(
            {
                "proposal_id": pid,
                "adjudicated": True,
                "dao_id": str(record.dao_id),
                "ruling": str(record.ruling),
                "is_compliant": str(record.ruling) == RULING_COMPLIANT,
                "mandate_class": str(record.mandate_class),
                "principle": str(record.principle),
                "holding_digest": str(record.principle_digest),
                "required_amendments": str(record.required_amendments),
                "constitution_drift": bool(record.constitution_drift),
                "charter_version": int(record.charter_version),
                "precedent_id": str(record.precedent_id),
                "revision": int(record.revision),
                "adjudicated_at": int(record.adjudicated_at),
                "appealed": bool(proposal.appealed) if proposal is not None else False,
                "submitter": self._addr_key(proposal.submitter) if proposal is not None else "",
            },
            sort_keys=True,
        )

    @gl.public.view
    def find_adjudication(self, dao_id: str, title: str, body: str) -> str:
        """
        Look up a ruling by proposal text rather than by id.

        A voting contract that keys ballots on the proposal text, or a front
        end checking whether a draft has already been ruled on, can ask here
        before spending a submission. The digest is taken over the same
        normalized text `submit_proposal` uses, so a re-cased or re-wrapped
        copy of an already-adjudicated proposal still resolves.

        The lookup is scoped by the charter's current version, which is the
        version a new submission would be judged under.
        """
        charter = self._charter(dao_id)
        dao_key = str(charter.dao_id)
        body_digest = _digest(
            ["proposal", _normalize_text(_as_text(title, MAX_TITLE_LEN)), _normalize_text(body)]
        )
        slot = f"{dao_key}|{int(charter.version)}|{body_digest}"
        if slot not in self.adjudicated_digest:
            return json.dumps(
                {
                    "dao_id": dao_key,
                    "charter_version": int(charter.version),
                    "body_digest": body_digest,
                    "found": False,
                },
                sort_keys=True,
            )
        pid = str(self.adjudicated_digest[slot])
        record = self.rulings[pid] if pid in self.rulings else None
        return json.dumps(
            {
                "dao_id": dao_key,
                "charter_version": int(charter.version),
                "body_digest": body_digest,
                "found": True,
                "proposal_id": pid,
                "ruling": str(record.ruling) if record is not None else "",
                "is_compliant": (
                    str(record.ruling) == RULING_COMPLIANT if record is not None else False
                ),
            },
            sort_keys=True,
        )

    # ------------------------------------------------------------------
    # Views: charters
    # ------------------------------------------------------------------

    @gl.public.view
    def get_charter(self, dao_id: str) -> str:
        """The registered charter, including the pinned document fingerprint."""
        charter = self._charter(dao_id)
        return json.dumps(
            {
                "dao_id": str(charter.dao_id),
                "display_name": str(charter.display_name),
                "steward": self._addr_key(charter.steward),
                "registered_by": self._addr_key(charter.registered_by),
                "constitution_url": str(charter.constitution_url),
                "url_digest": str(charter.url_digest),
                "doc_fingerprint": str(charter.doc_fingerprint),
                "doc_chars": int(charter.doc_chars),
                "version": int(charter.version),
                "ratified_at": int(charter.ratified_at),
                "amended_at": int(charter.amended_at),
                "active": bool(charter.active),
                "enforce_pinning": bool(charter.enforce_pinning),
                "restrict_proposers": bool(charter.restrict_proposers),
                "submission_cooldown_secs": int(charter.submission_cooldown_secs),
                "proposal_total": int(charter.proposal_total),
                "precedent_total": int(charter.precedent_total),
                "landmark_total": int(charter.landmark_total),
                "compliant_total": int(charter.compliant_total),
                "non_compliant_total": int(charter.non_compliant_total),
                "amendment_required_total": int(charter.amendment_required_total),
            },
            sort_keys=True,
        )

    @gl.public.view
    def charter_exists(self, dao_id: str) -> bool:
        """Whether a DAO is registered, under the canonical form of its id."""
        return self._dao_key(dao_id) in self.charters

    @gl.public.view
    def get_charter_count(self) -> int:
        return int(self.charter_total)

    @gl.public.view
    def get_charter_id_at(self, index: int) -> str:
        """Enumerate registered DAOs without scanning storage."""
        slot = str(int(index))
        self._require(
            slot in self.charter_at,
            f"{ERROR_EXPECTED} No charter at index {index}",
        )
        return str(self.charter_at[slot])

    @gl.public.view
    def get_amendment(self, dao_id: str, version: int) -> str:
        """
        One entry from the charter's amendment log. Version 1 is the original
        registration and has no amendment record.
        """
        charter = self._charter(dao_id)
        slot = f"{charter.dao_id}|{int(version)}"
        self._require(
            slot in self.dao_amendment_at,
            f"{ERROR_EXPECTED} No amendment recorded for version {version}",
        )
        record = self.dao_amendment_at[slot]
        return json.dumps(
            {
                "dao_id": str(record.dao_id),
                "version": int(record.version),
                "previous_url": str(record.previous_url),
                "previous_fingerprint": str(record.previous_fingerprint),
                "new_url": str(record.new_url),
                "new_fingerprint": str(record.new_fingerprint),
                "note": str(record.note),
                "ratified_at": int(record.ratified_at),
                "ratified_by": self._addr_key(record.ratified_by),
            },
            sort_keys=True,
        )

    @gl.public.view
    def is_proposer_allowed(self, dao_id: str, proposer: str) -> bool:
        """
        Whether an address may submit. Always True when the charter does not
        restrict proposers, so a front end can call this unconditionally.
        """
        charter = self._charter(dao_id)
        if not bool(charter.restrict_proposers):
            return True
        try:
            target = Address(str(proposer).strip())
        except Exception:
            return False
        slot = f"{charter.dao_id}|{self._addr_key(target)}"
        return bool(self.proposer_allowed[slot]) if slot in self.proposer_allowed else False

    @gl.public.view
    def submission_available_at(self, dao_id: str, proposer: str) -> int:
        """
        The earliest timestamp at which this address may submit again. Zero
        means it may submit now.
        """
        charter = self._charter(dao_id)
        cooldown = int(charter.submission_cooldown_secs)
        if cooldown <= 0:
            return 0
        try:
            target = Address(str(proposer).strip())
        except Exception:
            return 0
        slot = f"{charter.dao_id}|{self._addr_key(target)}"
        if slot not in self.last_submission_at:
            return 0
        return int(self.last_submission_at[slot]) + cooldown

    # ------------------------------------------------------------------
    # Views: proposals and rulings
    # ------------------------------------------------------------------

    @gl.public.view
    def get_proposal(self, proposal_id: str) -> str:
        """The submitted proposal and the decision context its ruling used."""
        pid = str(proposal_id).strip()
        self._require(
            pid in self.proposals,
            f"{ERROR_EXPECTED} Unknown proposal: {pid}",
        )
        record = self.proposals[pid]
        return json.dumps(
            {
                "proposal_id": str(record.proposal_id),
                "dao_id": str(record.dao_id),
                "submitter": self._addr_key(record.submitter),
                "title": str(record.title),
                "body": str(record.body),
                "body_digest": str(record.body_digest),
                "charter_version": int(record.charter_version),
                "submitted_at": int(record.submitted_at),
                "corpus_digest": str(record.corpus_digest),
                "corpus_size": int(record.corpus_size),
                "revision": int(record.revision),
                "appealed": bool(record.appealed),
                "appeal_grounds": str(record.appeal_grounds),
                "appealed_at": int(record.appealed_at),
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_ruling(self, proposal_id: str) -> str:
        """The full ruling of record, including the leader's reasoning."""
        pid = str(proposal_id).strip()
        self._require(
            pid in self.rulings,
            f"{ERROR_EXPECTED} Proposal has not been adjudicated: {pid}",
        )
        return self._ruling_json(self.rulings[pid])

    @gl.public.view
    def get_ruling_revision(self, proposal_id: str, revision: int) -> str:
        """
        A superseded ruling, by revision number. Revision 1 is the original
        adjudication; it moves into history when an appeal replaces it. The
        current ruling of record is always available from `get_ruling`.
        """
        pid = str(proposal_id).strip()
        slot = f"{pid}|{int(revision)}"
        if slot in self.ruling_history:
            return self._ruling_json(self.ruling_history[slot])
        self._require(
            pid in self.rulings and int(self.rulings[pid].revision) == int(revision),
            f"{ERROR_EXPECTED} No revision {revision} for proposal {pid}",
        )
        return self._ruling_json(self.rulings[pid])

    def _ruling_json(self, record: Ruling) -> str:
        return json.dumps(
            {
                "proposal_id": str(record.proposal_id),
                "dao_id": str(record.dao_id),
                "ruling": str(record.ruling),
                "is_compliant": str(record.ruling) == RULING_COMPLIANT,
                "mandate_class": str(record.mandate_class),
                "principle": str(record.principle),
                "holding_digest": str(record.principle_digest),
                "rationale": str(record.rationale),
                "required_amendments": str(record.required_amendments),
                "constitution_clause": str(record.constitution_clause),
                "cited_precedents": str(record.cited_precedents),
                "constitution_drift": bool(record.constitution_drift),
                "live_fingerprint": str(record.live_fingerprint),
                "pinned_fingerprint": str(record.pinned_fingerprint),
                "charter_version": int(record.charter_version),
                "corpus_digest": str(record.corpus_digest),
                "adjudicated_at": int(record.adjudicated_at),
                "revision": int(record.revision),
                "precedent_id": str(record.precedent_id),
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_proposal_count(self, dao_id: str) -> int:
        return int(self._charter(dao_id).proposal_total)

    @gl.public.view
    def get_proposal_id_at(self, dao_id: str, index: int) -> str:
        """Enumerate a DAO's proposals in submission order."""
        charter = self._charter(dao_id)
        slot = f"{charter.dao_id}|{int(index)}"
        self._require(
            slot in self.dao_proposal_at,
            f"{ERROR_EXPECTED} No proposal at index {index} for {charter.dao_id}",
        )
        return str(self.dao_proposal_at[slot])

    # ------------------------------------------------------------------
    # Views: the precedent registry
    # ------------------------------------------------------------------

    @gl.public.view
    def get_precedent(self, precedent_id: str) -> str:
        """One precedent, including whether it has been retired and why."""
        key = str(precedent_id).strip()
        self._require(
            key in self.precedents,
            f"{ERROR_EXPECTED} Unknown precedent: {key}",
        )
        record = self.precedents[key]
        return json.dumps(
            {
                "precedent_id": str(record.precedent_id),
                "dao_id": str(record.dao_id),
                "proposal_id": str(record.proposal_id),
                "ruling": str(record.ruling),
                "mandate_class": str(record.mandate_class),
                "holding": str(record.principle),
                "holding_digest": str(record.principle_digest),
                "charter_version": int(record.charter_version),
                "created_at": int(record.created_at),
                "landmark": bool(record.landmark),
                "overruled": bool(record.overruled),
                "overruled_reason": str(record.overruled_reason),
                "overruled_at": int(record.overruled_at),
                "superseded_by": str(record.superseded_by),
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_precedent_count(self, dao_id: str) -> int:
        return int(self._charter(dao_id).precedent_total)

    @gl.public.view
    def get_precedent_id_at(self, dao_id: str, index: int) -> str:
        """Enumerate a DAO's case law in the order it was decided."""
        charter = self._charter(dao_id)
        slot = f"{charter.dao_id}|{int(index)}"
        self._require(
            slot in self.dao_precedent_at,
            f"{ERROR_EXPECTED} No precedent at index {index} for {charter.dao_id}",
        )
        return str(self.dao_precedent_at[slot])

    @gl.public.view
    def list_precedents(self, dao_id: str, offset: int, limit: int) -> str:
        """
        A page of the DAO's case law, oldest first, with overruled entries
        included and flagged. `limit` is capped at MAX_CORPUS_SCAN so a single
        call cannot be made unbounded.
        """
        charter = self._charter(dao_id)
        dao_key = str(charter.dao_id)
        total = int(charter.precedent_total)
        start = max(0, int(offset))
        count = max(0, min(int(limit), MAX_CORPUS_SCAN))

        entries = []
        position = start
        while position < total and len(entries) < count:
            slot = f"{dao_key}|{position}"
            position += 1
            if slot not in self.dao_precedent_at:
                continue
            precedent_id = str(self.dao_precedent_at[slot])
            if precedent_id not in self.precedents:
                continue
            record = self.precedents[precedent_id]
            entries.append(
                {
                    "precedent_id": precedent_id,
                    "proposal_id": str(record.proposal_id),
                    "ruling": str(record.ruling),
                    "mandate_class": str(record.mandate_class),
                    "holding": str(record.principle),
                    "holding_digest": str(record.principle_digest),
                    "charter_version": int(record.charter_version),
                    "created_at": int(record.created_at),
                    "landmark": bool(record.landmark),
                    "overruled": bool(record.overruled),
                }
            )
        return json.dumps(
            {
                "dao_id": dao_key,
                "total": total,
                "offset": start,
                "returned": len(entries),
                "precedents": entries,
            },
            sort_keys=True,
        )

    @gl.public.view
    def active_precedent_corpus(self, dao_id: str) -> str:
        """
        Exactly the case law the next adjudication for this DAO will be given,
        in the order the panel will read it, with its commitment digest.

        This is the auditability hook. A steward can see what the gate is
        currently bound by before submitting, and a reviewer can compare this
        digest against the `corpus_digest` stored on any past ruling to confirm
        which precedents produced it.
        """
        charter = self._charter(dao_id)
        entries = self._select_corpus(charter)
        return json.dumps(
            {
                "dao_id": str(charter.dao_id),
                "charter_version": int(charter.version),
                "corpus_digest": self._corpus_digest(entries, charter),
                "corpus_size": len(entries),
                "max_corpus": MAX_CORPUS,
                "max_landmarks": MAX_CORPUS_LANDMARKS,
                "precedents": entries,
                "rendered": self._render_corpus(entries, int(charter.version)),
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_registry_info(self) -> str:
        """
        Registry-wide metadata plus the taxonomy and bounds a front end needs
        to render the gate without hard-coding this contract's constants.
        """
        return json.dumps(
            {
                "registry_name": str(self.registry_name),
                "deployer": self._addr_key(self.deployer),
                "deployed_at": int(self.deployed_at),
                "charter_total": int(self.charter_total),
                "proposal_total": int(self.proposal_total),
                "precedent_total": int(self.precedent_total),
                "rulings": list(RULINGS),
                "mandate_classes": list(MANDATE_CLASSES),
                "max_corpus": MAX_CORPUS,
                "max_corpus_landmarks": MAX_CORPUS_LANDMARKS,
                "max_corpus_scan": MAX_CORPUS_SCAN,
                "max_landmark_scan": MAX_LANDMARK_SCAN,
                "max_body_len": MAX_BODY_LEN,
                "min_body_len": MIN_BODY_LEN,
                "max_title_len": MAX_TITLE_LEN,
                "max_principle_len": MAX_PRINCIPLE_LEN,
                "min_principle_len": MIN_PRINCIPLE_LEN,
                "max_revisions": MAX_REVISIONS,
                "max_cooldown_secs": MAX_COOLDOWN_SECS,
                # The fields every validator independently binds before a
                # ruling is recorded, and the fields a panel reports without
                # binding. A front end can show integrators what the panel
                # actually agreed on rather than implying it agreed on
                # everything it reported.
                "consensus_bound_fields": [
                    "ruling", "mandate_class", "constitution_drift",
                ],
                # Derived by the contract from the bound fields above plus
                # deterministic on-chain data. This is the only text a later
                # adjudication reads out of the precedent registry.
                "derived_precedent_fields": ["holding", "holding_digest"],
                "reported_unbound_fields": [
                    "principle", "rationale", "constitution_clause",
                    "required_amendments", "cited_precedents",
                ],
            },
            sort_keys=True,
        )
