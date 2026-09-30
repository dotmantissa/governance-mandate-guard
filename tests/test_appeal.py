"""
The appeal path: one contested re-hearing, and what it does to the record.

An appeal is a full re-adjudication, not a review of the first one. The panel
fetches the constitution and reasons again, with the proposer's grounds put to it
as argument. The precedent created by the contested ruling is retired before the
new corpus is selected, so an appeal is never bound by the very ruling it
contests.
"""

import json

import pytest

from conftest import (
    AMENDMENT_REPLY,
    COMPLIANT_REPLY,
    CONSTITUTION,
    NON_COMPLIANT_REPLY,
    PROPOSAL_BODY,
    T0,
    revert_message,
    ruling_payload,
    submit,
)
from genvm_host import ConsensusFailure

GROUNDS = (
    "The panel treated the transfer as a grant under Article II section 2, but the body "
    "describes an operating reimbursement, which Article II section 2 does not reach."
)


@pytest.fixture
def rejected(host, registered, accounts):
    """A proposal with a NON_COMPLIANT ruling of record, ready to appeal."""
    host.mock_llm(r"constitutional review panel", NON_COMPLIANT_REPLY)
    verdict = submit(registered, accounts)
    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    return registered, verdict


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_successful_appeal_replaces_the_ruling_of_record(host, rejected, accounts):
    guard, original = rejected
    assert guard.is_compliant(original["proposal_id"]) is False

    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    host.warp(T0 + 3600)
    result = json.loads(guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"]))

    assert result["revision"] == 2
    assert result["previous_ruling"] == "NON_COMPLIANT"
    assert result["ruling"] == "COMPLIANT"
    assert result["ruling_changed"] is True
    assert result["is_compliant"] is True

    assert guard.is_compliant(original["proposal_id"]) is True
    current = json.loads(guard.get_ruling(original["proposal_id"]))
    assert current["ruling"] == "COMPLIANT"
    assert current["revision"] == 2
    assert current["adjudicated_at"] == T0 + 3600


def test_the_superseded_ruling_stays_readable(host, rejected, accounts):
    guard, original = rejected
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"])

    first = json.loads(guard.get_ruling_revision(original["proposal_id"], 1))
    assert first["ruling"] == "NON_COMPLIANT"
    assert first["revision"] == 1
    assert first["precedent_id"] == original["precedent_id"]

    second = json.loads(guard.get_ruling_revision(original["proposal_id"], 2))
    assert second["ruling"] == "COMPLIANT"

    with pytest.raises(Exception) as excinfo:
        guard.get_ruling_revision(original["proposal_id"], 3)
    assert "No revision 3" in revert_message(excinfo)


def test_the_contested_precedent_is_retired_and_points_at_its_successor(host, rejected, accounts):
    guard, original = rejected
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    host.warp(T0 + 7200)
    result = json.loads(guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"]))

    retired = json.loads(guard.get_precedent(original["precedent_id"]))
    assert retired["overruled"] is True
    assert retired["overruled_reason"] == "Superseded on appeal by the proposer"
    assert retired["overruled_at"] == T0 + 7200
    assert retired["superseded_by"] == result["precedent_id"]

    successor = json.loads(guard.get_precedent(result["precedent_id"]))
    assert successor["ruling"] == "COMPLIANT"
    assert successor["proposal_id"] == original["proposal_id"]
    assert guard.get_precedent_count("acme-dao") == 2, "both rulings stay on the record"

    corpus = json.loads(guard.active_precedent_corpus("acme-dao"))
    assert [e["precedent_id"] for e in corpus["precedents"]] == [result["precedent_id"]]


def test_an_appeal_is_not_bound_by_the_ruling_it_contests(host, rejected, accounts):
    """
    The contested precedent is retired before the re-hearing corpus is selected.
    Otherwise the panel would be shown its own rejection as binding case law and
    the appeal would be decided before it was heard.
    """
    guard, original = rejected
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"])

    prompt = host.llm_calls[-1]["prompt"]
    assert original["precedent_id"] not in prompt
    assert "No prior rulings exist for this DAO" in prompt


def test_the_grounds_are_put_to_the_panel_as_argument(host, rejected, accounts):
    guard, original = rejected
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"])

    prompt = host.llm_calls[-1]["prompt"]
    assert "APPEAL GROUNDS SUBMITTED BY THE PROPOSER" in prompt
    assert GROUNDS in prompt
    assert "They are argument," in prompt
    assert "not evidence, and they do not bind you." in prompt

    proposal = json.loads(guard.get_proposal(original["proposal_id"]))
    assert proposal["appealed"] is True
    assert proposal["appeal_grounds"] == GROUNDS
    assert proposal["revision"] == 2


def test_an_appeal_that_confirms_the_ruling_is_recorded_as_such(host, rejected, accounts):
    guard, original = rejected
    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
                       principle="The reimbursement framing does not escape the Article II ceiling."),
    )
    result = json.loads(guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"]))

    assert result["ruling"] == "NON_COMPLIANT"
    assert result["ruling_changed"] is False
    assert guard.is_compliant(original["proposal_id"]) is False
    assert "reimbursement framing" in json.loads(guard.get_ruling(original["proposal_id"]))["principle"]


def test_the_charter_tally_follows_the_ruling_of_record(host, rejected, accounts):
    """An appeal replaces the outcome, so the counters must not double count."""
    guard, original = rejected
    before = json.loads(guard.get_charter("acme-dao"))
    assert (before["non_compliant_total"], before["compliant_total"]) == (1, 0)

    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"])

    after = json.loads(guard.get_charter("acme-dao"))
    assert (after["non_compliant_total"], after["compliant_total"]) == (0, 1)
    assert after["proposal_total"] == 1


def test_an_appeal_can_land_on_amendment_required(host, rejected, accounts):
    guard, original = rejected
    host.mock_llm(r"constitutional review panel", AMENDMENT_REPLY)
    result = json.loads(guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"]))

    assert result["ruling"] == "AMENDMENT_REQUIRED"
    assert result["is_compliant"] is False
    assert result["required_amendments"]
    status = json.loads(guard.compliance_status(original["proposal_id"]))
    assert status["ruling"] == "AMENDMENT_REQUIRED"
    assert status["required_amendments"] == result["required_amendments"]


# ---------------------------------------------------------------------------
# Who may appeal, and how often
# ---------------------------------------------------------------------------

def test_only_the_original_submitter_may_appeal(host, rejected, accounts):
    guard, original = rejected
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    before = len(host.llm_calls)

    for sender in (accounts["outsider"], accounts["steward"], accounts["deployer"]):
        with pytest.raises(Exception) as excinfo:
            guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=sender)
        assert "Only the original submitter" in revert_message(excinfo)

    assert json.loads(guard.get_ruling(original["proposal_id"]))["revision"] == 1
    assert len(host.llm_calls) == before, "authorisation is checked before the panel runs"


def test_an_appeal_happens_once(host, rejected, accounts):
    guard, original = rejected
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"])

    with pytest.raises(Exception) as excinfo:
        guard.appeal_ruling(original["proposal_id"], GROUNDS + " Further argument.", sender=accounts["proposer"])
    assert "already been appealed" in revert_message(excinfo)
    assert json.loads(guard.get_ruling(original["proposal_id"]))["revision"] == 2
    assert guard.get_precedent_count("acme-dao") == 2


@pytest.mark.parametrize("grounds", ["", "   ", "too short"])
def test_appeal_grounds_must_be_substantive(host, rejected, accounts, grounds):
    guard, original = rejected
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    before = len(host.llm_calls)
    with pytest.raises(Exception) as excinfo:
        guard.appeal_ruling(original["proposal_id"], grounds, sender=accounts["proposer"])
    assert "at least 20 characters" in revert_message(excinfo)
    assert len(host.llm_calls) == before, "input validation precedes the panel"


def test_an_unknown_proposal_cannot_be_appealed(host, registered, accounts):
    with pytest.raises(Exception) as excinfo:
        registered.appeal_ruling("acme-dao#p99", GROUNDS, sender=accounts["proposer"])
    assert "Unknown proposal" in revert_message(excinfo)


def test_a_suspended_charter_blocks_appeals(host, rejected, accounts):
    guard, original = rejected
    guard.set_charter_active("acme-dao", False, sender=accounts["steward"])
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    with pytest.raises(Exception) as excinfo:
        guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"])
    assert "Charter is suspended" in revert_message(excinfo)


def test_a_failed_appeal_leaves_the_original_ruling_intact(host, rejected, accounts):
    """
    Validator disagreement during a re-hearing must not half-apply the appeal.
    The transaction reverts, so the contested ruling and its precedent survive.
    """
    guard, original = rejected
    host.mock_llm(r"constitutional review panel", [COMPLIANT_REPLY, NON_COMPLIANT_REPLY])

    with pytest.raises(ConsensusFailure):
        guard.appeal_ruling(original["proposal_id"], GROUNDS, sender=accounts["proposer"])

    current = json.loads(guard.get_ruling(original["proposal_id"]))
    assert current["revision"] == 1
    assert current["ruling"] == "NON_COMPLIANT"
    assert json.loads(guard.get_proposal(original["proposal_id"]))["appealed"] is False
    assert json.loads(guard.get_precedent(original["precedent_id"]))["overruled"] is False
    assert guard.get_precedent_count("acme-dao") == 1
