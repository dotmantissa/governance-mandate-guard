"""
The integration surface: what a DAO's voting contract actually calls.

These tests run the reference consumer, `MandateGatedDAO`, against a deployed
guard. The cross-contract call is real: `gl.get_contract_at(...).view()` goes
through the host's CallContract channel into the guard's own storage, so the gate
is exercised the way it runs on chain rather than against a stub.
"""

import json

import pytest

from conftest import (
    AMENDMENT_REPLY,
    COMPLIANT_REPLY,
    CONSTITUTION,
    DAO_SOURCE,
    NON_COMPLIANT_REPLY,
    PROPOSAL_BODY,
    T0,
    revert_message,
    submit,
)

VOTING_PERIOD = 3600
QUORUM = 2


@pytest.fixture
def dao(host, registered, accounts):
    """A ballot contract gated on the registered guard."""
    contract = host.deploy(
        DAO_SOURCE,
        "MandateGatedDAO",
        args=[registered.address, "acme-dao", VOTING_PERIOD, QUORUM],
        sender=accounts["deployer"],
    )
    contract.set_voter(accounts["voter_a"], True, sender=accounts["deployer"])
    contract.set_voter(accounts["voter_b"], True, sender=accounts["deployer"])
    return contract


def adjudicated(host, guard, accounts, reply, title="Fund the documentation working group",
                body=PROPOSAL_BODY):
    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", reply)
    return submit(guard, accounts, title=title, body=body)


# ---------------------------------------------------------------------------
# is_compliant, the one call a voting contract needs
# ---------------------------------------------------------------------------

def test_is_compliant_answers_for_each_ruling(host, registered, accounts):
    compliant = adjudicated(host, registered, accounts, COMPLIANT_REPLY)
    rejected = adjudicated(host, registered, accounts, NON_COMPLIANT_REPLY,
                           title="Oversized grant", body=PROPOSAL_BODY.replace("40,000", "900,000"))
    needs_work = adjudicated(host, registered, accounts, AMENDMENT_REPLY,
                             title="Unattributed transfer", body=PROPOSAL_BODY.replace("40,000", "39,000"))

    assert registered.is_compliant(compliant["proposal_id"]) is True
    assert registered.is_compliant(rejected["proposal_id"]) is False
    assert registered.is_compliant(needs_work["proposal_id"]) is False, (
        "a proposal that still needs amending has not cleared the gate"
    )


def test_is_compliant_is_false_for_an_unknown_proposal(host, registered, accounts):
    """
    A voting contract must be able to call the gate on any identifier without
    handling a revert, so an unknown proposal is simply not compliant.
    """
    assert registered.is_compliant("acme-dao#p404") is False
    assert registered.is_compliant("") is False
    assert json.loads(registered.compliance_status("acme-dao#p404")) == {
        "adjudicated": False,
        "is_compliant": False,
        "proposal_id": "acme-dao#p404",
    }


def test_require_compliant_reverts_with_the_reason(host, registered, accounts):
    compliant = adjudicated(host, registered, accounts, COMPLIANT_REPLY)
    assert registered.require_compliant(compliant["proposal_id"]) is True

    rejected = adjudicated(host, registered, accounts, NON_COMPLIANT_REPLY,
                           title="Oversized", body=PROPOSAL_BODY.replace("40,000", "900,000"))
    with pytest.raises(Exception) as excinfo:
        registered.require_compliant(rejected["proposal_id"])
    message = revert_message(excinfo)
    assert "non-compliant with the charter" in message
    assert "treasury_mandate" in message

    needs_work = adjudicated(host, registered, accounts, AMENDMENT_REPLY,
                             title="Unattributed", body=PROPOSAL_BODY.replace("40,000", "39,000"))
    with pytest.raises(Exception) as excinfo:
        registered.require_compliant(needs_work["proposal_id"])
    message = revert_message(excinfo)
    assert "requires amendment before a vote" in message
    assert "Council resolution number" in message

    with pytest.raises(Exception) as excinfo:
        registered.require_compliant("acme-dao#p404")
    assert "has not been adjudicated" in revert_message(excinfo)


def test_compliance_status_carries_everything_a_front_end_needs(host, registered, accounts):
    verdict = adjudicated(host, registered, accounts, NON_COMPLIANT_REPLY)
    status = json.loads(registered.compliance_status(verdict["proposal_id"]))

    assert status["adjudicated"] is True
    assert status["dao_id"] == "acme-dao"
    assert status["ruling"] == "NON_COMPLIANT"
    assert status["is_compliant"] is False
    assert status["mandate_class"] == "treasury_mandate"
    assert status["principle"] == verdict["principle"]
    assert status["precedent_id"] == verdict["precedent_id"]
    assert status["submitter"] == accounts["proposer"]
    assert status["revision"] == 1
    assert status["appealed"] is False
    assert status["adjudicated_at"] == T0


# ---------------------------------------------------------------------------
# The gate enforced from inside another contract
# ---------------------------------------------------------------------------

def test_the_gate_is_read_synchronously_when_a_ballot_opens(host, registered, dao, accounts):
    verdict = adjudicated(host, registered, accounts, COMPLIANT_REPLY)

    opened = json.loads(dao.open_ballot(verdict["proposal_id"], sender=accounts["voter_a"]))
    assert opened["proposal_id"] == verdict["proposal_id"]
    assert opened["opened_at"] == T0
    assert opened["closes_at"] == T0 + VOTING_PERIOD
    assert opened["guard_ruling"] == "COMPLIANT"
    assert opened["guard_precedent_id"] == verdict["precedent_id"]

    ballot = json.loads(dao.get_ballot(verdict["proposal_id"]))
    assert ballot["guard_principle"] == verdict["principle"], (
        "the DAO keeps its own record of why this vote was permitted"
    )


def test_a_non_compliant_proposal_never_gets_a_ballot(host, registered, dao, accounts):
    """
    The check happens inside the transaction that would create the ballot, so
    there is no window in which a rejected proposal has an open vote and no
    rejected ballot record is left behind.
    """
    verdict = adjudicated(host, registered, accounts, NON_COMPLIANT_REPLY,
                          title="Oversized", body=PROPOSAL_BODY.replace("40,000", "900,000"))

    with pytest.raises(Exception) as excinfo:
        dao.open_ballot(verdict["proposal_id"], sender=accounts["voter_a"])
    assert "has not cleared constitutional review" in revert_message(excinfo)

    assert dao.ballot_exists(verdict["proposal_id"]) is False
    assert dao.get_ballot_count() == 0


def test_an_amendment_required_proposal_is_also_blocked(host, registered, dao, accounts):
    verdict = adjudicated(host, registered, accounts, AMENDMENT_REPLY)
    with pytest.raises(Exception) as excinfo:
        dao.open_ballot(verdict["proposal_id"], sender=accounts["voter_a"])
    assert "has not cleared constitutional review" in revert_message(excinfo)
    assert dao.ballot_exists(verdict["proposal_id"]) is False


def test_a_proposal_that_was_never_submitted_is_blocked(host, registered, dao, accounts):
    with pytest.raises(Exception) as excinfo:
        dao.open_ballot("acme-dao#p404", sender=accounts["voter_a"])
    assert "has not cleared constitutional review" in revert_message(excinfo)


def test_the_gate_reads_the_live_ruling_not_a_stale_copy(host, registered, dao, accounts):
    """
    A ruling replaced by an appeal governs from that moment. Because the consumer
    reads the guard at open time rather than trusting a value it stored earlier,
    a successful appeal immediately unblocks the ballot.
    """
    verdict = adjudicated(host, registered, accounts, NON_COMPLIANT_REPLY,
                          title="Contested", body=PROPOSAL_BODY.replace("40,000", "48,000"))
    with pytest.raises(Exception):
        dao.open_ballot(verdict["proposal_id"], sender=accounts["voter_a"])

    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    registered.appeal_ruling(
        verdict["proposal_id"],
        "The transfer is an operating reimbursement, which Article II section 2 does not reach.",
        sender=accounts["proposer"],
    )

    opened = json.loads(dao.open_ballot(verdict["proposal_id"], sender=accounts["voter_a"]))
    assert opened["guard_ruling"] == "COMPLIANT"


def test_a_ballot_opened_before_an_appeal_keeps_its_own_record(host, registered, dao, accounts):
    """
    The reverse direction. A vote that legitimately opened is not retroactively
    invalidated by a later appeal, and the DAO's local copy shows the ruling that
    actually permitted it.
    """
    verdict = adjudicated(host, registered, accounts, COMPLIANT_REPLY)
    dao.open_ballot(verdict["proposal_id"], sender=accounts["voter_a"])

    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", NON_COMPLIANT_REPLY)
    registered.appeal_ruling(
        verdict["proposal_id"],
        "The panel should have found this transfer outside the treasury article entirely.",
        sender=accounts["proposer"],
    )

    assert registered.is_compliant(verdict["proposal_id"]) is False
    assert json.loads(dao.get_ballot(verdict["proposal_id"]))["guard_ruling"] == "COMPLIANT"


def test_a_proposal_from_another_dao_is_refused(host, guard, accounts, serve_constitution):
    """
    The consumer is bound to one DAO identifier. A compliant proposal belonging to
    a different registry must not open a ballot here, or one DAO's gate would
    launder proposals for another.
    """
    host.mock_web(r"other\.example/charter$", {"status": 200, "body": "Article I. The Other DAO funds translation work."})
    guard.register_charter("Acme DAO", "Acme", "https://acme.example/constitution", 0, False, True,
                           sender=accounts["steward"])
    guard.register_charter("Other DAO", "Other", "https://other.example/charter", 0, False, True,
                           sender=accounts["outsider"])

    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    foreign = json.loads(
        guard.submit_proposal("other-dao", "Translate the docs",
                              "Fund a translation effort with 5,000 USDC from the Other DAO treasury.",
                              sender=accounts["proposer"])
    )
    assert guard.is_compliant(foreign["proposal_id"]) is True

    dao = host.deploy(DAO_SOURCE, "MandateGatedDAO",
                      args=[guard.address, "acme-dao", VOTING_PERIOD, QUORUM],
                      sender=accounts["deployer"])
    with pytest.raises(Exception) as excinfo:
        dao.open_ballot(foreign["proposal_id"], sender=accounts["voter_a"])
    assert "belongs to a different DAO" in revert_message(excinfo)
    assert dao.ballot_exists(foreign["proposal_id"]) is False


def test_a_missing_guard_blocks_rather_than_opens(host, accounts):
    """
    A consumer pointed at an address with no guard must fail closed. Treating an
    unreachable gate as a pass would defeat the whole mechanism.
    """
    dao = host.deploy(DAO_SOURCE, "MandateGatedDAO",
                      args=[host.new_address(), "acme-dao", VOTING_PERIOD, QUORUM],
                      sender=accounts["deployer"])
    with pytest.raises(Exception):
        dao.open_ballot("acme-dao#p0", sender=accounts["voter_a"])
    assert dao.get_ballot_count() == 0


# ---------------------------------------------------------------------------
# The consumer's own ballot lifecycle
# ---------------------------------------------------------------------------

def test_the_full_ballot_lifecycle(host, registered, dao, accounts):
    verdict = adjudicated(host, registered, accounts, COMPLIANT_REPLY)
    pid = verdict["proposal_id"]
    dao.open_ballot(pid, sender=accounts["voter_a"])

    dao.cast_vote(pid, True, sender=accounts["voter_a"])
    result = json.loads(dao.cast_vote(pid, False, sender=accounts["voter_b"]))
    assert (result["yes_votes"], result["no_votes"]) == (1, 1)
    assert dao.has_voted(pid, accounts["voter_a"]) is True
    assert dao.has_voted(pid, accounts["outsider"]) is False

    with pytest.raises(Exception) as excinfo:
        dao.close_ballot(pid, sender=accounts["voter_a"])
    assert "Voting period has not ended" in revert_message(excinfo)

    host.warp(T0 + VOTING_PERIOD)
    closed = json.loads(dao.close_ballot(pid, sender=accounts["outsider"]))
    assert closed["turnout"] == 2
    assert closed["passed"] is False, "a tie does not carry"

    with pytest.raises(Exception) as excinfo:
        dao.close_ballot(pid, sender=accounts["voter_a"])
    assert "already closed" in revert_message(excinfo)


def test_a_ballot_passes_on_a_majority_that_meets_quorum(host, registered, dao, accounts):
    verdict = adjudicated(host, registered, accounts, COMPLIANT_REPLY)
    pid = verdict["proposal_id"]
    dao.open_ballot(pid, sender=accounts["voter_a"])
    dao.cast_vote(pid, True, sender=accounts["voter_a"])
    dao.cast_vote(pid, True, sender=accounts["voter_b"])

    host.warp(T0 + VOTING_PERIOD + 1)
    closed = json.loads(dao.close_ballot(pid, sender=accounts["voter_a"]))
    assert closed["passed"] is True
    assert json.loads(dao.get_ballot(pid))["passed"] is True


def test_quorum_is_enforced(host, registered, accounts):
    dao = host.deploy(DAO_SOURCE, "MandateGatedDAO",
                      args=[registered.address, "acme-dao", VOTING_PERIOD, 3],
                      sender=accounts["deployer"])
    dao.set_voter(accounts["voter_a"], True, sender=accounts["deployer"])
    dao.set_voter(accounts["voter_b"], True, sender=accounts["deployer"])

    verdict = adjudicated(host, registered, accounts, COMPLIANT_REPLY)
    pid = verdict["proposal_id"]
    dao.open_ballot(pid, sender=accounts["voter_a"])
    dao.cast_vote(pid, True, sender=accounts["voter_a"])
    dao.cast_vote(pid, True, sender=accounts["voter_b"])

    host.warp(T0 + VOTING_PERIOD + 1)
    assert json.loads(dao.close_ballot(pid, sender=accounts["voter_a"]))["passed"] is False


def test_voting_rules(host, registered, dao, accounts):
    verdict = adjudicated(host, registered, accounts, COMPLIANT_REPLY)
    pid = verdict["proposal_id"]
    dao.open_ballot(pid, sender=accounts["voter_a"])

    with pytest.raises(Exception) as excinfo:
        dao.cast_vote(pid, True, sender=accounts["outsider"])
    assert "not on the voter roll" in revert_message(excinfo)

    dao.cast_vote(pid, True, sender=accounts["voter_a"])
    with pytest.raises(Exception) as excinfo:
        dao.cast_vote(pid, False, sender=accounts["voter_a"])
    assert "already voted" in revert_message(excinfo)

    host.warp(T0 + VOTING_PERIOD)
    with pytest.raises(Exception) as excinfo:
        dao.cast_vote(pid, True, sender=accounts["voter_b"])
    assert "Voting period has ended" in revert_message(excinfo)

    with pytest.raises(Exception) as excinfo:
        dao.cast_vote("acme-dao#p404", True, sender=accounts["voter_b"])
    assert "No ballot for" in revert_message(excinfo)


def test_a_ballot_opens_once(host, registered, dao, accounts):
    verdict = adjudicated(host, registered, accounts, COMPLIANT_REPLY)
    pid = verdict["proposal_id"]
    dao.open_ballot(pid, sender=accounts["voter_a"])
    with pytest.raises(Exception) as excinfo:
        dao.open_ballot(pid, sender=accounts["voter_b"])
    assert "already open or closed" in revert_message(excinfo)
    assert dao.get_ballot_count() == 1


def test_only_the_admin_manages_the_voter_roll(host, registered, dao, accounts):
    with pytest.raises(Exception) as excinfo:
        dao.set_voter(accounts["outsider"], True, sender=accounts["outsider"])
    assert "Only the admin" in revert_message(excinfo)

    assert dao.is_voter(accounts["voter_a"]) is True
    dao.set_voter(accounts["voter_a"], False, sender=accounts["deployer"])
    assert dao.is_voter(accounts["voter_a"]) is False
    assert json.loads(dao.get_config())["voter_total"] == 1


@pytest.mark.parametrize(
    "period,quorum,expected",
    [
        (59, 2, "voting_period_secs must be between"),
        (7_776_001, 2, "voting_period_secs must be between"),
        (3600, 0, "quorum must be at least 1"),
    ],
)
def test_the_consumer_validates_its_own_configuration(host, registered, accounts, period, quorum, expected):
    with pytest.raises(Exception) as excinfo:
        host.deploy(DAO_SOURCE, "MandateGatedDAO",
                    args=[registered.address, "acme-dao", period, quorum],
                    sender=accounts["deployer"])
    assert expected in revert_message(excinfo)


def test_the_consumer_rejects_a_malformed_guard_address(host, accounts):
    with pytest.raises(Exception) as excinfo:
        host.deploy(DAO_SOURCE, "MandateGatedDAO",
                    args=["not-an-address", "acme-dao", 3600, 2],
                    sender=accounts["deployer"])
    assert "guard_address is not a valid address" in revert_message(excinfo)


def test_the_consumer_publishes_the_guard_it_is_bound_to(host, registered, dao, accounts):
    config = json.loads(dao.get_config())
    assert config["guard_address"] == registered.address
    assert config["dao_id"] == "acme-dao"
    assert config["admin"] == accounts["deployer"]
    assert config["voting_period_secs"] == VOTING_PERIOD
    assert config["quorum"] == QUORUM


def test_ballots_are_enumerable(host, registered, dao, accounts):
    first = adjudicated(host, registered, accounts, COMPLIANT_REPLY)
    second = adjudicated(host, registered, accounts, COMPLIANT_REPLY,
                         title="Second", body=PROPOSAL_BODY.replace("40,000", "41,000"))
    dao.open_ballot(first["proposal_id"], sender=accounts["voter_a"])
    dao.open_ballot(second["proposal_id"], sender=accounts["voter_a"])

    assert dao.get_ballot_count() == 2
    assert dao.get_ballot_id_at(0) == first["proposal_id"]
    assert dao.get_ballot_id_at(1) == second["proposal_id"]
    with pytest.raises(Exception) as excinfo:
        dao.get_ballot_id_at(2)
    assert "No ballot at index 2" in revert_message(excinfo)
