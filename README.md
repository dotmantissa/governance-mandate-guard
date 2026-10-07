# Governance Mandate Guard

A pre-vote constitutional gate for DAOs, built as a GenLayer intelligent contract.

Token voting measures popularity. It does not measure whether a proposal is legal
inside the protocol's own framework. A proposal can win a vote and still violate
the charter the DAO ratified, or contradict a ruling the same DAO issued six
months earlier. Voters almost never have time to cross-reference a forty-page
constitution and two years of governance history before a snapshot closes, so the
violation tends to be discovered after execution.

This contract runs before the vote. A DAO registers its constitution once. Every
proposal is then submitted here first, and GenLayer validators independently fetch
that constitution, read the DAO's own accumulated case law, and rule:

| Ruling | Meaning |
| --- | --- |
| `COMPLIANT` | Neither the charter nor precedent prohibits it. It may go to a vote. |
| `NON_COMPLIANT` | It conflicts with a provision or with binding precedent, and rewording cannot cure it. |
| `AMENDMENT_REQUIRED` | The substance is permissible but the text breaches a provision that a change of wording would satisfy. The required amendments are named. |

The DAO's voting contract calls `is_compliant(proposal_id)` before it opens a
ballot. Only proposals that cleared the gate can be voted on.

The part that compounds is the precedent registry. Every adjudicated proposal,
accepted or rejected, is written into the DAO's registry with a one-line holding
that the contract derives from what the validators actually agreed, and later
compliance checks are handed that growing body of rulings as binding context. The registry is per DAO, so a ruling
made in month two constrains the reading of a similar proposal in month twenty.
That is institutional memory a token vote cannot produce on its own.

The registry is multi-tenant. Any DAO on any chain registers its own charter and
gets its own isolated precedent registry, stewards, allowlist and counters.

## Live deployment

| | |
| --- | --- |
| Network | GenLayer StudioNet |
| Chain ID | 61999 |
| RPC endpoint | `https://studio.genlayer.com/api` |
| Contract address | `0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6` |
| Explorer | https://explorer-studio.genlayer.com/address/0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6 |
| Deployment transaction | `0xfae9bdf9672eeeada424910dabc6c7f04831404389db338382d669fd2de3e720` |
| Deployer | `0xBC1399c55538eC034d4Da550C03c34Ae0C357f53` |
| GenVM runner | `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` |
| Source SHA-256 | `ac7a01a3c0a4038c30418926832a4d33dcbb44098efbc7028eef594a96c5f845` |

The deployed bytes are byte-for-byte identical to `contracts/governance_mandate_guard.py`
in this repository. That is not a claim, it is a test:
`tests/test_live_studionet.py::test_deployed_source_matches_the_repository` fetches
the contract with `gen_getContractCode` and compares it against the local file on
every run. A reviewer can confirm it themselves in one command:

```bash
curl -sS https://studio.genlayer.com/api \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"gen_getContractCode",
       "params":["0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6"]}' \
  | python3 -c 'import base64,json,sys; sys.stdout.buffer.write(base64.b64decode(json.load(sys.stdin)["result"]))' \
  | diff - contracts/governance_mandate_guard.py && echo identical
```

`gen_getContractCode` returns the source base64 encoded. The `genlayer code`
CLI command prints the same source, but it adds a `Result:` header and a trailing
blank line, so it is convenient for reading and awkward for byte comparison.

The reference consumer used by the live suite is deployed at
[`0xEcD0f96114fDeff3Fb59200473579099DfD00b9E`](https://explorer-studio.genlayer.com/address/0xEcD0f96114fDeff3Fb59200473579099DfD00b9E).
Full deployment metadata is in `artifacts/deployment.json`; the recorded live run
with all 12 transactions, their consensus results and vote breakdowns is in
`artifacts/live-verification.json`.

## Why this needs GenLayer

The question "does this proposal violate our constitution?" is not computable by a
deterministic EVM contract. The constitution is prose. The proposal is prose.
Deciding whether

> the Treasury may not allocate more than ten percent of reserves to a single
> counterparty in any calendar quarter

is breached by

> authorize a strategic partnership tranche of 1,200,000 USDC to Acme Labs

requires reading both documents, knowing that the reserve figure is defined in the
charter's definitions section, and knowing whether an earlier ruling already
interpreted "tranche" as an allocation.

A single oracle making that call is a single point of capture, which is exactly
what a constitutional gate cannot be. GenLayer's Optimistic Democracy runs the
reading on many validators at once. Each fetches the constitution itself, receives
the identical precedent corpus from contract storage, and reaches its own
conclusion. The contract's equivalence principle decides whether those conclusions
agree well enough to be binding. No validator's reading is authoritative, and a
validator that returns a different ruling forces the appeal path rather than
silently passing.

## How consensus is used

Two separate nondeterministic operations, each with a purpose-built leader and
validator pair through `gl.vm.run_nondet_unsafe`.

### 1. Charter pinning

Used by `register_charter` and `ratify_amendment`.

- **Leader** fetches the constitution URL, normalizes the document (markup
  stripped, whitespace collapsed, case folded) and returns the Keccak-256
  fingerprint of that normalized text plus its length.
- **Validator** fetches the same URL itself and recomputes the fingerprint from
  its own copy. It agrees only on an exact match of both fields.

Exact agreement is deliberate. The whole guarantee of the primitive is that a
specific constitutional text was pinned and that every later ruling was made
against that text. A URL whose bytes are not stable across a handful of
near-simultaneous fetches cannot support that claim, and failing loudly at
registration is better than failing quietly during an adjudication. DAOs should
pin an immutable source: IPFS, Arweave, or a commit-pinned raw file.

### 2. Adjudication

Used by `submit_proposal` and `appeal_ruling`.

- **Leader** fetches the live constitution, compares its fingerprint against the
  pinned one to detect drift, then asks an LLM to rule on the proposal against the
  constitution text and the precedent corpus that deterministic code selected
  before the nondeterministic block began.
- **Validator** reruns that entire function. It does not inspect the shape of the
  leader's answer, and it does not ask a model whether the leader looked
  reasonable. It performs its own fetch and its own reasoning, then compares
  decisions.

Agreement requires all three of:

| Field | Values | Why it is bound |
| --- | --- | --- |
| `ruling` | `COMPLIANT`, `NON_COMPLIANT`, `AMENDMENT_REQUIRED` | This is what `is_compliant` returns. |
| `mandate_class` | one of eight | This is the ground of the decision and the precedent's index, so two validators rejecting a proposal for unrelated reasons are not treated as agreeing. |
| `constitution_drift` | boolean | A ruling made against a document that no longer matches the pin was made against different text. |

All three are closed vocabularies compared exactly, character for character.
Nothing is NLP-judged, and no second model call sits inside the validator.

The prose that is not bound is the prose no later panel reads. `principle`,
`rationale`, `constitution_clause`, `required_amendments` and `cited_precedents`
are stored and reported for human review, never enter a precedent corpus and
never gate anything, so two validators that reason in completely different words
still reach consensus as long as the decision and its ground match.
`get_registry_info` publishes all three lists under `consensus_bound_fields`,
`derived_precedent_fields` and `reported_unbound_fields`, so an integrator never
has to guess which is which.

### Why the case law is derived and not authored

The registry is the compounding asset of this primitive, so the one thing that
must hold is that the case law a later panel reads was checked by every
validator that accepted it. If a single node can write the rule that binds
future readings, the gate has a single point of capture in the one place it
cannot afford one.

An earlier build of this contract stored the panel's own `principle` sentence as
that case law and tried to bind it across validators. That approach was measured
on StudioNet rather than argued about, and it failed. The measurement is worth
stating, because it is the reason the design looks the way it does.

The exact adjudication prompt was run five times against the live model, with
the real pinned constitution and one compliant proposal. All five panels agreed
on the ruling and the ground. The rules they wrote were these:

1. a ceiling of 50,000 USDC per individual grant
2. 50,000 USDC or less needs no further constitutional authorisation
3. a ceiling of 50,000 USDC, plus a seven day review period
4. a ceiling of 50,000 USDC in a calendar quarter
5. grants are permissible if they comply with spending limits, naming no figure

These are not rewordings of one rule. Rule 1 is per grant and rule 4 is per
quarter: five grants of 40,000 USDC in one quarter are each permitted by rule 1
and refused in total by rule 4. Rule 5 admits what rule 1 refuses. Rule 2
affirmatively dispenses with an authorisation that rule 3 requires. Generalizing
a rule from a single case is underdetermined, which is the holding and dicta
problem courts have always had, so five honest models produce five different
rules. The premise that honest panels converge on one sentence is simply false.

Every route to binding that sentence fails on that evidence:

| Route | Outcome |
| --- | --- |
| Exact string equality | Deadlocks. Honest models never produce identical prose, so every adjudication fails. |
| Deterministic gates on figures, polarity and subject matter | Necessary but never sufficient. Replayed over all ten pairs above, the gates rejected nothing: the figures agree, the polarity agrees, the subject matter agrees. The rules still differ. |
| Comparative semantic equivalence | Correct, and fatal to liveness. Asked whether two of these rules decide the same future case the same way, the comparator answered no, because they do not. Deployed, it returned `MAJORITY_DISAGREE` at 1 agreement of 5 on every adjudication, while charter pinning and amendment ratification, which do not touch the principle, reached `MAJORITY_AGREE`. Validator receipts showed `SUCCESS` with no error: the validators had genuinely computed disagreement. |
| Each validator checks the leader's sentence against the constitution it fetched | Live, and unsound. Rules 1 and 4 are each individually defensible against the text, so both pass, and they produce different future outcomes. That is the original defect restored. |

So no sentence a panel wrote becomes binding case law. The contract derives the
holding instead, with `_render_holding`, from the three consensus-bound fields
plus data already on chain:

```
Under charter v1, a proposal titled "Fund the documentation working group" was
ruled NON_COMPLIANT, on the ground of treasury_mandate.
```

A compliant ruling implicates no provision and so names no ground. A ruling made
against a drifted document says so, because drift is bound too:

```
Under charter v2, a proposal titled "Fund the documentation working group" was
ruled AMENDMENT_REQUIRED, on the ground of procedural_mandate. The live
constitution had drifted from the ratified text when this was decided.
```

Every input is either consensus-bound, meaning `ruling`, `mandate_class` and
`constitution_drift`, which every validator compared exactly before voting, or
already on chain, meaning the proposer's own title and the charter version. The
rendering runs outside the nondeterministic block on inputs every node holds
identically, so any two validators produce the same bytes. Nothing unchecked can
reach it, because nothing unchecked is passed to it.

What a later panel receives is therefore the facts of the decided proposal and
the outcome reached on them, and the prompt directs it to reason from those by
analogy. That is what citing a precedent is. The generalized rule is left to the
panel reading the case, which is where the law of a case actually gets made.

The panel's own sentence is not discarded. It stays on the `Ruling` record and is
reported through `compliance_status` for human review, in the same unbound group
as `rationale`, `constitution_clause`, `required_amendments` and
`cited_precedents`. It is useful to a steward reading why a proposal failed, and
it is provably not load-bearing:
`test_the_holding_and_not_the_prose_is_what_future_panels_read` asserts the
sentence is absent from the next panel's prompt and the derived holding is
present.

`holding_digest` is the Keccak-256 of the holding's canonical key, stored on both
the precedent and the ruling. Anyone can recompute it from the registry text, it
ties a ruling to the case law it generated, and it is mixed into the corpus
digest, so the body of precedent a past ruling was decided under is verifiable
rather than asserted.

The mandate class is drawn from a fixed taxonomy and normalized on both sides
through the same deterministic mapping, so a difference in phrasing never
registers as a difference in judgment:

```
precedent_conflict   contradicts a prior binding ruling
amendment_mandate    changes to the charter itself, or its supermajority procedure
authority_mandate    who may act, and the scope of a role or delegation
treasury_mandate     spending, allocation and reserve limits
rights_mandate       protections of token holders and minorities
scope_mandate        outside the protocol's stated purpose
procedural_mandate   notice, quorum, timelock, process requirements
none                 no provision implicated
```

The list is in priority order. When a normalized reply matches more than one
class, the earlier entry wins, so the mapping is a total function and both sides
derive the same class from the same reply. When the ruling is `COMPLIANT` the class
is forced to `none` on both sides: a passing proposal implicates no provision, and
requiring agreement on a field that carries no decision would manufacture
disagreement.

Drift is compared as a boolean rather than as a hash, because the boolean stays
stable even when a page carries a trivial dynamic element.

### Error classification

Failures are classified so validators can agree on a failure as readily as on a
success:

| Prefix | Used for | Comparison |
| --- | --- | --- |
| `[EXPECTED]` | business logic rejections | exact match |
| `[EXTERNAL]` | 4xx from the document source, the server's settled answer | exact match |
| `[TRANSIENT]` | 5xx and transport failures | two transient failures agree without identical text |
| `[LLM_ERROR]` | unparseable or invalid model output | never agrees, forces validator rotation |

A 502 on one validator and a 503 on another is still the same outage, so those
agree. A broken model reply never agrees, because freezing one into a
constitutional ruling is the single outcome the gate must not allow.

## The determinism boundary

Everything that decides what the validators are asked is computed before the
nondeterministic block and captured in the closure GenVM ships to every validator:
the proposal text, the pinned fingerprint, and the precedent corpus with its own
Keccak-256 digest. Validators cannot be handed different case law than the leader,
and the corpus digest is stored on the proposal so any later reviewer can confirm
which body of precedent produced a ruling.

A test enforces this rather than trusting it. The SDK warns when a storage manager
is pickled, because storage reads inside a nondeterministic block are not
supported; `test_nondeterministic_closures_never_capture_storage` turns that
warning into an error for the duration of a submission.

Nothing a panel reads is leader-authored and unchecked. A corpus entry is the
complete set of fields a later adjudication consumes, and every one of them is
either deterministic or consensus-bound:

| Corpus field | Status |
| --- | --- |
| `precedent_id`, `proposal_id` | deterministic, the DAO id plus a counter |
| `ruling`, `mandate_class` | consensus-bound, exact |
| `holding` | derived by the contract from the bound ruling, ground and drift flag plus the proposal's own title and the charter version |
| `holding_digest` | deterministic from the derived holding |
| `charter_version` | deterministic, read from the charter |
| `landmark` | deterministic, set only by a steward call |
| `decided_at` | deterministic, the pinned transaction clock |

`test_every_corpus_field_is_bound_or_deterministic` holds that table to its
promise field by field, so a field added to the corpus later cannot quietly
reintroduce an unbound input to future rulings: the suite fails until the new
field is classified. The case law that constrains a ruling in month twenty was
agreed by the panels that sat in month two.

Precedent selection is deterministic and bounded:

1. Landmark rulings first, newest first, up to 6.
2. Remaining slots filled from ordinary non-overruled rulings, newest first, up to
   a corpus of 12.
3. Overruled entries skipped everywhere.
4. Both walks capped (64 ordinary records, 32 landmark records), so a DAO with
   thousands of rulings still adjudicates in bounded gas.

Landmarks live in their own index, so a foundational early ruling is never pushed
out of reach by the volume of later routine decisions. `active_precedent_corpus`
returns exactly what the next adjudication will be given, in the order the panel
will read it, with its commitment digest.

## Contract lifecycle

```
register_charter      steward pins the constitution, the DAO is now gated
submit_proposal       adjudication runs, ruling stored, precedent written
is_compliant          the DAO's voting contract reads the gate
appeal_ruling         submitter contests once, re-adjudication supersedes
mark_landmark         steward elevates a ruling to binding precedent
overrule_precedent    steward retires a ruling from the corpus, on the record
ratify_amendment      steward re-pins after the constitution changes
```

### Amendment and charter versions

`ratify_amendment` re-pins the charter and increments its version. The previous URL
and fingerprint are written to the amendment log, so the text any historical ruling
was made against stays identifiable forever. Re-pinning an identical document is
rejected: an amendment that changes nothing would rotate the version and silently
reopen every previously rejected proposal.

Existing precedents survive an amendment and stay readable, but later corpora label
them as decided under a superseded version, and the panel is told to treat them as
persuasive rather than binding. The replay index is scoped by version, so a proposal
rejected under one constitutional text may be resubmitted once that text has been
amended, which is the correct behaviour when the provision that rejected it has
itself changed.

### Drift

Two behaviours, chosen per charter at registration through `enforce_pinning`:

- **Enforced (default).** A live document that no longer matches the pin blocks
  adjudication outright, with a message naming the remedy. A ruling made against
  text the DAO did not ratify is not a constitutional ruling.
- **Not enforced.** Drift is recorded on the ruling along with both fingerprints
  and surfaced through the views, and the panel is warned in its prompt, but the
  gate still answers. This is for DAOs that deliberately treat their charter as a
  living document.

### Appeals

The original submitter may contest the ruling on their own proposal once. An appeal
is a full re-adjudication rather than a review of the first one: the panel fetches
the constitution and reasons again from scratch, with the proposer's grounds put to
it as argument that does not bind it.

The precedent created by the contested ruling is retired *before* the new corpus is
selected, so an appeal is never bound by the very ruling it contests. The original
ruling is kept in `ruling_history`, the retired precedent stays readable, and both
record what superseded them.

## Admission control

Every submission spends a model call on every validator, so a charter can limit who
may put a proposal in front of the panel:

- `submission_cooldown_secs` rate limits per address. The clock is the pinned
  transaction time, so the gate is deterministic across validators.
- `restrict_proposers` limits submissions to an allowlist the steward maintains.
- The replay index refuses identical text under the same charter version, matched
  on normalized text so re-casing or re-wrapping does not evade it.

All of these run before the nondeterministic block, so a malformed, duplicated or
rate-limited submission costs no fetch and no model call.

## Method surface

### Writes

| Method | Caller | Purpose |
| --- | --- | --- |
| `register_charter(dao_id, display_name, constitution_url, submission_cooldown_secs, restrict_proposers, enforce_pinning)` | anyone | Register a DAO and pin its constitution. Caller becomes steward. |
| `submit_proposal(dao_id, title, body)` | proposer | Submit for constitutional review. Returns the ruling as JSON. |
| `appeal_ruling(proposal_id, grounds)` | original submitter | Contest a ruling once. Re-adjudicates. |
| `ratify_amendment(dao_id, constitution_url, note)` | steward | Re-pin after the constitution changes. |
| `mark_landmark(precedent_id)` | steward | Elevate a ruling to binding precedent. |
| `overrule_precedent(precedent_id, reason)` | steward | Retire a ruling from active case law, on the record. |
| `transfer_stewardship(dao_id, new_steward)` | steward | Hand over charter administration. |
| `set_charter_active(dao_id, active)` | steward | Suspend or resume the gate. |
| `set_proposer_allowed(dao_id, proposer, allowed)` | steward | Manage the allowlist. |

### Views

The integration surface:

| Method | Returns |
| --- | --- |
| `is_compliant(proposal_id)` | `bool`. The gate. False for an unknown proposal rather than an error. |
| `require_compliant(proposal_id)` | `bool`, or reverts with the reason. |
| `compliance_status(proposal_id)` | JSON: ruling, class, holding digest, the panel's unbound principle, required amendments, drift, revision, submitter. |
| `find_adjudication(dao_id, title, body)` | JSON. Look up a ruling by proposal text rather than by id. |

Charters: `get_charter`, `charter_exists`, `get_charter_count`, `get_charter_id_at`,
`get_amendment`, `is_proposer_allowed`, `submission_available_at`.

Proposals and rulings: `get_proposal`, `get_ruling`, `get_ruling_revision`,
`get_proposal_count`, `get_proposal_id_at`.

Precedent: `get_precedent`, `get_precedent_count`, `get_precedent_id_at`,
`list_precedents`, `active_precedent_corpus`.

Registry: `get_registry_info`, which publishes the taxonomy and every bound a front
end would otherwise have to hard-code.

## Integration

See [`INTEGRATION.md`](INTEGRATION.md) for the full guide. The short version is one
call:

```python
guard = gl.get_contract_at(Address("0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6"))
if not guard.view().is_compliant(proposal_id):
    raise gl.vm.UserError("proposal did not clear constitutional review")
```

`contracts/mandate_gated_dao.py` is a working, deployable DAO ballot contract that
does exactly this. It is not an illustration: it holds a voter roll, opens ballots,
counts votes, closes on a deadline and records outcomes, and the live suite deploys
it and proves the gate across a real cross-contract call.

## Repository layout

```
contracts/
  governance_mandate_guard.py   the primitive
  mandate_gated_dao.py          reference consumer, deployable as-is
tests/
  genvm_host.py                 in-process GenVM host, runs the real SDK
  conftest.py                   shared fixtures
  test_consensus.py             what validators check and reject
  test_precedent_registry.py    the registry and corpus selection
  test_charter_lifecycle.py     pinning, drift, amendment, admission control
  test_appeal.py                the contested re-hearing
  test_integration_gate.py      the gate from inside another contract
  test_state_integrity.py       atomicity, isolation, the clock, digests
  test_documentation.py         runs the documented snippets, checks the claims
  test_live_studionet.py        live end-to-end against the deployment
artifacts/
  deployment.json               addresses, hashes, runner, method counts
  live-verification.json        the recorded live run
```

## Running the tests

### Offline: 157 tests, no network, no keys

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/genvm-lint download --version v0.2.16
.venv/bin/python -m pytest tests/ -v
```

The offline suite runs the real contract source through the real GenLayer SDK.
Storage goes through the SDK's own slot engine, `TreeMap`, `DynArray` and type
coercion. Nondeterministic blocks go through the real `gl.vm.run_nondet_unsafe`,
which cloudpickles the leader and validator closures exactly as it does on chain.

Only the node is replaced. GenVM reaches its host through five wasi functions, and
`tests/genvm_host.py` supplies them, dispatching `gl_call` on the same request keys
the node uses: `WebRequest`, `WebRender`, `ExecPrompt`, `RunNondet`, `Sandbox`,
`CallContract`, `PostMessage`, `EmitEvent`, `Trace`.

Because `RunNondet` arrives as the two cloudpickled closures, the harness runs the
leader, encodes its outcome the way the node does (a result code byte followed by
calldata), and executes the validator closure against those bytes. Validator
functions are therefore tested for real, against leader output they did not
produce. Giving the web or model channel a list of answers makes the leader's call
take the first and the validator's call take the second, which reproduces genuine
validator disagreement.

The harness models storage layout and persistence, revert atomicity, per-contract
isolation, synchronous cross-contract view calls and the pinned transaction clock.
It does not model gas metering, the multi-validator panel with its rotation and
appeal, or transaction finality. Those need a network, and the live suite covers
them.

### Live: 15 tests against StudioNet

```bash
GENLAYER_PRIVATE_KEY=0x... \
GUARD_ADDRESS=0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6 \
LIVE_DAO_ID=my-dao-$(date +%s) \
  .venv/bin/python -m pytest tests/test_live_studionet.py -v -s
```

StudioNet is gasless, so a zero balance is fine. Set `LIVE_DAO_ID` to something
fresh: the suite registers a charter and submits specific proposals, and both the
charter registry and the replay index will refuse a repeat.

The live suite walks the arc a DAO actually walks, against five validators, real
HTTP fetches of a revision-pinned public document, and real model calls:

1. Confirm the deployed bytes are this repository's source.
2. Confirm the published schema.
3. Pin a charter against the document.
4. Clear a 40,000 USDC grant that sits under the charter's 50,000 ceiling.
5. Refuse the identical text on resubmission.
6. Refuse a 120,000 USDC grant that sits over that ceiling.
7. Read both rulings back out of the precedent registry.
8. Check the raising gate and the non-raising status endpoint.
9. Landmark a ruling and confirm it moves to the front of the corpus.
10. Deploy the reference consumer and gate a real ballot with it, both ways.
11. Ratify an amendment raising the ceiling to 250,000 USDC.
12. Appeal, and watch the identical proposal text become `COMPLIANT`.
13. Confirm the consumer now admits the appealed proposal with no change to itself.
14. Overrule a precedent and confirm it leaves the corpus but not the record.
15. Write the verification artifact.

Step 12 is the strongest signal in the suite. The same bytes were adjudicated twice
and reached opposite answers because the constitution changed underneath them,
which can only happen if validators genuinely read the live document rather than
reusing a cached conclusion. From the recorded run:

```
meridian-dao-r2b#p1  submitted   NON_COMPLIANT (treasury_mandate)
  A single grant may not exceed 50,000 USDC.

meridian-dao-r2b#p1  on appeal   COMPLIANT
  Treasury allocations are permissible if they are for market sustainability, fall below the single grant ceiling, and comply with procedural disclosure and authorization requirements.
```

All 12 transactions in that run reached `MAJORITY_AGREE`. That is worth stating
explicitly, because an earlier iteration of this contract did not: when the panel
was asked to agree on the ruling principle itself, every `submit_proposal`
returned `MAJORITY_DISAGREE` at 1 agree of 5, while the calls that did not touch
the principle agreed normally. The stored case law is now derived from the
consensus-bound fields instead, and the liveness cost is gone. The measurements
are in the repository history, and the design rationale is in the section above.

The prose a panel writes is still recorded on each ruling and reported by
`get_ruling`, so a reviewer can read the reasoning. It is reported, not bound:
`principle` never reaches a later panel, and the live suite asserts that the
corpus entries carry `holding` and not `principle`.

### Linting

```bash
.venv/bin/genvm-lint check contracts/governance_mandate_guard.py --json
.venv/bin/genvm-lint check contracts/mandate_gated_dao.py --json
```

Both report `"ok": true`.

## Notes for reviewers

- `ACCEPTED` and `FINALIZED` are transaction lifecycle states, not proof that
  execution succeeded. A transaction can finalize with an execution error and apply
  no state change. The live suite reads success from the leader's
  `execution_result`, never from the status.
- `consensus_data.leader_receipt` is a list. Its first entry is the executing
  leader; later entries include rotation slots, and an idle validator appears there
  with `execution_result: ERROR` and a payload of `idle`. Only the first entry is
  the execution of record.
- StudioNet returns a generic failure for a reverting view call rather than the
  contract's message, so `require_compliant` is checked for raising and the reason
  is read from `compliance_status`.
- StudioNet rate limits per IP: 60 requests per minute, 1000 per hour, 10000 per
  day, with a pending-queue cap of 32 in-flight transactions per sender. The live
  suite waits for each receipt before submitting the next transaction.
