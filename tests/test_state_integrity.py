"""
State integrity: atomicity, isolation, the clock and the digests.

A gate that half-applies a failed adjudication, leaks one DAO's state into
another, or derives a different digest on two nodes would be unsafe regardless of
how well it reasons. These tests cover the mechanical guarantees underneath the
consensus behaviour.
"""

import json

import pytest

from conftest import (
    COMPLIANT_REPLY,
    CONSTITUTION,
    CONSTITUTION_URL,
    GUARD_SOURCE,
    NON_COMPLIANT_REPLY,
    PROPOSAL_BODY,
    PROPOSAL_TITLE,
    T0,
    revert_message,
    ruling_payload,
    submit,
)
from genvm_host import ConsensusFailure


def snapshot(guard, dao_id="acme-dao") -> dict:
    """Everything observable about a DAO's registry, for before and after checks."""
    charter = json.loads(guard.get_charter(dao_id))
    return {
        "charter": charter,
        "corpus": json.loads(guard.active_precedent_corpus(dao_id)),
        "precedents": json.loads(guard.list_precedents(dao_id, 0, 64)),
        "registry": json.loads(guard.get_registry_info()),
    }


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------

def test_a_failed_adjudication_changes_nothing_at_all(host, registered, accounts):
    """
    A transaction that fails applies no state change. Not a counter, not a
    replay key, not the cooldown stamp. A cooldown that survived a failure would
    let one broken model reply lock a proposer out.
    """
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    submit(registered, accounts)
    before = snapshot(registered)

    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", [COMPLIANT_REPLY, NON_COMPLIANT_REPLY])

    with pytest.raises(ConsensusFailure):
        submit(registered, accounts, title="Doomed", body=PROPOSAL_BODY.replace("40,000", "77,000"))

    assert snapshot(registered) == before
    assert registered.submission_available_at("acme-dao", accounts["proposer"]) == 0

    # The identical text is still submittable, because the replay key was never burned.
    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    retried = submit(registered, accounts, title="Doomed", body=PROPOSAL_BODY.replace("40,000", "77,000"))
    assert retried["ruling"] == "COMPLIANT"


def test_a_failed_registration_leaves_no_charter(host, guard, accounts):
    host.mock_web(
        r"acme\.example/constitution$",
        [{"status": 200, "body": CONSTITUTION}, {"status": 200, "body": CONSTITUTION + " drifted"}],
    )
    with pytest.raises(ConsensusFailure):
        guard.register_charter("acme-dao", "Acme", CONSTITUTION_URL, 0, False, True,
                               sender=accounts["steward"])

    assert guard.get_charter_count() == 0
    assert guard.charter_exists("acme-dao") is False
    assert json.loads(guard.get_registry_info())["charter_total"] == 0


def test_a_failed_amendment_leaves_the_old_pin_in_place(host, registered, accounts):
    before = json.loads(registered.get_charter("acme-dao"))
    host.clear_routes()
    host.mock_web(
        r"acme\.example/constitution-v2$",
        [{"status": 200, "body": "Article I. Version two."},
         {"status": 200, "body": "Article I. Version two, but different."}],
    )
    with pytest.raises(ConsensusFailure):
        registered.ratify_amendment("acme-dao", "https://acme.example/constitution-v2", "note",
                                    sender=accounts["steward"])

    after = json.loads(registered.get_charter("acme-dao"))
    assert after == before
    with pytest.raises(Exception):
        registered.get_amendment("acme-dao", 2)


def test_counters_stay_consistent_across_a_long_session(host, registered, accounts):
    """The per-ruling tallies must always sum to the proposal total."""
    replies = [COMPLIANT_REPLY, NON_COMPLIANT_REPLY, COMPLIANT_REPLY,
               ruling_payload(ruling="AMENDMENT_REQUIRED", mandate_class="procedural_mandate",
                              amendments="Name the authorising resolution.")]
    for index, reply in enumerate(replies):
        host.clear_routes()
        host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
        host.mock_llm(r"constitutional review panel", reply)
        submit(registered, accounts, title=f"P{index}",
               body=PROPOSAL_BODY.replace("40,000", f"{20_000 + index}"))

    charter = json.loads(registered.get_charter("acme-dao"))
    assert charter["proposal_total"] == 4
    assert charter["precedent_total"] == 4
    assert charter["compliant_total"] == 2
    assert charter["non_compliant_total"] == 1
    assert charter["amendment_required_total"] == 1
    assert (charter["compliant_total"] + charter["non_compliant_total"]
            + charter["amendment_required_total"]) == charter["proposal_total"]

    registry = json.loads(registered.get_registry_info())
    assert registry["proposal_total"] == 4
    assert registry["precedent_total"] == 4


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

def test_two_deployments_do_not_share_storage(host, accounts, serve_constitution):
    """Each deployment is an independent registry."""
    first = host.deploy(GUARD_SOURCE, "GovernanceMandateGuard", args=["First"],
                        sender=accounts["deployer"])
    second = host.deploy(GUARD_SOURCE, "GovernanceMandateGuard", args=["Second"],
                         sender=accounts["outsider"])

    first.register_charter("acme-dao", "Acme", CONSTITUTION_URL, 0, False, True,
                           sender=accounts["steward"])

    assert first.get_charter_count() == 1
    assert second.get_charter_count() == 0
    assert second.charter_exists("acme-dao") is False
    assert json.loads(first.get_registry_info())["registry_name"] == "First"
    assert json.loads(second.get_registry_info())["registry_name"] == "Second"
    assert json.loads(second.get_registry_info())["deployer"] == accounts["outsider"]


def test_one_daos_suspension_does_not_affect_another(host, guard, accounts, serve_constitution):
    host.mock_web(r"other\.example/charter$", {"status": 200, "body": "Article I. Other DAO purpose."})
    guard.register_charter("Acme DAO", "Acme", CONSTITUTION_URL, 0, False, True,
                           sender=accounts["steward"])
    guard.register_charter("Other DAO", "Other", "https://other.example/charter", 0, False, True,
                           sender=accounts["outsider"])

    guard.set_charter_active("acme-dao", False, sender=accounts["steward"])
    assert json.loads(guard.get_charter("other-dao"))["active"] is True

    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    assert json.loads(
        guard.submit_proposal("other-dao", "Still open",
                              "Fund a 5,000 USDC translation effort from the Other DAO treasury.",
                              sender=accounts["proposer"])
    )["ruling"] == "COMPLIANT"


def test_a_steward_cannot_act_on_another_dao(host, guard, accounts, serve_constitution):
    host.mock_web(r"other\.example/charter$", {"status": 200, "body": "Article I. Other DAO purpose."})
    guard.register_charter("Acme DAO", "Acme", CONSTITUTION_URL, 0, False, True,
                           sender=accounts["steward"])
    guard.register_charter("Other DAO", "Other", "https://other.example/charter", 0, False, True,
                           sender=accounts["outsider"])

    with pytest.raises(Exception) as excinfo:
        guard.set_charter_active("other-dao", False, sender=accounts["steward"])
    assert "Only the charter steward" in revert_message(excinfo)
    assert json.loads(guard.get_charter("other-dao"))["active"] is True


# ---------------------------------------------------------------------------
# The transaction clock
# ---------------------------------------------------------------------------

def test_timestamps_come_from_the_pinned_transaction_clock(host, registered, accounts):
    """
    GenVM pins `datetime.now()` to the transaction datetime so leaders and
    validators read the same value. Every stored timestamp must come from it.
    """
    host.warp(T0 + 12_345)
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    verdict = submit(registered, accounts)

    assert verdict["adjudicated_at"] == T0 + 12_345
    assert json.loads(registered.get_proposal(verdict["proposal_id"]))["submitted_at"] == T0 + 12_345
    assert json.loads(registered.get_precedent(verdict["precedent_id"]))["created_at"] == T0 + 12_345


def test_the_message_datetime_is_the_documented_fallback(host, registered, accounts):
    """
    When the pinned clock cannot be read the contract falls back to the same
    transaction datetime carried on the message, which produces an identical
    value on every node rather than a per-node one.
    """
    host.warp(T0 + 555)
    host.clock_mode = "broken_now"
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    try:
        verdict = submit(registered, accounts)
    finally:
        host.clock_mode = "pinned"

    assert verdict["adjudicated_at"] == T0 + 555
    assert json.loads(registered.get_proposal(verdict["proposal_id"]))["submitted_at"] == T0 + 555


def test_an_unreadable_clock_fails_closed(host, guard, accounts, serve_constitution):
    """
    With neither the pinned clock nor the message datetime readable, `_now`
    returns 0. A zero clock leaves every cooldown unexpired, so the gate admits
    nothing rather than admitting everything.
    """
    guard.register_charter("acme-dao", "Acme", CONSTITUTION_URL, 600, False, True,
                           sender=accounts["steward"])
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    submit(guard, accounts)

    host.clock_mode = "dead"
    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    try:
        with pytest.raises(Exception) as excinfo:
            submit(guard, accounts, title="Blocked", body=PROPOSAL_BODY.replace("40,000", "31,000"))
    finally:
        host.clock_mode = "pinned"

    assert "cooldown of 600s has not elapsed" in revert_message(excinfo)


# ---------------------------------------------------------------------------
# Digests and normalization
# ---------------------------------------------------------------------------

def test_the_body_digest_is_stable_and_text_dependent(host, registered, accounts):
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    verdict = submit(registered, accounts)

    same = json.loads(registered.find_adjudication("acme-dao", PROPOSAL_TITLE, PROPOSAL_BODY))
    assert same["body_digest"] == verdict["body_digest"]
    assert len(verdict["body_digest"]) == 64

    different = json.loads(
        registered.find_adjudication("acme-dao", PROPOSAL_TITLE, PROPOSAL_BODY + " One more clause.")
    )
    assert different["body_digest"] != verdict["body_digest"]
    assert different["found"] is False


def test_the_title_participates_in_the_digest(host, registered, accounts):
    """
    Two proposals with identical bodies but different titles are different
    proposals, so the same body under a new title is admissible.
    """
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    submit(registered, accounts)

    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    second = submit(registered, accounts, title="A materially different heading")
    assert second["proposal_id"] == "acme-dao#p1"


def test_the_document_fingerprint_is_content_dependent(host, guard, accounts):
    """The pin must change when the text changes and not when the markup does."""
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    host.mock_web(r"acme\.example/wrapped$",
                  {"status": 200, "body": f"<html><body><pre>{CONSTITUTION}</pre></body></html>"})
    host.mock_web(r"acme\.example/edited$",
                  {"status": 200, "body": CONSTITUTION.replace("ten percent", "twenty percent")})

    plain = json.loads(guard.register_charter("a", "A", CONSTITUTION_URL, 0, False, True,
                                              sender=accounts["steward"]))
    wrapped = json.loads(guard.register_charter("b", "B", "https://acme.example/wrapped", 0, False, True,
                                                sender=accounts["steward"]))
    edited = json.loads(guard.register_charter("c", "C", "https://acme.example/edited", 0, False, True,
                                               sender=accounts["steward"]))

    assert wrapped["doc_fingerprint"] == plain["doc_fingerprint"], "markup is not content"
    assert edited["doc_fingerprint"] != plain["doc_fingerprint"], "a changed limit is content"


def test_addresses_are_stored_and_returned_canonically(host, registered, accounts):
    """
    Checksummed input is accepted and every address the contract returns is the
    lower-case form, so a caller comparing strings never sees two spellings of
    one address.
    """
    mixed = "0x" + accounts["outsider"][2:].upper().replace("X", "x")
    registered.set_proposer_allowed("acme-dao", mixed, True, sender=accounts["steward"])
    assert registered.is_proposer_allowed("acme-dao", accounts["outsider"]) is True

    result = json.loads(
        registered.transfer_stewardship("acme-dao", mixed, sender=accounts["steward"])
    )
    assert result["new_steward"] == accounts["outsider"]
    assert json.loads(registered.get_charter("acme-dao"))["steward"] == accounts["outsider"]


def test_dao_identifiers_are_canonicalized_consistently(host, guard, accounts, serve_constitution):
    guard.register_charter("  Acme   DAO  ", "Acme", CONSTITUTION_URL, 0, False, True,
                           sender=accounts["steward"])
    for spelling in ("acme-dao", "Acme DAO", "ACME   dao", "  acme-dao  "):
        assert guard.charter_exists(spelling) is True
        assert json.loads(guard.get_charter(spelling))["dao_id"] == "acme-dao"


# ---------------------------------------------------------------------------
# Field bounds reaching storage
# ---------------------------------------------------------------------------

def test_oversized_model_output_is_truncated_before_storage(host, registered, accounts):
    """
    Model output is bounded before it reaches a storage slot, so a long reply
    cannot inflate the registry, and the truncation is identical on every node
    because it happens in deterministic code after consensus.
    """
    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(
            ruling="NON_COMPLIANT",
            mandate_class="treasury_mandate",
            principle="P" * 2000,
            rationale="R" * 5000,
            clause="C" * 2000,
            citations=[f"acme-dao#r{n}" for n in range(40)],
        ),
    )
    verdict = submit(registered, accounts)

    assert len(verdict["principle"]) == 240
    assert len(verdict["rationale"]) == 900
    assert len(verdict["constitution_clause"]) == 300
    assert len(verdict["cited_precedents"].split(",")) == 8

    stored = json.loads(registered.get_ruling(verdict["proposal_id"]))
    assert stored["principle"] == verdict["principle"]
    assert stored["rationale"] == verdict["rationale"]


def test_a_compliant_ruling_stores_no_required_amendments(host, registered, accounts):
    """
    Only an AMENDMENT_REQUIRED ruling carries a remedy. A model that volunteers
    one alongside a pass must not have it recorded as a condition of the pass.
    """
    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(ruling="COMPLIANT", amendments="You could also tighten the wording."),
    )
    verdict = submit(registered, accounts)
    assert verdict["required_amendments"] == ""
    assert json.loads(registered.get_ruling(verdict["proposal_id"]))["required_amendments"] == ""


def test_the_whole_proposal_body_is_preserved_for_review(host, registered, accounts):
    """
    The submitted text is kept verbatim, because an appeal re-adjudicates it and
    a reviewer must be able to read exactly what the panel read.
    """
    body = PROPOSAL_BODY + " " + "Additional context. " * 100
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    verdict = submit(registered, accounts, body=body)

    stored = json.loads(registered.get_proposal(verdict["proposal_id"]))
    assert stored["body"] == body.strip()
    assert stored["title"] == PROPOSAL_TITLE
    assert body.strip() in host.llm_calls[0]["prompt"]
