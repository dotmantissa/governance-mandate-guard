"""
Live end-to-end tests against the deployed contract on GenLayer StudioNet.

These run the real thing: five validators, real HTTP fetches of a real public
document, real model calls and real Optimistic Democracy consensus. Nothing here
is simulated, and the constitution is a revision-pinned gist, which is exactly
the kind of immutable source the primitive tells DAOs to use.

The suite is ordered and stateful on purpose, because the point is the arc a DAO
actually walks: pin a charter, clear one proposal, reject another, watch the
precedent registry grow, gate a real ballot in another contract, amend the
constitution, and see the appeal reach a different answer because the text
changed under it.

Enable with:

    GENLAYER_PRIVATE_KEY=0x... GUARD_ADDRESS=0x... \
      .venv/bin/python -m pytest tests/test_live_studionet.py -v -s

Without `GENLAYER_PRIVATE_KEY` the whole module is skipped, so the offline suite
stays runnable with no network and no keys.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time

from pathlib import Path

import pytest

from genlayer_py import create_account, create_client
from genlayer_py.chains import studionet

# The outside reviewer's independent implementation of the contract's
# `_render_holding`, so this suite checks the deployed derivation rather than
# agreeing with it.
from conftest import holding_of

ROOT = Path(__file__).resolve().parent.parent
GUARD_SOURCE = ROOT / "contracts" / "governance_mandate_guard.py"
DAO_SOURCE = ROOT / "contracts" / "mandate_gated_dao.py"

GIST = "473984450835cbb0e139fa39cc67f640"
REVISION = "015a8e292e0f52c0c44600284fa5c70c150a36cf"
BASE = f"https://gist.githubusercontent.com/dotmantissa/{GIST}/raw/{REVISION}"
CONSTITUTION_V1 = f"{BASE}/meridian-constitution-v1.txt"
CONSTITUTION_V2 = f"{BASE}/meridian-constitution-v2.txt"

DAO_ID = os.environ.get("LIVE_DAO_ID", "meridian-live")

# Article II section 2 caps a single grant at 50,000 USDC in v1 and at
# 250,000 USDC in v2. These two proposals sit either side of the v1 ceiling, and
# the larger one sits below the v2 ceiling, so amending the document is what
# changes its ruling.
COMPLIANT_TITLE = "Fund the documentation working group for Q3"
COMPLIANT_BODY = (
    "Allocate 40,000 USDC from the Treasury to the documentation working group for "
    "the third quarter. The transfer is authorised by Council resolution 2027-11, "
    "adopted at the Council meeting of 2 June, and will be signed under the standard "
    "Council multisig quorum. The working group maintains the borrower and liquidator "
    "documentation that the lending market depends on. This proposal was published for "
    "review on 1 June, more than seven days before voting opens."
)
OVERSIZED_TITLE = "Fund the security audit programme for the year"
OVERSIZED_BODY = (
    "Allocate 120,000 USDC from the Treasury to the security audit programme for the "
    "coming year. The transfer is authorised by Council resolution 2027-12 and will be "
    "signed under the standard Council multisig quorum. Opening reserves for the quarter "
    "were 9,400,000 USDC. This proposal was published for review on 1 June, more than "
    "seven days before voting opens."
)
APPEAL_GROUNDS = (
    "The constitution has since been amended. Article II section 2 now sets the single "
    "grant ceiling at 250,000 USDC, and this allocation of 120,000 USDC is below that "
    "ceiling. Please adjudicate against the currently ratified text."
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("GENLAYER_PRIVATE_KEY"),
        reason="Set GENLAYER_PRIVATE_KEY to explicitly enable live network tests",
    ),
]


# ---------------------------------------------------------------------------
# Session state, shared across the ordered walk
# ---------------------------------------------------------------------------

STATE: dict = {}


@pytest.fixture(scope="session")
def client():
    account = create_account(os.environ["GENLAYER_PRIVATE_KEY"])
    return create_client(chain=studionet, account=account)


@pytest.fixture(scope="session", autouse=True)
def fresh_dao(client, guard_address):
    """
    The walk below is stateful and ordered, so it has to start from a DAO that has
    never been registered on this contract.

    Re-running it against a DAO that already exists would leave the shared STATE
    half populated and fail eight unrelated tests, which reads as a broken
    contract when it is really a reused fixture. Skip the module with the reason
    instead, so a re-run costs a second rather than an afternoon.
    """
    if read(client, guard_address, "charter_exists", [DAO_ID]):
        pytest.skip(
            f"{DAO_ID} is already registered on {guard_address}. Set LIVE_DAO_ID to "
            f"a fresh value for a clean walk, or deploy a fresh guard."
        )


@pytest.fixture(scope="session")
def guard_address() -> str:
    address = os.environ.get("GUARD_ADDRESS")
    if not address:
        pytest.skip("Set GUARD_ADDRESS to the deployed Governance Mandate Guard")
    return address


def settle(client, tx_hash, retries: int = 80, interval: int = 3000):
    """
    Wait for a transaction to be accepted.

    ACCEPTED is a lifecycle state, not proof that execution succeeded, so every
    caller checks the execution result separately.
    """
    return client.wait_for_transaction_receipt(
        transaction_hash=tx_hash, retries=retries, interval=interval
    )


def leader_receipt(receipt) -> dict:
    """
    The actual leader's receipt.

    `consensus_data.leader_receipt` is a list whose first entry is the executing
    leader; later entries are rotation slots, and an idle validator appears there
    with `execution_result: ERROR` and a payload of "idle". Treating those as
    execution failures would report every successful transaction as a revert, so
    only the first entry is the execution of record.
    """
    data = receipt if isinstance(receipt, dict) else dict(receipt)
    leader = (data.get("consensus_data") or {}).get("leader_receipt") or []
    if isinstance(leader, dict):
        leader = [leader]
    return leader[0] if leader else {}


def execution_succeeded(receipt) -> bool:
    """
    Whether contract execution succeeded.

    ACCEPTED and FINALIZED are lifecycle states: a transaction can finalize with
    an execution error and apply no state change, so success is read from the
    leader's `execution_result`, never from the status.
    """
    return str(leader_receipt(receipt).get("execution_result", "")).upper() == "SUCCESS"


def revert_reason(receipt) -> str:
    """The contract's revert message, decoded from the leader's result."""
    result = leader_receipt(receipt).get("result") or {}
    if not isinstance(result, dict):
        return str(result)[:400]

    payload = result.get("payload")
    if isinstance(payload, str) and payload:
        return payload
    if isinstance(payload, dict):
        raw = payload.get("raw")
        if isinstance(raw, list):
            # A GenVM result is a one-byte result code followed by the body.
            return bytes(raw[1:]).decode("utf-8", errors="replace")
        if isinstance(raw, str):
            try:
                return base64.b64decode(raw)[1:].decode("utf-8", errors="replace")
            except Exception:
                return raw

    raw = result.get("raw")
    if isinstance(raw, str):
        try:
            return base64.b64decode(raw)[1:].decode("utf-8", errors="replace")
        except Exception:
            return raw
    return json.dumps(result)[:400]


def write(client, address, method, args, expect_success: bool = True):
    """Submit a write, wait for it, and record the outcome for the artifact."""
    tx_hash = client.write_contract(address=address, function_name=method, args=args, value=0)
    receipt = settle(client, tx_hash)
    succeeded = execution_succeeded(receipt)
    data = dict(receipt)
    consensus = data.get("consensus_data") or {}
    votes = consensus.get("votes") or {}
    entry = {
        "operation": method,
        "transactionHash": tx_hash if isinstance(tx_hash, str) else tx_hash.hex(),
        "expected": "success" if expect_success else "revert",
        "outcome": "success" if succeeded else "revert",
        "status": str(data.get("status_name", "")),
        "consensusResult": str(data.get("result_name", "")),
        "votes": {k: str(v) for k, v in votes.items()},
        "agreeCount": sum(1 for v in votes.values() if str(v) == "agree"),
        "initialValidators": data.get("num_of_initial_validators"),
        "rotations": data.get("rotation_count"),
    }
    if not succeeded:
        entry["revertReason"] = revert_reason(receipt)
    STATE.setdefault("transactions", []).append(entry)
    print(f"\n  {method}: {entry['outcome']} | {entry['consensusResult']} "
          f"| {entry['agreeCount']} agree | {entry['transactionHash']}")
    if not succeeded and entry.get("revertReason"):
        print(f"    reason: {entry['revertReason'][:200]}")
    return succeeded, receipt


def read(client, address, method, args=None):
    return client.read_contract(address=address, function_name=method, args=args or [])


def read_json(client, address, method, args=None):
    value = read(client, address, method, args)
    return json.loads(value) if isinstance(value, str) else value


# ---------------------------------------------------------------------------
# 1. The deployed contract is the reviewed contract
# ---------------------------------------------------------------------------

def test_deployed_source_matches_the_repository(client, guard_address):
    """
    The bytes running on StudioNet are byte-for-byte the file in this repository.
    Without this check every other live result describes some other program.
    """
    result = client.provider.make_request("gen_getContractCode", [guard_address])
    encoded = result["result"] if isinstance(result, dict) else result
    deployed = base64.b64decode(encoded)
    local = GUARD_SOURCE.read_bytes()

    STATE["sourceSha256"] = hashlib.sha256(local).hexdigest()
    assert deployed == local, "the deployed contract is not the source in this repository"
    assert len(local) == len(deployed)
    print(f"\n  source sha256: {STATE['sourceSha256']} ({len(local)} bytes)")


def test_the_published_schema_matches_the_contract(client, guard_address):
    schema = client.get_contract_schema(guard_address)
    if isinstance(schema, str):
        schema = json.loads(schema)
    methods = set((schema.get("methods") or {}).keys())

    for required in ("register_charter", "submit_proposal", "appeal_ruling", "is_compliant",
                     "require_compliant", "compliance_status", "active_precedent_corpus",
                     "mark_landmark", "overrule_precedent", "ratify_amendment"):
        assert required in methods, f"{required} is missing from the deployed schema"
    assert schema["ctor"]["params"] == [["registry_name", "string"]]
    STATE["schemaMethods"] = sorted(methods)


# ---------------------------------------------------------------------------
# 2. Pinning a real constitution under real consensus
# ---------------------------------------------------------------------------

def test_register_the_charter_against_a_real_document(client, guard_address):
    """
    Five validators independently fetch the gist and must agree on the
    Keccak-256 of its normalized text before the charter exists.
    """
    if read(client, guard_address, "charter_exists", [DAO_ID]):
        pytest.skip(f"{DAO_ID} is already registered on this contract")

    ok, _ = write(client, guard_address, "register_charter",
                  [DAO_ID, "Meridian Protocol", CONSTITUTION_V1, 0, False, True])
    assert ok, "charter registration failed"

    charter = read_json(client, guard_address, "get_charter", [DAO_ID])
    assert charter["version"] == 1
    assert charter["constitution_url"] == CONSTITUTION_V1
    assert len(charter["doc_fingerprint"]) == 64
    assert charter["doc_chars"] > 500
    assert charter["active"] is True
    assert charter["enforce_pinning"] is True

    STATE["pinnedFingerprint"] = charter["doc_fingerprint"]
    print(f"\n  pinned fingerprint: {charter['doc_fingerprint']}")


# ---------------------------------------------------------------------------
# 3. Adjudication: the gate reaches the right answers on real text
# ---------------------------------------------------------------------------

def test_a_proposal_within_the_ceiling_clears_the_gate(client, guard_address):
    """
    40,000 USDC is below the Article II ceiling of 50,000 and names its
    authorising Council resolution, so the panel should let it through.
    """
    ok, _ = write(client, guard_address, "submit_proposal",
                  [DAO_ID, COMPLIANT_TITLE, COMPLIANT_BODY])
    assert ok, "submission of a compliant proposal failed"

    index = read(client, guard_address, "get_proposal_count", [DAO_ID]) - 1
    proposal_id = read(client, guard_address, "get_proposal_id_at", [DAO_ID, index])
    STATE["compliantProposalId"] = proposal_id

    ruling = read_json(client, guard_address, "get_ruling", [proposal_id])
    print(f"\n  {proposal_id}: {ruling['ruling']} ({ruling['mandate_class']})")
    print(f"    principle: {ruling['principle']}")

    assert ruling["ruling"] == "COMPLIANT", (
        f"expected COMPLIANT, got {ruling['ruling']}: {ruling['rationale']}"
    )
    assert ruling["mandate_class"] == "none"
    assert ruling["constitution_drift"] is False
    assert ruling["live_fingerprint"] == STATE["pinnedFingerprint"]
    assert ruling["principle"], "a ruling must state its principle"
    assert read(client, guard_address, "is_compliant", [proposal_id]) is True
    STATE["compliantPrecedentId"] = ruling["precedent_id"]
    STATE["adjudication"] = {"compliant": ruling["principle"]}


def test_identical_text_cannot_be_resubmitted(client, guard_address):
    """The replay index is enforced on chain, not only in local tests."""
    ok, receipt = write(client, guard_address, "submit_proposal",
                        [DAO_ID, COMPLIANT_TITLE, COMPLIANT_BODY], expect_success=False)
    assert not ok, "an identical proposal was adjudicated twice"
    assert "already adjudicated" in revert_reason(receipt)


def test_a_proposal_over_the_ceiling_is_refused(client, guard_address):
    """
    120,000 USDC is above the Article II ceiling of 50,000 in version 1. The
    panel has to read the number out of the document to get this right.
    """
    ok, _ = write(client, guard_address, "submit_proposal",
                  [DAO_ID, OVERSIZED_TITLE, OVERSIZED_BODY])
    assert ok, "submission of the oversized proposal failed"

    index = read(client, guard_address, "get_proposal_count", [DAO_ID]) - 1
    proposal_id = read(client, guard_address, "get_proposal_id_at", [DAO_ID, index])
    STATE["rejectedProposalId"] = proposal_id

    ruling = read_json(client, guard_address, "get_ruling", [proposal_id])
    print(f"\n  {proposal_id}: {ruling['ruling']} ({ruling['mandate_class']})")
    print(f"    principle: {ruling['principle']}")

    assert ruling["ruling"] in ("NON_COMPLIANT", "AMENDMENT_REQUIRED"), (
        f"a 120,000 USDC grant must not clear a 50,000 USDC ceiling: {ruling['rationale']}"
    )
    assert ruling["mandate_class"] != "none"
    assert read(client, guard_address, "is_compliant", [proposal_id]) is False
    STATE["rejectedPrecedentId"] = ruling["precedent_id"]
    STATE["rejectedRuling"] = ruling["ruling"]
    STATE["adjudication"]["rejected"] = ruling["principle"]


def test_the_precedent_registry_accumulated_both_rulings(client, guard_address):
    corpus = read_json(client, guard_address, "active_precedent_corpus", [DAO_ID])
    ids = [entry["precedent_id"] for entry in corpus["precedents"]]

    assert STATE["compliantPrecedentId"] in ids
    assert STATE["rejectedPrecedentId"] in ids
    assert ids[0] == STATE["rejectedPrecedentId"], "newest first"
    assert len(corpus["corpus_digest"]) == 64

    titles = {
        STATE["compliantProposalId"]: COMPLIANT_TITLE,
        STATE["rejectedProposalId"]: OVERSIZED_TITLE,
    }
    for entry in corpus["precedents"]:
        # The reviewer's defect, asserted against the live deployment: no leader
        # prose reaches a later panel.
        assert "principle" not in entry, (
            f"{entry['precedent_id']}: a corpus entry carries unbound leader prose"
        )
        assert entry["holding"], "every precedent carries a usable holding"
        assert len(entry["holding_digest"]) == 64

        # Re-derive it here, from the bound fields plus on chain data, and require
        # the deployed bytes to match. This is the whole claim of the design.
        ruling = read_json(client, guard_address, "get_ruling", [entry["proposal_id"]])
        expected = holding_of(
            ruling["ruling"],
            ruling["mandate_class"],
            ruling["constitution_drift"],
            titles[entry["proposal_id"]],
            entry["charter_version"],
        )
        assert entry["holding"] == expected, (
            f"{entry['precedent_id']}: deployed holding is not the derived holding\n"
            f"  deployed: {entry['holding']}\n  derived:  {expected}"
        )

    charter = read_json(client, guard_address, "get_charter", [DAO_ID])
    assert charter["precedent_total"] == charter["proposal_total"] == 2
    assert charter["compliant_total"] == 1
    print(f"\n  corpus digest: {corpus['corpus_digest']} over {corpus['corpus_size']} rulings")


def test_require_compliant_reverts_for_the_rejected_proposal(client, guard_address):
    """
    The raising variant of the gate, over a real `gen_call`.

    StudioNet returns a generic "execution failed" for a reverting view rather
    than the contract's message, so the reason is read from `compliance_status`,
    which is the non-raising endpoint a front end would use for exactly that.
    """
    assert read(client, guard_address, "require_compliant",
                [STATE["compliantProposalId"]]) is True

    with pytest.raises(Exception):
        read(client, guard_address, "require_compliant", [STATE["rejectedProposalId"]])

    status = read_json(client, guard_address, "compliance_status",
                       [STATE["rejectedProposalId"]])
    assert status["adjudicated"] is True
    assert status["is_compliant"] is False
    assert status["ruling"] == STATE["rejectedRuling"]
    assert status["mandate_class"] != "none"
    assert len(status["holding_digest"]) == 64, (
        "compliance_status must publish the commitment to the derived holding"
    )


def test_a_landmark_is_recorded(client, guard_address):
    ok, _ = write(client, guard_address, "mark_landmark", [STATE["compliantPrecedentId"]])
    assert ok
    precedent = read_json(client, guard_address, "get_precedent", [STATE["compliantPrecedentId"]])
    assert precedent["landmark"] is True

    corpus = read_json(client, guard_address, "active_precedent_corpus", [DAO_ID])
    assert corpus["precedents"][0]["precedent_id"] == STATE["compliantPrecedentId"]
    assert "LANDMARK, binding" in corpus["rendered"]


# ---------------------------------------------------------------------------
# 4. The gate enforced from a second contract, on chain
# ---------------------------------------------------------------------------

def test_a_consuming_dao_can_only_open_a_ballot_on_a_compliant_proposal(client, guard_address):
    """
    Deploys the reference consumer and has it read the guard across a real
    cross-contract call while opening a ballot.
    """
    tx_hash = client.deploy_contract(
        code=DAO_SOURCE.read_bytes(),
        args=[guard_address, DAO_ID, 3600, 1],
    )
    receipt = settle(client, tx_hash)
    assert execution_succeeded(receipt), f"consumer deployment failed: {revert_reason(receipt)}"
    dao_address = dict(receipt).get("data", {}).get("contract_address") or dict(receipt).get("contract_address")
    assert dao_address, f"no contract address in receipt: {dict(receipt).keys()}"
    STATE["daoAddress"] = dao_address
    STATE.setdefault("transactions", []).append({
        "operation": "deploy MandateGatedDAO",
        "transactionHash": tx_hash if isinstance(tx_hash, str) else tx_hash.hex(),
        "expected": "success",
        "outcome": "success",
        "contractAddress": dao_address,
    })
    print(f"\n  consumer deployed at {dao_address}")

    config = read_json(client, dao_address, "get_config", [])
    assert config["guard_address"].lower() == guard_address.lower()
    assert config["dao_id"] == DAO_ID

    ok, _ = write(client, dao_address, "open_ballot", [STATE["compliantProposalId"]])
    assert ok, "a compliant proposal must be votable"
    ballot = read_json(client, dao_address, "get_ballot", [STATE["compliantProposalId"]])
    assert ballot["guard_ruling"] == "COMPLIANT"
    assert ballot["guard_precedent_id"] == STATE["compliantPrecedentId"]

    ok, receipt = write(client, dao_address, "open_ballot",
                        [STATE["rejectedProposalId"]], expect_success=False)
    assert not ok, "a rejected proposal must not be votable"
    assert "has not cleared constitutional review" in revert_reason(receipt)
    assert read(client, dao_address, "ballot_exists", [STATE["rejectedProposalId"]]) is False


# ---------------------------------------------------------------------------
# 5. Amendment, and an appeal decided by the amended text
# ---------------------------------------------------------------------------

def test_ratifying_an_amendment_repins_the_charter(client, guard_address):
    """Version 2 of the gist raises the single grant ceiling to 250,000 USDC."""
    ok, _ = write(client, guard_address, "ratify_amendment",
                  [DAO_ID, CONSTITUTION_V2,
                   "Raise the single grant ceiling in Article II section 2 to 250,000 USDC"])
    assert ok, "amendment ratification failed"

    charter = read_json(client, guard_address, "get_charter", [DAO_ID])
    assert charter["version"] == 2
    assert charter["constitution_url"] == CONSTITUTION_V2
    assert charter["doc_fingerprint"] != STATE["pinnedFingerprint"]

    amendment = read_json(client, guard_address, "get_amendment", [DAO_ID, 2])
    assert amendment["previous_fingerprint"] == STATE["pinnedFingerprint"]
    assert amendment["new_url"] == CONSTITUTION_V2
    STATE["amendedFingerprint"] = charter["doc_fingerprint"]
    print(f"\n  charter v2 fingerprint: {charter['doc_fingerprint']}")


def test_the_appeal_reaches_a_different_answer_under_the_amended_text(client, guard_address):
    """
    The strongest end-to-end signal in this suite. The identical proposal text is
    re-adjudicated against a constitution whose ceiling has moved above it, and
    the ruling flips. That can only happen if validators genuinely read the live
    document rather than reusing a cached conclusion.
    """
    ok, _ = write(client, guard_address, "appeal_ruling",
                  [STATE["rejectedProposalId"], APPEAL_GROUNDS])
    assert ok, "appeal failed"

    ruling = read_json(client, guard_address, "get_ruling", [STATE["rejectedProposalId"]])
    print(f"\n  {STATE['rejectedProposalId']} on appeal: {ruling['ruling']}")
    print(f"    principle: {ruling['principle']}")

    assert ruling["revision"] == 2
    assert ruling["charter_version"] == 2
    assert ruling["live_fingerprint"] == STATE["amendedFingerprint"]
    assert ruling["constitution_drift"] is False
    assert ruling["ruling"] == "COMPLIANT", (
        f"120,000 USDC is below the amended 250,000 ceiling: {ruling['rationale']}"
    )
    STATE["adjudication"]["appealed"] = ruling["principle"]

    superseded = read_json(client, guard_address, "get_ruling_revision",
                           [STATE["rejectedProposalId"], 1])
    assert superseded["ruling"] == STATE["rejectedRuling"]
    assert superseded["charter_version"] == 1

    retired = read_json(client, guard_address, "get_precedent", [STATE["rejectedPrecedentId"]])
    assert retired["overruled"] is True
    assert retired["superseded_by"] == ruling["precedent_id"]


def test_the_consuming_dao_now_admits_the_appealed_proposal(client, guard_address):
    """
    The consumer reads the guard live, so the successful appeal unblocks the
    ballot with no change to the consumer and no migration.
    """
    assert read(client, guard_address, "is_compliant", [STATE["rejectedProposalId"]]) is True
    ok, _ = write(client, STATE["daoAddress"], "open_ballot", [STATE["rejectedProposalId"]])
    assert ok, "the appealed proposal must now be votable"

    ballot = read_json(client, STATE["daoAddress"], "get_ballot", [STATE["rejectedProposalId"]])
    assert ballot["guard_ruling"] == "COMPLIANT"
    assert read(client, STATE["daoAddress"], "get_ballot_count", []) == 2


def test_overruling_retires_a_precedent_on_the_record(client, guard_address):
    ok, _ = write(client, guard_address, "overrule_precedent",
                  [STATE["compliantPrecedentId"], "Superseded by the amended funding policy"])
    assert ok

    precedent = read_json(client, guard_address, "get_precedent", [STATE["compliantPrecedentId"]])
    assert precedent["overruled"] is True
    assert precedent["overruled_reason"] == "Superseded by the amended funding policy"

    corpus = read_json(client, guard_address, "active_precedent_corpus", [DAO_ID])
    assert STATE["compliantPrecedentId"] not in [e["precedent_id"] for e in corpus["precedents"]]
    assert read(client, guard_address, "get_precedent_count", [DAO_ID]) == 3, (
        "the count is the historical record, not the live corpus"
    )


def test_write_the_verification_artifact(client, guard_address):
    """Record what this run actually observed, for the repository artifact."""
    charter = read_json(client, guard_address, "get_charter", [DAO_ID])
    corpus = read_json(client, guard_address, "active_precedent_corpus", [DAO_ID])

    artifact = {
        "network": "studionet",
        "rpc": "https://studio.genlayer.com/api",
        "chainId": 61999,
        "contractAddress": guard_address,
        "consumerAddress": STATE.get("daoAddress"),
        "sourceSha256": STATE.get("sourceSha256"),
        "deployedSourceMatchesRepository": True,
        "schemaMethods": STATE.get("schemaMethods"),
        "constitution": {
            "version1": CONSTITUTION_V1,
            "version2": CONSTITUTION_V2,
            "pinnedFingerprintV1": STATE.get("pinnedFingerprint"),
            "pinnedFingerprintV2": STATE.get("amendedFingerprint"),
        },
        "finalCharter": charter,
        "finalCorpus": {
            "digest": corpus["corpus_digest"],
            "size": corpus["corpus_size"],
            "precedents": corpus["precedents"],
        },
        "adjudication": STATE.get("adjudication"),
        "transactions": STATE.get("transactions", []),
        "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = ROOT / "artifacts" / "live-verification.json"
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(f"\n  wrote {path}")
    assert path.is_file()
