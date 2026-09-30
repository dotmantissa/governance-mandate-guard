"""
Charter lifecycle: pinning, amendment, drift, stewardship and admission control.

The guarantee the primitive sells is that a ruling was made against a known
constitutional text. These tests cover how that text is pinned, what happens
when the live document stops matching it, and the rules that decide who may put
a proposal in front of the panel at all.
"""

import json

import pytest

from conftest import (
    AMENDED_CONSTITUTION,
    AMENDED_URL,
    COMPLIANT_REPLY,
    CONSTITUTION,
    CONSTITUTION_URL,
    NON_COMPLIANT_REPLY,
    PROPOSAL_BODY,
    PROPOSAL_TITLE,
    T0,
    revert_message,
    submit,
)
from genvm_host import ConsensusFailure


def serve(host, body=CONSTITUTION, url=r"acme\.example/constitution$", status=200):
    host.mock_web(url, {"status": status, "body": body})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registration_pins_the_document_and_seeds_the_charter(host, guard, accounts, serve_constitution):
    result = json.loads(
        guard.register_charter("Acme DAO", "Acme Protocol", CONSTITUTION_URL, 3600, False, True,
                               sender=accounts["steward"])
    )
    assert result["dao_id"] == "acme-dao"
    assert result["version"] == 1
    assert result["ratified_at"] == T0

    charter = json.loads(guard.get_charter("acme-dao"))
    assert charter["steward"] == accounts["steward"]
    assert charter["registered_by"] == accounts["steward"]
    assert charter["doc_fingerprint"] == result["doc_fingerprint"]
    assert charter["doc_chars"] > 0
    assert charter["active"] is True
    assert charter["enforce_pinning"] is True
    assert charter["submission_cooldown_secs"] == 3600
    assert charter["proposal_total"] == charter["precedent_total"] == 0
    assert guard.charter_exists("ACME  dao") is True, "ids are canonicalized"
    assert guard.get_charter_count() == 1
    assert guard.get_charter_id_at(0) == "acme-dao"


@pytest.mark.parametrize(
    "dao_id,name,url,cooldown,expected",
    [
        ("", "Acme", CONSTITUTION_URL, 0, "dao_id must be 1 to"),
        ("   ", "Acme", CONSTITUTION_URL, 0, "dao_id must be 1 to"),
        ("a" * 70, "Acme", CONSTITUTION_URL, 0, "dao_id must be 1 to"),
        ("acme", "   ", CONSTITUTION_URL, 0, "display_name must not be empty"),
        ("acme", "Acme", "", 0, "constitution_url must be 1 to"),
        ("acme", "Acme", "ftp://acme.example/c", 0, "must be an http or https URL"),
        ("acme", "Acme", "https:///nohost", 0, "must include a host"),
        ("acme", "Acme", CONSTITUTION_URL, -1, "submission_cooldown_secs must be between"),
        ("acme", "Acme", CONSTITUTION_URL, 2_592_001, "submission_cooldown_secs must be between"),
    ],
)
def test_registration_input_is_validated_before_any_fetch(
    host, guard, accounts, serve_constitution, dao_id, name, url, cooldown, expected
):
    """
    Every deterministic check runs before the nondeterministic block, so a
    malformed registration costs no fetch and no model call on any validator.
    """
    before = len(host.web_calls)
    with pytest.raises(Exception) as excinfo:
        guard.register_charter(dao_id, name, url, cooldown, False, True, sender=accounts["steward"])
    assert expected in revert_message(excinfo)
    assert len(host.web_calls) == before, "input validation must precede the fetch"
    assert guard.get_charter_count() == 0


def test_a_dao_cannot_be_registered_twice(host, registered, accounts):
    with pytest.raises(Exception) as excinfo:
        registered.register_charter("acme-dao", "Acme again", CONSTITUTION_URL, 0, False, True,
                                    sender=accounts["outsider"])
    assert "already registered" in revert_message(excinfo)
    assert registered.get_charter_count() == 1


def test_an_empty_document_is_an_external_failure(host, guard, accounts):
    serve(host, body="   \n  ")
    with pytest.raises(Exception) as excinfo:
        guard.register_charter("acme", "Acme", CONSTITUTION_URL, 0, False, True,
                               sender=accounts["steward"])
    assert "[EXTERNAL]" in revert_message(excinfo)
    assert guard.get_charter_count() == 0


def test_a_document_that_is_only_markup_is_rejected(host, guard, accounts):
    """Normalization strips tags, so a page with no prose has nothing to pin."""
    serve(host, body="<html><head><style>b{}</style></head><body><!-- x --></body></html>")
    with pytest.raises(Exception) as excinfo:
        guard.register_charter("acme", "Acme", CONSTITUTION_URL, 0, False, True,
                               sender=accounts["steward"])
    assert "empty after normalization" in revert_message(excinfo)


def test_an_unstable_document_cannot_be_registered(host, guard, accounts):
    """
    Two nodes that fetch different text cannot pin a fingerprint. Failing at
    registration is the point: a URL that is not byte stable cannot support the
    claim that a ruling was made against a known text.
    """
    host.mock_web(
        r"acme\.example/constitution$",
        [{"status": 200, "body": CONSTITUTION},
         {"status": 200, "body": CONSTITUTION + "\nRendered at 12:00:01."}],
    )
    with pytest.raises(ConsensusFailure):
        guard.register_charter("acme", "Acme", CONSTITUTION_URL, 0, False, True,
                               sender=accounts["steward"])
    assert guard.get_charter_count() == 0


def test_cosmetic_document_differences_do_not_break_pinning(host, guard, accounts):
    """
    Normalization collapses whitespace, folds case and strips markup, so two
    nodes served the same content with different indentation, line endings or
    surrounding HTML still agree.
    """
    host.mock_web(
        r"acme\.example/constitution$",
        [
            {"status": 200, "body": CONSTITUTION},
            {"status": 200, "body": "<article>\r\n  " + CONSTITUTION.upper().replace("\n", "\r\n  ") + "\r\n</article>"},
        ],
    )
    result = json.loads(
        guard.register_charter("acme", "Acme", CONSTITUTION_URL, 0, False, True,
                               sender=accounts["steward"])
    )
    assert len(result["doc_fingerprint"]) == 64


def test_urls_are_canonicalized(host, guard, accounts):
    """
    A trailing slash, an upper-case host or a fragment must not create a second
    charter or defeat the drift check.
    """
    serve(host, url=r"acme\.example/constitution")
    result = json.loads(
        guard.register_charter("acme", "Acme", "HTTPS://ACME.EXAMPLE/constitution/#article-2", 0,
                               False, True, sender=accounts["steward"])
    )
    assert result["constitution_url"] == CONSTITUTION_URL


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

def test_drift_blocks_adjudication_when_pinning_is_enforced(host, registered, accounts):
    """
    A ruling made against a document that is no longer the ratified text is not a
    constitutional ruling, so the gate refuses and names the remedy. The failure
    is EXPECTED, so every validator produces the same message and consensus is
    reached on the refusal.
    """
    host.clear_routes()
    serve(host, body=AMENDED_CONSTITUTION)
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)

    with pytest.raises(Exception) as excinfo:
        submit(registered, accounts)

    message = revert_message(excinfo)
    assert "[EXPECTED]" in message
    assert "no longer matches the pinned document for acme-dao" in message
    assert "ratify an amendment" in message
    assert host.nondet_runs[-1]["validator_agreed"] is True, "both nodes see the same drift"
    assert registered.get_proposal_count("acme-dao") == 0
    assert len(host.llm_calls) == 0, "drift is detected before the model is consulted"


def test_drift_is_recorded_rather_than_blocking_when_pinning_is_not_enforced(host, guard, accounts):
    """
    A charter may opt into adjudicating against a living document. The ruling then
    carries the drift flag and both fingerprints, so the divergence is on the
    record and a reviewer can see the ruling was not made against the pin.
    """
    serve(host)
    guard.register_charter("acme-dao", "Acme", CONSTITUTION_URL, 0, False, False,
                           sender=accounts["steward"])
    pinned = json.loads(guard.get_charter("acme-dao"))["doc_fingerprint"]

    host.clear_routes()
    serve(host, body=AMENDED_CONSTITUTION)
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    verdict = submit(guard, accounts)

    assert verdict["constitution_drift"] is True
    assert verdict["ruling"] == "COMPLIANT"
    ruling = json.loads(guard.get_ruling(verdict["proposal_id"]))
    assert ruling["pinned_fingerprint"] == pinned
    assert ruling["live_fingerprint"] != pinned
    assert "no longer matches the text this DAO ratified" in host.llm_calls[0]["prompt"]
    assert json.loads(guard.compliance_status(verdict["proposal_id"]))["constitution_drift"] is True


# ---------------------------------------------------------------------------
# Amendment
# ---------------------------------------------------------------------------

def test_ratifying_an_amendment_repins_and_logs(host, registered, accounts):
    original = json.loads(registered.get_charter("acme-dao"))
    host.clear_routes()
    host.mock_web(r"acme\.example/constitution-v2$", {"status": 200, "body": AMENDED_CONSTITUTION})
    host.warp(T0 + 86_400)

    result = json.loads(
        registered.ratify_amendment("acme-dao", AMENDED_URL,
                                    "Raise the single grant ceiling to 250,000 USDC",
                                    sender=accounts["steward"])
    )
    assert result["version"] == 2
    assert result["previous_fingerprint"] == original["doc_fingerprint"]
    assert result["new_fingerprint"] != original["doc_fingerprint"]

    charter = json.loads(registered.get_charter("acme-dao"))
    assert charter["version"] == 2
    assert charter["constitution_url"] == AMENDED_URL
    assert charter["amended_at"] == T0 + 86_400
    assert charter["ratified_at"] == T0, "ratified_at records the original pinning"

    logged = json.loads(registered.get_amendment("acme-dao", 2))
    assert logged["previous_url"] == CONSTITUTION_URL
    assert logged["new_url"] == AMENDED_URL
    assert logged["note"] == "Raise the single grant ceiling to 250,000 USDC"
    assert logged["ratified_by"] == accounts["steward"]


def test_an_amendment_that_changes_nothing_is_refused(host, registered, accounts):
    """
    Re-pinning identical text would rotate the version and silently reopen every
    previously rejected proposal, so it is rejected.
    """
    host.clear_routes()
    serve(host)
    with pytest.raises(Exception) as excinfo:
        registered.ratify_amendment("acme-dao", CONSTITUTION_URL, "No change", sender=accounts["steward"])
    assert "identical to the current pin" in revert_message(excinfo)
    assert json.loads(registered.get_charter("acme-dao"))["version"] == 1


def test_an_amendment_requires_a_note(host, registered, accounts):
    host.clear_routes()
    host.mock_web(r"acme\.example/constitution-v2$", {"status": 200, "body": AMENDED_CONSTITUTION})
    with pytest.raises(Exception) as excinfo:
        registered.ratify_amendment("acme-dao", AMENDED_URL, "  ", sender=accounts["steward"])
    assert "amendment note is required" in revert_message(excinfo)


def test_only_the_steward_may_amend(host, registered, accounts):
    host.clear_routes()
    host.mock_web(r"acme\.example/constitution-v2$", {"status": 200, "body": AMENDED_CONSTITUTION})
    with pytest.raises(Exception) as excinfo:
        registered.ratify_amendment("acme-dao", AMENDED_URL, "note", sender=accounts["outsider"])
    assert "Only the charter steward" in revert_message(excinfo)


def test_amendment_unblocks_a_drifted_charter(host, registered, accounts):
    """The documented remedy for drift actually works."""
    host.clear_routes()
    serve(host, body=AMENDED_CONSTITUTION)
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    with pytest.raises(Exception):
        submit(registered, accounts)

    registered.ratify_amendment("acme-dao", CONSTITUTION_URL, "Adopt the revised ceiling",
                                sender=accounts["steward"])
    verdict = submit(registered, accounts)
    assert verdict["ruling"] == "COMPLIANT"
    assert verdict["constitution_drift"] is False
    assert verdict["charter_version"] == 2


def test_precedents_from_an_earlier_version_become_persuasive(host, registered, accounts):
    """
    A precedent decided against different constitutional text is not binding. The
    corpus labels it, and the panel is told to weigh it accordingly.
    """
    host.mock_llm(r"constitutional review panel", NON_COMPLIANT_REPLY)
    submit(registered, accounts)

    host.clear_routes()
    host.mock_web(r"acme\.example/constitution-v2$", {"status": 200, "body": AMENDED_CONSTITUTION})
    registered.ratify_amendment("acme-dao", AMENDED_URL, "Raise the ceiling", sender=accounts["steward"])

    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    submit(registered, accounts, title="After the amendment", body=PROPOSAL_BODY.replace("40,000", "200,000"))

    prompt = host.llm_calls[-1]["prompt"]
    assert "decided under charter v1, persuasive only" in prompt
    assert "charter version 2" in prompt


# ---------------------------------------------------------------------------
# Replay protection
# ---------------------------------------------------------------------------

def test_identical_text_cannot_be_resubmitted_under_the_same_version(host, registered, accounts):
    host.mock_llm(r"constitutional review panel", NON_COMPLIANT_REPLY)
    first = submit(registered, accounts)

    with pytest.raises(Exception) as excinfo:
        submit(registered, accounts)
    message = revert_message(excinfo)
    assert "already adjudicated under charter version 1" in message
    assert first["proposal_id"] in message
    assert registered.get_proposal_count("acme-dao") == 1


def test_recasing_and_rewrapping_does_not_evade_the_replay_index(host, registered, accounts):
    host.mock_llm(r"constitutional review panel", NON_COMPLIANT_REPLY)
    submit(registered, accounts)

    with pytest.raises(Exception) as excinfo:
        submit(
            registered, accounts,
            title=PROPOSAL_TITLE.upper(),
            body="  " + PROPOSAL_BODY.replace(" ", "\n  ") + "  ",
        )
    assert "already adjudicated" in revert_message(excinfo)


def test_an_amendment_reopens_a_previously_rejected_proposal(host, registered, accounts):
    """
    The replay index is scoped by charter version. Once the provision that
    rejected a proposal has itself changed, the proposal is a new question.
    """
    host.mock_llm(r"constitutional review panel", NON_COMPLIANT_REPLY)
    submit(registered, accounts)

    host.clear_routes()
    host.mock_web(r"acme\.example/constitution-v2$", {"status": 200, "body": AMENDED_CONSTITUTION})
    registered.ratify_amendment("acme-dao", AMENDED_URL, "Raise the ceiling", sender=accounts["steward"])

    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    again = submit(registered, accounts)
    assert again["ruling"] == "COMPLIANT"
    assert again["charter_version"] == 2
    assert registered.get_proposal_count("acme-dao") == 2


def test_find_adjudication_resolves_by_text(host, registered, accounts):
    """A caller can ask whether a draft was already ruled on before spending a submission."""
    missing = json.loads(registered.find_adjudication("acme-dao", PROPOSAL_TITLE, PROPOSAL_BODY))
    assert missing["found"] is False

    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    verdict = submit(registered, accounts)

    found = json.loads(registered.find_adjudication("acme-dao", PROPOSAL_TITLE.upper(),
                                                   PROPOSAL_BODY.replace(" ", "  ")))
    assert found["found"] is True
    assert found["proposal_id"] == verdict["proposal_id"]
    assert found["is_compliant"] is True
    assert found["body_digest"] == verdict["body_digest"]


# ---------------------------------------------------------------------------
# Admission control
# ---------------------------------------------------------------------------

def test_the_cooldown_throttles_a_single_submitter(host, guard, accounts, serve_constitution):
    """
    Every submission spends a model call on every validator, so a charter can rate
    limit per address. The clock is the pinned transaction time, so the gate is
    deterministic across nodes.
    """
    guard.register_charter("acme-dao", "Acme", CONSTITUTION_URL, 3600, False, True,
                           sender=accounts["steward"])
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    submit(guard, accounts)

    assert guard.submission_available_at("acme-dao", accounts["proposer"]) == T0 + 3600
    assert guard.submission_available_at("acme-dao", accounts["outsider"]) == 0

    host.warp(T0 + 3599)
    with pytest.raises(Exception) as excinfo:
        submit(guard, accounts, title="Too soon", body=PROPOSAL_BODY.replace("40,000", "41,000"))
    assert "cooldown of 3600s has not elapsed" in revert_message(excinfo)

    # A different address is unaffected.
    submit(guard, accounts, title="Other", body=PROPOSAL_BODY.replace("40,000", "42,000"),
           sender=accounts["outsider"])

    host.warp(T0 + 3600)
    later = submit(guard, accounts, title="Now allowed", body=PROPOSAL_BODY.replace("40,000", "43,000"))
    assert later["ruling"] == "COMPLIANT"


def test_the_allowlist_restricts_who_may_submit(host, guard, accounts, serve_constitution):
    guard.register_charter("acme-dao", "Acme", CONSTITUTION_URL, 0, True, True,
                           sender=accounts["steward"])
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)

    assert guard.is_proposer_allowed("acme-dao", accounts["proposer"]) is False
    with pytest.raises(Exception) as excinfo:
        submit(guard, accounts)
    assert "not on the proposer allowlist" in revert_message(excinfo)
    assert len(host.llm_calls) == 0, "admission is checked before the model is consulted"

    guard.set_proposer_allowed("acme-dao", accounts["proposer"], True, sender=accounts["steward"])
    assert guard.is_proposer_allowed("acme-dao", accounts["proposer"]) is True
    assert submit(guard, accounts)["ruling"] == "COMPLIANT"

    guard.set_proposer_allowed("acme-dao", accounts["proposer"], False, sender=accounts["steward"])
    with pytest.raises(Exception):
        submit(guard, accounts, title="After revocation", body=PROPOSAL_BODY.replace("40,000", "44,000"))


def test_an_open_charter_allows_everyone(host, registered, accounts):
    assert registered.is_proposer_allowed("acme-dao", accounts["outsider"]) is True


def test_only_the_steward_manages_the_allowlist(host, registered, accounts):
    with pytest.raises(Exception) as excinfo:
        registered.set_proposer_allowed("acme-dao", accounts["outsider"], True, sender=accounts["outsider"])
    assert "Only the charter steward" in revert_message(excinfo)


@pytest.mark.parametrize(
    "title,body,expected",
    [
        ("  ", PROPOSAL_BODY, "title must not be empty"),
        (PROPOSAL_TITLE, "too short", "body must be at least 40 characters"),
        (PROPOSAL_TITLE, "x" * 6001, "body must be at most 6000 characters"),
    ],
)
def test_proposal_input_is_validated_before_the_panel_runs(host, registered, accounts, title, body, expected):
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    with pytest.raises(Exception) as excinfo:
        submit(registered, accounts, title=title, body=body)
    assert expected in revert_message(excinfo)
    assert len(host.llm_calls) == 0


# ---------------------------------------------------------------------------
# Stewardship and suspension
# ---------------------------------------------------------------------------

def test_stewardship_transfers(host, registered, accounts):
    result = json.loads(
        registered.transfer_stewardship("acme-dao", accounts["outsider"], sender=accounts["steward"])
    )
    assert result["previous_steward"] == accounts["steward"]
    assert result["new_steward"] == accounts["outsider"]
    assert json.loads(registered.get_charter("acme-dao"))["steward"] == accounts["outsider"]

    with pytest.raises(Exception) as excinfo:
        registered.set_charter_active("acme-dao", False, sender=accounts["steward"])
    assert "Only the charter steward" in revert_message(excinfo)
    registered.set_charter_active("acme-dao", False, sender=accounts["outsider"])


@pytest.mark.parametrize("bad", ["not-an-address", "0x1234", ""])
def test_stewardship_rejects_malformed_addresses(host, registered, accounts, bad):
    with pytest.raises(Exception) as excinfo:
        registered.transfer_stewardship("acme-dao", bad, sender=accounts["steward"])
    assert "not a valid address" in revert_message(excinfo)


def test_transferring_to_the_current_steward_is_refused(host, registered, accounts):
    with pytest.raises(Exception) as excinfo:
        registered.transfer_stewardship("acme-dao", accounts["steward"], sender=accounts["steward"])
    assert "already the steward" in revert_message(excinfo)


def test_suspension_stops_new_submissions_but_keeps_past_rulings_answerable(host, registered, accounts):
    """
    Suspending the gate must not retroactively invalidate proposals that already
    cleared it, so `is_compliant` keeps answering while submissions are refused.
    """
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    verdict = submit(registered, accounts)

    registered.set_charter_active("acme-dao", False, sender=accounts["steward"])
    with pytest.raises(Exception) as excinfo:
        submit(registered, accounts, title="While suspended", body=PROPOSAL_BODY.replace("40,000", "45,000"))
    assert "Charter is suspended for acme-dao" in revert_message(excinfo)

    assert registered.is_compliant(verdict["proposal_id"]) is True
    assert json.loads(registered.get_ruling(verdict["proposal_id"]))["ruling"] == "COMPLIANT"

    registered.set_charter_active("acme-dao", True, sender=accounts["steward"])
    assert submit(registered, accounts, title="Resumed",
                  body=PROPOSAL_BODY.replace("40,000", "46,000"))["ruling"] == "COMPLIANT"


def test_unknown_dao_lookups_are_rejected(host, guard, accounts):
    for method, args in [
        ("get_charter", ("nope",)),
        ("get_proposal_count", ("nope",)),
        ("active_precedent_corpus", ("nope",)),
        ("set_charter_active", ("nope", True)),
    ]:
        with pytest.raises(Exception) as excinfo:
            getattr(guard, method)(*args, sender=accounts["steward"])
        assert "Unknown DAO" in revert_message(excinfo)
    assert guard.charter_exists("nope") is False


def test_registry_info_publishes_the_taxonomy_and_bounds(host, registered, accounts):
    """A front end must not have to hard-code the contract's constants."""
    info = json.loads(registered.get_registry_info())
    assert info["registry_name"] == "Acme Governance Registry"
    assert info["deployer"] == accounts["deployer"]
    assert info["charter_total"] == 1
    assert info["rulings"] == ["COMPLIANT", "NON_COMPLIANT", "AMENDMENT_REQUIRED"]
    assert info["mandate_classes"][0] == "precedent_conflict"
    assert info["mandate_classes"][-1] == "none"
    assert len(info["mandate_classes"]) == 8
    assert info["max_corpus"] == 12
    assert info["max_revisions"] == 2
