# Integrating the Governance Mandate Guard

This guide covers the three ways to consume the primitive: from another GenLayer
intelligent contract, from a front end or backend over JSON-RPC, and by running
your own registry instance.

The deployed registry on StudioNet is
`0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6`. It is multi-tenant, so you do not
need to deploy anything to start using it. Register your charter and go.

---

## 1. From a GenLayer contract

This is the main integration. Your DAO's voting contract reads the gate
synchronously while it decides whether to open a ballot.

### The one call that matters

```python
guard = gl.get_contract_at(Address("0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6"))
if not guard.view().is_compliant(proposal_id):
    raise gl.vm.UserError("[EXPECTED] proposal did not clear constitutional review")
```

`view()` is a synchronous read of the other contract's state during your
contract's execution. That matters: the constitutional check happens inside the
same transaction that would create the ballot, so there is no window in which a
non-compliant proposal has an open vote, and no off-chain keeper is trusted to
check first.

`is_compliant` returns `False` for an unknown proposal rather than reverting, so
you can call it on any identifier without handling an error. Both rejecting
rulings return `False`, including `AMENDMENT_REQUIRED`: a proposal that still
needs changes has not cleared the gate, and it should be amended and resubmitted
rather than voted on.

### A complete gated contract

`contracts/mandate_gated_dao.py` in this repository is a working ballot contract,
deployable as-is. Here is the shape of the gated action:

```python
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *


class MandateGatedDAO(gl.Contract):
    guard_address: Address
    dao_id: str
    ballots: TreeMap[str, Ballot]

    def __init__(self, guard_address: str, dao_id: str, voting_period_secs: int, quorum: int) -> None:
        self.guard_address = Address(str(guard_address).strip())
        self.dao_id = str(dao_id).strip().lower()
        # ...

    @gl.public.write
    def open_ballot(self, proposal_id: str) -> str:
        pid = str(proposal_id).strip()
        if pid in self.ballots:
            raise gl.vm.UserError("[EXPECTED] a ballot already exists for this proposal")

        guard = gl.get_contract_at(self.guard_address)

        # The gate.
        if not bool(guard.view().is_compliant(pid)):
            raise gl.vm.UserError(
                f"[EXPECTED] Proposal {pid} has not cleared constitutional review"
            )

        # The check passed. Copy the guard's reasoning into your own storage so
        # the justification for this vote is preserved locally and survives any
        # later change in the guard.
        status = json.loads(str(guard.view().compliance_status(pid)))
        if str(status.get("dao_id", self.dao_id)) != str(self.dao_id):
            raise gl.vm.UserError(f"[EXPECTED] Proposal {pid} belongs to a different DAO")

        self.ballots[pid] = Ballot(
            proposal_id=pid,
            guard_ruling=str(status.get("ruling", "")),
            guard_principle=str(status.get("principle", ""))[:240],
            guard_precedent_id=str(status.get("precedent_id", "")),
            # ...
        )
        return json.dumps({"proposal_id": pid, "guard_ruling": status.get("ruling")})
```

Three things in that snippet are worth copying rather than paraphrasing.

**Check the DAO id.** A proposal that cleared the gate for a different DAO is not
a proposal that cleared the gate for yours. Without this check, one DAO's gate
would launder proposals for another.

**Treat `guard_principle` as display text.** It is the sentence the panel wrote,
which the guard reports without binding, so it is the right thing to show a
member reading why a vote was permitted and the wrong thing to branch on. Branch
on `guard_ruling`, which is consensus-bound, and cite `guard_precedent_id` when
you need the bound case law itself.

**Read the gate, do not trust a stored flag.** `is_compliant` is a view, so you
read the guard's live state at the moment the ballot opens. A ruling replaced by
an appeal therefore governs from that moment. Copying the ruling into your own
storage *after* the check gives you an immutable local record without making the
check itself depend on a value you stored earlier. A ballot that legitimately
opened is not retroactively invalidated by a later appeal, and a proposal that
was rejected and then won its appeal becomes votable with no change to your
contract and no migration.

**Fail closed.** If the guard is unreachable, `guard.view().is_compliant(...)`
raises and your transaction reverts. Treating an unreachable gate as a pass would
defeat the entire mechanism. Do not wrap the gate call in a `try`.

### If you prefer the reason in your own revert

```python
guard.view().require_compliant(pid)
```

This returns `True` or reverts with the ruling's reason, so the rejection surfaces
in your contract's own revert message:

```
[EXPECTED] Proposal requires amendment before a vote (procedural_mandate):
State the Council resolution number and the signing quorum in the proposal body.
```

### Keying on proposal text instead of an id

If your DAO identifies proposals by their text rather than by the guard's id:

```python
found = json.loads(str(guard.view().find_adjudication(dao_id, title, body)))
if not found["found"]:
    raise gl.vm.UserError("[EXPECTED] this proposal has not been adjudicated")
if not found["is_compliant"]:
    raise gl.vm.UserError("[EXPECTED] this proposal did not clear review")
```

The digest is taken over the same normalized text `submit_proposal` uses, so a
re-cased or re-wrapped copy of an already-adjudicated proposal still resolves. The
lookup is scoped to the charter's current version, which is the version a new
submission would be judged under.

### Cross-contract call reference

| Pattern | Use |
| --- | --- |
| `gl.get_contract_at(addr).view().method(args)` | Synchronous read. Use this for the gate. |
| `gl.get_contract_at(addr).emit(on="finalized").method(args)` | Asynchronous write. Not needed for the gate. |

Do not use `emit` for the gate. It is asynchronous, so it cannot decide anything
inside the current transaction.

---

## 2. From a front end or backend

### Python, with `genlayer-py`

```python
from genlayer_py import create_account, create_client
from genlayer_py.chains import studionet

GUARD = "0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6"

account = create_account("0x...")
client = create_client(chain=studionet, account=account)

# Reads are free and instant.
compliant = client.read_contract(address=GUARD, function_name="is_compliant",
                                 args=["acme-dao#p0"])

import json
status = json.loads(client.read_contract(address=GUARD, function_name="compliance_status",
                                         args=["acme-dao#p0"]))
print(status["ruling"], status["mandate_class"])
print(status["principle"])
if status["ruling"] == "AMENDMENT_REQUIRED":
    print("required changes:", status["required_amendments"])
```

Writes go through consensus, so they take time and must be waited for:

```python
tx_hash = client.write_contract(
    address=GUARD,
    function_name="submit_proposal",
    args=["acme-dao", title, body],
    value=0,
)
receipt = client.wait_for_transaction_receipt(
    transaction_hash=tx_hash, retries=80, interval=3000
)
```

**Reading the outcome correctly.** `ACCEPTED` and `FINALIZED` are lifecycle
states, not proof that execution succeeded. A transaction can finalize with an
execution error and apply no state change. Read success from the leader's
`execution_result`:

```python
def leader_receipt(receipt) -> dict:
    """
    consensus_data.leader_receipt is a list. Its first entry is the executing
    leader; later entries include rotation slots, and an idle validator appears
    there with execution_result ERROR and a payload of "idle". Treating those as
    failures would report every successful transaction as a revert.
    """
    data = dict(receipt)
    leader = (data.get("consensus_data") or {}).get("leader_receipt") or []
    if isinstance(leader, dict):
        leader = [leader]
    return leader[0] if leader else {}


def succeeded(receipt) -> bool:
    return str(leader_receipt(receipt).get("execution_result", "")).upper() == "SUCCESS"


def revert_reason(receipt) -> str:
    result = leader_receipt(receipt).get("result") or {}
    payload = result.get("payload")
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("raw"), list):
        # A GenVM result is a one-byte result code followed by the body.
        return bytes(payload["raw"][1:]).decode("utf-8", errors="replace")
    return ""
```

`tests/test_live_studionet.py` uses exactly these helpers, so they are tested
against the live network on every run.

### The CLI

The method name is positional, and arguments follow `--args`:

```bash
genlayer network set studionet

genlayer call 0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6 \
  charter_exists --args acme-dao

genlayer call 0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6 \
  is_compliant --args "acme-dao#p0"

genlayer call 0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6 \
  active_precedent_corpus --args acme-dao

genlayer write 0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6 \
  submit_proposal --args acme-dao "My proposal title" "My proposal body..."

genlayer schema 0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6
genlayer code 0x67eAef1A9c24E9FFc365A20A1fe8b1c0323A82E6
```

Quote any argument containing a space, and quote proposal ids because `#` is
otherwise significant to your shell.

### Rendering the gate without hard-coding constants

```python
info = json.loads(client.read_contract(address=GUARD, function_name="get_registry_info"))
info["rulings"]          # ["COMPLIANT", "NON_COMPLIANT", "AMENDMENT_REQUIRED"]
info["mandate_classes"]  # the eight-value taxonomy, in priority order
info["min_body_len"]     # 40
info["max_body_len"]     # 6000
info["max_title_len"]    # 200
info["max_corpus"]       # 12
info["max_revisions"]    # 2
```

Your form validation and your labels should come from here, so a future change to
a bound does not silently break your client.

---

## 3. Registering your DAO

### Choose a constitution URL that is byte stable

This is the single most important decision, and getting it wrong is the most
common way a registration fails.

Registration fetches the document on every validator and requires them to agree on
the Keccak-256 of its normalized text, exactly. Normalization strips scripts,
styles, comments and HTML tags, decodes the common entities, collapses every
whitespace run to one space, and case folds, so indentation, line endings and
surrounding markup do not matter. What does matter is the text itself.

| Source | Suitable | Why |
| --- | --- | --- |
| IPFS gateway with a CID path | Yes | Content addressed and immutable. |
| Arweave transaction | Yes | Immutable. |
| GitHub raw URL pinned to a commit SHA | Yes | `raw.githubusercontent.com/org/repo/<sha>/charter.md`. |
| Gist raw URL pinned to a revision | Yes | The live suite uses one. |
| A branch-pinned raw URL (`.../main/charter.md`) | No | Moves when the branch moves. |
| A docs site page | Usually not | Rendered nav, build ids, analytics and timestamps change between fetches. |
| A Notion or Google Docs share link | No | Dynamic and often not plain HTML. |

If registration fails with a consensus error, the document is not stable across
near-simultaneous fetches. Fetch it twice yourself and compare:

```bash
for i in 1 2 3; do curl -sS "$URL" | sha256sum; done
```

Three identical hashes is a good sign; three different ones means find another
source. Failing at registration is the intended behaviour, because a URL that
cannot support a stable fingerprint cannot support the claim that a ruling was
made against a known text.

### Write a constitution a panel can apply

The gate reads your charter literally. Two properties make it far more useful:

**State numbers.** "The Treasury may not allocate more than ten percent of
reserves to a single counterparty in any calendar quarter, where reserves means
the total assets held by the Treasury at the opening of that quarter" is
adjudicable. "The Treasury should be managed prudently" is not.

**Define your terms.** If "grant", "tranche" and "allocation" mean different
things in your protocol, say so. Otherwise the first proposal that exploits the
ambiguity becomes your precedent on it.

The panel is instructed that silence is permission: if your constitution does not
restrict something, the proposal is compliant. It will not infer restrictions from
general principle or from what a prudent DAO would do. That is deliberate, because
a gate that invented restrictions would be unpredictable, but it means anything
you want enforced has to be written down.

### Register

```python
tx_hash = client.write_contract(
    address=GUARD,
    function_name="register_charter",
    args=[
        "acme-dao",                                   # dao_id, canonicalized
        "Acme Protocol",                              # display name
        "https://ipfs.io/ipfs/bafy.../charter.md",    # immutable source
        3600,                                         # submission cooldown, seconds
        False,                                        # restrict_proposers
        True,                                         # enforce_pinning
    ],
    value=0,
)
```

| Parameter | Notes |
| --- | --- |
| `dao_id` | Trimmed, lower-cased, whitespace collapsed to single hyphens. `"Acme DAO"` and `"acme-dao"` are the same charter. Max 64 characters after normalization. |
| `submission_cooldown_secs` | Per address. `0` disables. Max 30 days. Every submission spends a model call on every validator, so a non-zero value is recommended for an open charter. |
| `restrict_proposers` | When `True`, only allowlisted addresses may submit. The allowlist can be populated before or after registration. |
| `enforce_pinning` | When `True` (recommended), a live document that no longer matches the pin blocks adjudication. When `False`, drift is recorded on the ruling and surfaced in the views but does not stop the gate. |

The caller becomes the charter steward, controlling amendments, the allowlist,
landmarking and overruling. Transfer it to your governance multisig immediately:

```python
client.write_contract(address=GUARD, function_name="transfer_stewardship",
                      args=["acme-dao", "0xYourMultisig..."], value=0)
```

### Submit proposals

```python
tx_hash = client.write_contract(
    address=GUARD,
    function_name="submit_proposal",
    args=["acme-dao", title, body],
    value=0,
)
```

The returned JSON carries the full ruling:

```json
{
  "proposal_id": "acme-dao#p7",
  "ruling": "AMENDMENT_REQUIRED",
  "is_compliant": false,
  "mandate_class": "procedural_mandate",
  "principle": "A treasury proposal must name the Council resolution that authorises the transfer.",
  "holding_digest": "9f2c41be07d3a5...",
  "required_amendments": "State the Council resolution number and the signing quorum in the proposal body.",
  "constitution_clause": "Article II, section 4.",
  "rationale": "The substance is permissible but the authorising resolution is not identified.",
  "cited_precedents": "acme-dao#r2,acme-dao#r5",
  "constitution_drift": false,
  "precedent_id": "acme-dao#r9",
  "charter_version": 2,
  "corpus_digest": "46d1525aeaeb87...",
  "corpus_size": 8
}
```

Three of those fields were agreed by every validator that accepted the
transaction: `ruling`, `mandate_class` and `constitution_drift`. All three are
closed vocabularies compared character for character. The rest are the leader's,
reported for human review. The distinction matters when you build on this, so do
not treat `principle`, `rationale` or `constitution_clause` as though the panel
agreed on them; `get_registry_info` returns the three lists under
`consensus_bound_fields`, `derived_precedent_fields` and
`reported_unbound_fields` if you want to surface them in a UI.

`principle` is the sentence this panel recorded as the rule its decision turned
on. It is unbound, and it deliberately never reaches a later adjudication. Show
it to a human reading why a proposal failed; do not build logic on it, and do not
present it as something the validators certified.

What later adjudications consume is the holding, which the contract derives from
the three bound fields plus the proposal's own title and the charter version:

```
Under charter v2, a proposal titled "Fund the quarterly security audit" was
ruled AMENDMENT_REQUIRED, on the ground of procedural_mandate.
```

Because every input is bound or already on chain, every validator would render
that string identically, so the case law carries nothing one node authored
alone. `holding_digest` is the Keccak-256 of its canonical key: it is stored on
both the ruling and the precedent, you can recompute it from the registry text,
and it is mixed into `corpus_digest`. Read `get_precedent` for the holding
itself, which that view returns as `holding`.

The reason the holding is derived rather than agreed is empirical, and the README
records the measurement: five independent panels given the identical prompt wrote
five substantively different rules, one capping a grant per grant and another per
quarter, so binding the sentence either deadlocks consensus or admits
incompatible rules. The decision and its ground are what panels do agree on, so
they are what the registry is built from.

If a panel cannot agree on the ruling, the ground or the drift flag, the
transaction fails consensus and nothing is written.

A proposal that states its figures plainly is still easier to adjudicate, and
is less likely to need a validator rotation.

Bodies must be 40 to 6000 characters. Write the proposal as the thing to be
adjudicated: state the amount, the recipient, the authority relied on, and the
procedural steps already taken. A proposal that omits its authorising resolution
will often come back `AMENDMENT_REQUIRED` for exactly that reason, which is the
gate working.

### Maintain the precedent registry

The registry grows on its own, but two steward actions shape how it is read.

**Landmark the rulings that should bind.** A landmark is taken into every later
corpus ahead of ordinary rulings and presented as binding, and it lives in its own
index so it is never pushed out by the volume of routine decisions.

```python
client.write_contract(address=GUARD, function_name="mark_landmark",
                      args=["acme-dao#r3"], value=0)
```

**Overrule the rulings that should not.** An overruled precedent leaves the active
corpus but is never deleted: the entry, the reason and the timestamp stay readable,
because a constitutional court that could erase its own history would not be one.

```python
client.write_contract(address=GUARD, function_name="overrule_precedent",
                      args=["acme-dao#r3", "Superseded by the revised funding policy"],
                      value=0)
```

Inspect what the next adjudication will actually be bound by before you submit:

```python
corpus = json.loads(client.read_contract(address=GUARD,
                                         function_name="active_precedent_corpus",
                                         args=["acme-dao"]))
print(corpus["corpus_digest"], corpus["corpus_size"])
print(corpus["rendered"])   # exactly what the panel reads, in order
```

Compare that digest against the `corpus_digest` stored on any past ruling to
confirm which precedents produced it.

### Amend the constitution

```python
client.write_contract(
    address=GUARD,
    function_name="ratify_amendment",
    args=["acme-dao", new_url, "Raise the single grant ceiling to 250,000 USDC"],
    value=0,
)
```

The version increments, the previous URL and fingerprint go into the amendment log,
and the note is mandatory because it is the record. Re-pinning an identical
document is rejected.

After an amendment:

- Existing precedents stay readable but are labelled in later corpora as decided
  under a superseded version, and treated as persuasive rather than binding.
- The replay index is scoped by version, so a proposal rejected under the old text
  may be resubmitted.
- If `enforce_pinning` is on and the document changed without an amendment,
  adjudication is blocked until you ratify. That is the intended remedy, and the
  revert message says so.

---

## Running your own registry

The deployed instance is multi-tenant and there is usually no reason to deploy
your own. Deploy one if you want to change the bounds, run on a different network,
or keep your governance history in a contract you control.

```bash
git clone https://github.com/dotmantissa/governance-mandate-guard
cd governance-mandate-guard

python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/genvm-lint download --version v0.2.16

.venv/bin/genvm-lint check contracts/governance_mandate_guard.py --json
.venv/bin/python -m pytest tests/ -v

genlayer network set studionet
genlayer deploy --contract contracts/governance_mandate_guard.py \
  --args "My Governance Registry"
```

The constants worth tuning are grouped at the top of the contract:

| Constant | Default | Effect |
| --- | --- | --- |
| `MAX_CORPUS` | 12 | Precedents handed to a single adjudication. Higher means more context and more tokens. |
| `MAX_CORPUS_LANDMARKS` | 6 | How many of those slots landmarks may take. |
| `MAX_CORPUS_SCAN` | 64 | Bound on the backward walk over ordinary precedents. |
| `MAX_BODY_LEN` | 6000 | Longest proposal accepted. |
| `MAX_PRINCIPLE_LEN` | 240 | Longest panel principle stored on a ruling. |
| `MIN_PRINCIPLE_LEN` | 12 | Shortest principle accepted. Below this a reply cannot state a rule and is an `[LLM_ERROR]`. |
| `MAX_TITLE_LEN` | 200 | Longest proposal title, and the title is quoted inside every derived holding. |
| `MAX_REVISIONS` | 2 | One original ruling plus one appeal. |
| `MANDATE_CLASSES` | eight values | The taxonomy. Changing it changes what validators must agree on. |

If you change `MANDATE_CLASSES`, keep it a closed set in priority order, and keep
`MANDATE_KEYWORDS` covering every entry. The normalization has to be a total
function or validators will disagree on phrasing rather than on judgment.

`MIN_PRINCIPLE_LEN` and `MAX_PRINCIPLE_LEN` bound the panel's own principle
sentence, which is reported for human review and is never read into a later
adjudication. They affect what a steward sees, not what consensus binds, so they
are safe to move.

`MAX_TITLE_LEN` is different, because the title is quoted inside every derived
holding. Lowering it truncates the facts a later panel reasons from, and changing
it changes the text of holdings written after the change, so previously stored
holdings and their digests stay as they were. That is correct, since a precedent
records what was decided at the time, but it does mean two holdings of the same
proposal under different limits are not byte-identical.

If you edit the adjudication prompt, keep the sentence telling the panel that its
ruling and its ground are compared exactly and that the case law is generated by
the contract from them. The panel should spend its care on the ruling and the
ground, because those are the fields that must match. Telling it instead that its
principle sentence must agree with other validators would be false, and a prompt
that misdescribes the consensus rule makes the model optimize for the wrong
thing.

---

## Testing your integration

The in-process harness in `tests/genvm_host.py` runs the real SDK, so you can test
a consumer contract against a real guard with no network:

```python
import json, sys
sys.path.insert(0, "tests")
from genvm_host import bootstrap

host = bootstrap()

CONSTITUTION = "Article I. Purpose. Article II. No single grant may exceed 50,000 USDC."
host.mock_web(r"acme\.example/charter", {"status": 200, "body": CONSTITUTION})
host.mock_llm(r"constitutional review panel", json.dumps({
    "ruling": "COMPLIANT",
    "mandate_class": "none",
    "principle": "A grant within the stated ceiling is permissible.",
    "constitution_clause": "Article II",
    "rationale": "Within the cap.",
    "required_amendments": "",
    "cited_precedents": [],
}))

steward = host.new_address()
proposer = host.new_address()
host.warp(1_800_000_000)

guard = host.deploy("contracts/governance_mandate_guard.py", "GovernanceMandateGuard",
                    args=["Test registry"], sender=steward)
guard.register_charter("acme-dao", "Acme", "https://acme.example/charter",
                       0, False, True, sender=steward)

verdict = json.loads(guard.submit_proposal(
    "acme-dao", "Docs grant",
    "Award 40,000 USDC to the documentation working group for the coming quarter.",
    sender=proposer,
))
assert verdict["ruling"] == "COMPLIANT"

# Now point your own contract at it. This uses the reference consumer; swap in
# your own contract path and class name.
dao = host.deploy("contracts/mandate_gated_dao.py", "MandateGatedDAO",
                  args=[guard.address, "acme-dao", 3600, 1], sender=steward)
dao.set_voter(proposer, True, sender=steward)
dao.open_ballot(verdict["proposal_id"], sender=proposer)
assert dao.ballot_exists(verdict["proposal_id"]) is True
```

Cross-contract view calls run for real through the harness, so the gate is
exercised rather than stubbed.

Routes are matched in registration order, so to change an answer you clear the
routes and register the new set. Forgetting the clear leaves the first answer in
effect, which quietly tests the wrong path.

To test that your contract fails closed when the gate rejects:

```python
import pytest

host.clear_routes()
host.mock_web(r"acme\.example/charter", {"status": 200, "body": CONSTITUTION})
host.mock_llm(r"constitutional review panel", json.dumps({
    "ruling": "NON_COMPLIANT",
    "mandate_class": "treasury_mandate",
    "principle": "A grant above the ceiling is impermissible.",
    "constitution_clause": "Article II",
    "rationale": "Over the cap.",
    "required_amendments": "",
    "cited_precedents": [],
}))
rejected = json.loads(guard.submit_proposal("acme-dao", "Oversized",
    "Award 900,000 USDC to a single vendor for infrastructure this year.",
    sender=proposer))
assert rejected["ruling"] == "NON_COMPLIANT"

with pytest.raises(Exception):
    dao.open_ballot(rejected["proposal_id"], sender=proposer)
assert dao.ballot_exists(rejected["proposal_id"]) is False
```

And that it fails closed when the guard is unreachable:

```python
orphan = host.deploy("contracts/mandate_gated_dao.py", "MandateGatedDAO",
                     args=[host.new_address(), "acme-dao", 3600, 1], sender=steward)
with pytest.raises(Exception):
    orphan.open_ballot(verdict["proposal_id"], sender=proposer)
assert orphan.get_ballot_count() == 0
```

Giving `mock_llm` a list of two replies makes the leader take the first and the
validator take the second, which is how you test that your consumer behaves
correctly when an adjudication fails consensus:

```python
from genvm_host import ConsensusFailure

host.mock_llm(r"constitutional review panel", [compliant_reply, non_compliant_reply])
with pytest.raises(ConsensusFailure):
    guard.submit_proposal("acme-dao", "Contested", body, sender=proposer)
assert guard.get_proposal_count("acme-dao") == 1   # nothing was written
```

---

## Common problems

**Registration fails with a consensus error.** The document is not byte stable
across near-simultaneous fetches. Use an immutable source.

**Adjudication reverts with "no longer matches the pinned document".** The
constitution changed since you pinned it. Call `ratify_amendment`, or register the
charter with `enforce_pinning=False` if you intend to adjudicate against a living
document.

**"An identical proposal was already adjudicated under charter version N".** The
replay index matched on normalized text. Change the proposal, or amend the charter
if the provision that rejected it has itself changed.

**"Submission cooldown of Ns has not elapsed for this address".** Wait, or submit
from another address. `submission_available_at(dao_id, proposer)` returns the
earliest timestamp at which that address may submit again.

**"Address is not on the proposer allowlist".** The charter was registered with
`restrict_proposers=True`. The steward must call `set_proposer_allowed`.

**A submission fails consensus repeatedly.** Validators are reaching different
rulings, which means the proposal is genuinely borderline against your
constitution, or your constitution is too vague to adjudicate on the point at
issue. Both are useful signals. Tighten the provision, or make the proposal
explicit about the question the panel is splitting on.

**`is_compliant` returns `False` for a proposal you just submitted.** Check the
ruling with `compliance_status`. `AMENDMENT_REQUIRED` is a `False`, and it comes
with the specific changes needed in `required_amendments`.

**HTTP 429 or error `-32429`.** StudioNet rate limits per IP: 60 requests per
minute, 1000 per hour, 10000 per day. Pace your calls, or run against a local
Studio.

**Error `-32028`.** The pending-queue cap of 32 in-flight transactions per sender.
Wait for each receipt before submitting the next.
