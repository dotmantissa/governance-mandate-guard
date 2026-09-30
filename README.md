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
accepted or rejected, is written into the DAO's registry with a one-line statement
of the principle the ruling turned on, and later compliance checks are handed that
growing body of rulings as binding context. The registry is per DAO, so a ruling
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
| Contract address | `0x0D6759A08AbC0bC71bFA50EA33a220c95d84E15e` |
| Explorer | https://explorer-studio.genlayer.com/address/0x0D6759A08AbC0bC71bFA50EA33a220c95d84E15e |
| Deployment transaction | `0x62c033bc6981c0864710d2e0e1830f8ec6ebf1547009a546897e90ea490bbcd9` |
| Deployer | `0xBC1399c55538eC034d4Da550C03c34Ae0C357f53` |
| GenVM runner | `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` |
| Source SHA-256 | `61239eb4fd1c76848f7158ab4d546328236dc6b2ea09949cbb8ccee493251393` |

The deployed bytes are byte-for-byte identical to `contracts/governance_mandate_guard.py`
in this repository. That is not a claim, it is a test:
`tests/test_live_studionet.py::test_deployed_source_matches_the_repository` fetches
the contract with `gen_getContractCode` and compares it against the local file on
every run. A reviewer can confirm it themselves in one command:

```bash
curl -sS https://studio.genlayer.com/api \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"gen_getContractCode",
       "params":["0x0D6759A08AbC0bC71bFA50EA33a220c95d84E15e"]}' \
  | python3 -c 'import base64,json,sys; sys.stdout.buffer.write(base64.b64decode(json.load(sys.stdin)["result"]))' \
  | diff - contracts/governance_mandate_guard.py && echo identical
```

`gen_getContractCode` returns the source base64 encoded. The `genlayer code`
CLI command prints the same source, but it adds a `Result:` header and a trailing
blank line, so it is convenient for reading and awkward for byte comparison.

The reference consumer used by the live suite is deployed at
[`0x91e24D4CD36876219cD19Fa5aeC12bca322a680A`](https://explorer-studio.genlayer.com/address/0x91e24D4CD36876219cD19Fa5aeC12bca322a680A).
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

Everything else the model returns is prose. It is stored and reported for human
review, and it is never gated, so two validators that reason in completely
different words still reach consensus as long as the decision matches.

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
| `compliance_status(proposal_id)` | JSON: ruling, class, principle, required amendments, drift, revision, submitter. |
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
guard = gl.get_contract_at(Address("0x0D6759A08AbC0bC71bFA50EA33a220c95d84E15e"))
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
GUARD_ADDRESS=0x0D6759A08AbC0bC71bFA50EA33a220c95d84E15e \
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
meridian-dao#p1  submitted   NON_COMPLIANT (treasury_mandate)
  A single grant or treasury allocation may not exceed the 50,000 USDC cap,
  regardless of the purpose or the total size of the treasury reserves.

meridian-dao#p1  on appeal   COMPLIANT
  A treasury grant that complies with the current single-grant ceiling, names its
  authorising council resolution, and funds work sustaining the lending market
  satisfies constitutional requirements.
```

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
