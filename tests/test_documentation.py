"""
Tests that keep the documentation honest.

Two failure modes matter here. Documented code that does not run wastes an
integrator's afternoon, and documented addresses or hashes that drift from the
artifacts make the deployment claims unverifiable. Both are checked mechanically
rather than by review, because both are exactly the kind of thing that rots
silently after the third edit.
"""

from __future__ import annotations

import json
import re

from pathlib import Path

import pytest

from conftest import GUARD_SOURCE, DAO_SOURCE
from genvm_host import ConsensusFailure

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
INTEGRATION = ROOT / "INTEGRATION.md"
DEPLOYMENT = ROOT / "artifacts" / "deployment.json"


# ---------------------------------------------------------------------------
# The documented integration actually runs
# ---------------------------------------------------------------------------

CONSTITUTION = "Article I. Purpose. Article II. No single grant may exceed 50,000 USDC."

COMPLIANT_REPLY = json.dumps({
    "ruling": "COMPLIANT",
    "mandate_class": "none",
    "principle": "A grant within the stated ceiling is permissible.",
    "constitution_clause": "Article II",
    "rationale": "Within the cap.",
    "required_amendments": "",
    "cited_precedents": [],
})
NON_COMPLIANT_REPLY = json.dumps({
    "ruling": "NON_COMPLIANT",
    "mandate_class": "treasury_mandate",
    "principle": "A grant above the ceiling is impermissible.",
    "constitution_clause": "Article II",
    "rationale": "Over the cap.",
    "required_amendments": "",
    "cited_precedents": [],
})


@pytest.fixture
def documented(host):
    """The setup from the INTEGRATION.md testing section, run verbatim."""
    host.mock_web(r"acme\.example/charter", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)

    steward = host.new_address()
    proposer = host.new_address()
    host.warp(1_800_000_000)

    guard = host.deploy(GUARD_SOURCE, "GovernanceMandateGuard",
                        args=["Test registry"], sender=steward)
    guard.register_charter("acme-dao", "Acme", "https://acme.example/charter",
                           0, False, True, sender=steward)

    verdict = json.loads(guard.submit_proposal(
        "acme-dao", "Docs grant",
        "Award 40,000 USDC to the documentation working group for the coming quarter.",
        sender=proposer,
    ))
    assert verdict["ruling"] == "COMPLIANT"
    return host, guard, verdict, steward, proposer


def test_the_documented_consumer_setup_gates_a_ballot(documented):
    host, guard, verdict, steward, proposer = documented

    dao = host.deploy(DAO_SOURCE, "MandateGatedDAO",
                      args=[guard.address, "acme-dao", 3600, 1], sender=steward)
    dao.set_voter(proposer, True, sender=steward)
    dao.open_ballot(verdict["proposal_id"], sender=proposer)
    assert dao.ballot_exists(verdict["proposal_id"]) is True


def test_the_documented_rejection_path_blocks_a_ballot(documented):
    host, guard, verdict, steward, proposer = documented

    dao = host.deploy(DAO_SOURCE, "MandateGatedDAO",
                      args=[guard.address, "acme-dao", 3600, 1], sender=steward)
    dao.set_voter(proposer, True, sender=steward)

    host.clear_routes()
    host.mock_web(r"acme\.example/charter", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", NON_COMPLIANT_REPLY)
    rejected = json.loads(guard.submit_proposal(
        "acme-dao", "Oversized",
        "Award 900,000 USDC to a single vendor for infrastructure this year.",
        sender=proposer,
    ))
    assert rejected["ruling"] == "NON_COMPLIANT"

    with pytest.raises(Exception):
        dao.open_ballot(rejected["proposal_id"], sender=proposer)
    assert dao.ballot_exists(rejected["proposal_id"]) is False


def test_the_documented_unreachable_guard_fails_closed(documented):
    host, guard, verdict, steward, proposer = documented

    orphan = host.deploy(DAO_SOURCE, "MandateGatedDAO",
                         args=[host.new_address(), "acme-dao", 3600, 1], sender=steward)
    with pytest.raises(Exception):
        orphan.open_ballot(verdict["proposal_id"], sender=proposer)
    assert orphan.get_ballot_count() == 0


def test_the_documented_disagreement_recipe_writes_nothing(documented):
    """The two-reply list that INTEGRATION.md documents for testing disagreement."""
    host, guard, verdict, steward, proposer = documented

    host.clear_routes()
    host.mock_web(r"acme\.example/charter", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", [COMPLIANT_REPLY, NON_COMPLIANT_REPLY])

    before = guard.get_proposal_count("acme-dao")
    with pytest.raises(ConsensusFailure):
        guard.submit_proposal(
            "acme-dao", "Contested",
            "Award 12,345 USDC to the research group for the coming quarter of work.",
            sender=proposer,
        )
    assert guard.get_proposal_count("acme-dao") == before


def test_the_documented_registry_info_keys_exist(documented):
    """A front end reads its bounds from here, so the documented keys must be real."""
    _host, guard, _verdict, _steward, _proposer = documented
    info = json.loads(guard.get_registry_info())
    for key in ("rulings", "mandate_classes", "min_body_len", "max_body_len",
                "max_title_len", "max_corpus", "max_revisions"):
        assert key in info, f"{key} is documented but not returned"
    assert info["rulings"] == ["COMPLIANT", "NON_COMPLIANT", "AMENDMENT_REQUIRED"]
    assert len(info["mandate_classes"]) == 8
    assert info["mandate_classes"][0] == "precedent_conflict"
    assert info["mandate_classes"][-1] == "none"


def test_the_documented_lookup_and_corpus_endpoints_work(documented):
    _host, guard, _verdict, _steward, _proposer = documented

    found = json.loads(guard.find_adjudication(
        "acme-dao", "Docs grant",
        "Award 40,000 USDC to the documentation working group for the coming quarter.",
    ))
    assert found["found"] is True
    assert found["is_compliant"] is True

    corpus = json.loads(guard.active_precedent_corpus("acme-dao"))
    assert len(corpus["corpus_digest"]) == 64
    assert corpus["rendered"]


# ---------------------------------------------------------------------------
# The documented facts match the artifacts
# ---------------------------------------------------------------------------

def test_the_documented_addresses_match_the_deployment_artifact():
    deployment = json.loads(DEPLOYMENT.read_text())
    guard = deployment["contracts"]["GovernanceMandateGuard"]
    consumer = deployment["contracts"]["MandateGatedDAO"]
    readme = README.read_text()

    assert guard["contractAddress"] in readme
    assert guard["deploymentTransaction"] in readme
    assert guard["sourceSha256"] in readme
    assert guard["explorer"] in readme
    assert deployment["deployerAddress"] in readme
    assert deployment["runner"] in readme
    assert consumer["contractAddress"] in readme


def test_the_documented_source_hash_matches_the_source_on_disk():
    import hashlib

    deployment = json.loads(DEPLOYMENT.read_text())
    for name, path in (("GovernanceMandateGuard", GUARD_SOURCE), ("MandateGatedDAO", DAO_SOURCE)):
        entry = deployment["contracts"][name]
        raw = path.read_bytes()
        assert entry["sourceSha256"] == hashlib.sha256(raw).hexdigest(), f"{name} hash is stale"
        assert entry["sourceBytes"] == len(raw), f"{name} byte count is stale"


def test_the_pinned_runner_matches_the_contract_headers():
    """
    Both contracts must pin the same runner the artifact and docs name. An
    unpinned or mismatched runner is rejected by every GenLayer network.
    """
    deployment = json.loads(DEPLOYMENT.read_text())
    runner = deployment["runner"]
    for path in (GUARD_SOURCE, DAO_SOURCE):
        head = path.read_text().splitlines()[:2]
        assert any(runner in line for line in head), f"{path.name} does not pin {runner}"
        assert '"Depends"' in head[1]


def test_every_documented_contract_method_exists():
    """
    Method names named in the docs must be on one of the contracts. Catches a
    rename that updated the code and not the guide.
    """
    guard_src = GUARD_SOURCE.read_text()
    dao_src = DAO_SOURCE.read_text()
    defined = set(re.findall(r"^\s{4}def (\w+)", guard_src, re.M))
    defined |= set(re.findall(r"^\s{4}def (\w+)", dao_src, re.M))

    documented = set()
    for path in (README, INTEGRATION):
        text = path.read_text()
        documented |= set(re.findall(r"`(\w+)\(", text))
        documented |= set(re.findall(r"^\| `(\w+)\(", text, re.M))

    # Names that are Python, SDK or CLI surface rather than contract methods.
    external = {
        "print", "json", "len", "str", "int", "bool", "dict", "list", "bytes",
        "Address", "isinstance", "open", "raises", "range", "sorted", "sum",
        "create_account", "create_client", "read_contract", "write_contract",
        "wait_for_transaction_receipt", "deploy_contract", "get_contract_at",
        "get_contract_schema", "loads", "dumps", "view", "emit", "bootstrap",
        "mock_web", "mock_llm", "clear_routes", "new_address", "warp", "deploy",
        "UserError", "run_nondet_unsafe", "gen_getContractCode", "leader_receipt",
        "succeeded", "revert_reason", "b64decode", "sha256", "hexdigest",
        "method", "decode", "get", "strip", "lower", "format", "date",
        "__init__", "Ballot", "MyDAO", "MandateGatedDAO", "GovernanceMandateGuard",
    }
    unknown = sorted(name for name in documented - defined - external
                     if not name.startswith("_"))
    assert not unknown, f"documented but not defined on any contract: {unknown}"


@pytest.mark.parametrize("path", [README, INTEGRATION], ids=["README", "INTEGRATION"])
def test_the_docs_contain_no_em_dashes(path):
    """A house style rule, checked rather than remembered."""
    text = path.read_text()
    offenders = [
        (i, line.strip()[:90])
        for i, line in enumerate(text.splitlines(), start=1)
        if "—" in line or "–" in line
    ]
    assert not offenders, f"{path.name} contains dash characters: {offenders}"


@pytest.mark.parametrize("path", [README, INTEGRATION], ids=["README", "INTEGRATION"])
def test_the_docs_have_balanced_code_fences(path):
    fences = re.findall(r"^```", path.read_text(), re.M)
    assert len(fences) % 2 == 0, f"{path.name} has an unclosed code fence"
