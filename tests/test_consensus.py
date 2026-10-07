"""
Consensus behaviour: what validators actually check, and what they reject.

These are the tests that matter most for a GenLayer primitive, because the
equivalence principle is the part a reviewer cannot verify by reading storage.
Each test runs the real `gl.vm.run_nondet_unsafe`, so the validator closure is
executed against leader output it did not produce.

The web and model channels can be given a list of answers, consumed in order.
The leader's call takes the first and the validator's call takes the second, so
disagreement is produced the same way it happens on a real network: two honest
nodes reading the same sources and reaching different conclusions.
"""

import json

import pytest

from conftest import (
    AMENDMENT_REPLY,
    COMPLIANT_REPLY,
    CONSTITUTION,
    CONSTITUTION_URL,
    GUARD_SOURCE,
    NON_COMPLIANT_REPLY,
    PROPOSAL_BODY,
    PROPOSAL_TITLE,
    body_variant,
    holding_of,
    principle_digest_of,
    ruling_payload,
    submit,
)
from genvm_host import ConsensusFailure


# ---------------------------------------------------------------------------
# Independent verification really happens
# ---------------------------------------------------------------------------

def test_validator_reruns_the_whole_reading(host, registered, accounts):
    """
    The validator must fetch the constitution and call the model itself.

    Registration fetches once for the leader and once for the validator, and an
    adjudication does the same, so a passing submission shows two model calls and
    four fetches in total. A validator that only inspected the leader's JSON
    would leave the second model call absent.
    """
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    before_web = len(host.web_calls)

    submit(registered, accounts)

    assert len(host.llm_calls) == 2, "leader and validator must each call the model"
    assert len(host.web_calls) - before_web == 2, "leader and validator must each fetch the document"
    assert host.nondet_runs[-1]["validator_agreed"] is True


def test_registration_agrees_on_the_document_fingerprint(host, guard, accounts, serve_constitution):
    """Charter pinning runs its own leader/validator pair before any proposal."""
    result = json.loads(
        guard.register_charter(
            "Acme DAO", "Acme Protocol", CONSTITUTION_URL, 0, False, True,
            sender=accounts["steward"],
        )
    )
    assert len(result["doc_fingerprint"]) == 64
    assert len(host.web_calls) == 2, "the validator must fetch the document itself"
    run = host.nondet_runs[-1]
    assert run["validator_agreed"] is True
    assert run["leader_value"]["fingerprint"] == result["doc_fingerprint"]


# ---------------------------------------------------------------------------
# Disagreement on each consensus-bound field
# ---------------------------------------------------------------------------

def test_disagreement_on_the_ruling_fails_consensus(host, registered, accounts):
    """
    The ruling is what the gate returns, so validators must agree on it. The
    leader rules COMPLIANT and the validator rules NON_COMPLIANT.
    """
    host.mock_llm(r"constitutional review panel", [COMPLIANT_REPLY, NON_COMPLIANT_REPLY])

    with pytest.raises(ConsensusFailure):
        submit(registered, accounts)

    assert host.nondet_runs[-1]["validator_agreed"] is False
    assert registered.get_proposal_count("acme-dao") == 0
    assert registered.get_precedent_count("acme-dao") == 0


def test_disagreement_on_the_mandate_class_fails_consensus(host, registered, accounts):
    """
    Two validators that reject a proposal for unrelated reasons have not agreed.
    The mandate class is the ground of decision and the precedent's index, so it
    is bound even when the ruling matches.
    """
    host.mock_llm(
        r"constitutional review panel",
        [
            ruling_payload(ruling="NON_COMPLIANT", mandate_class="treasury_mandate"),
            ruling_payload(ruling="NON_COMPLIANT", mandate_class="authority_mandate"),
        ],
    )

    with pytest.raises(ConsensusFailure):
        submit(registered, accounts)
    assert registered.get_precedent_count("acme-dao") == 0


def test_disagreement_on_drift_fails_consensus(host, guard, accounts):
    """
    Drift is a consensus-bound boolean. A charter that permits adjudication under
    drift still requires validators to agree on whether the document changed,
    because the ruling was made against different text otherwise.

    Four fetches happen in this test: two to pin, then one per node to adjudicate.
    The fourth serves changed text, so only the validator sees drift.
    """
    changed = CONSTITUTION.replace("ten percent", "twenty five percent")
    host.mock_web(
        r"acme\.example/constitution$",
        [
            {"status": 200, "body": CONSTITUTION},
            {"status": 200, "body": CONSTITUTION},
            {"status": 200, "body": CONSTITUTION},
            {"status": 200, "body": changed},
        ],
    )
    guard.register_charter(
        "Acme DAO", "Acme Protocol", CONSTITUTION_URL, 0, False, False,
        sender=accounts["steward"],
    )
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)

    with pytest.raises(ConsensusFailure):
        submit(guard, accounts)
    assert guard.get_proposal_count("acme-dao") == 0


def test_unbound_prose_does_not_block_agreement(host, registered, accounts):
    """
    The prose no later panel reads is reported, not gated.

    `rationale` and `constitution_clause` are written for human review. Neither
    is ever read into a precedent corpus, so neither can change a future
    compliance outcome, and two nodes that word them completely differently
    still reach consensus. The principle is held identical here on purpose: it
    *is* bound, and the tests below are what exercise that.

    The corpus assertion is the load-bearing half of this test. If either field
    ever started reaching a later panel, it would have to be bound too, and this
    would fail rather than quietly pass.
    """
    host.mock_llm(
        r"constitutional review panel",
        [
            ruling_payload(
                ruling="NON_COMPLIANT",
                mandate_class="treasury_mandate",
                principle="A grant above the 50,000 USDC ceiling is impermissible.",
                clause="Article II section 2",
                rationale="The amount is above the ceiling.",
            ),
            ruling_payload(
                ruling="NON_COMPLIANT",
                mandate_class="Treasury Mandate",
                principle="A grant above the 50,000 USDC ceiling is impermissible.",
                clause="Art. II(2), the grant ceiling, read with the definitions",
                rationale="Wholly different wording, same conclusion and same ground.",
            ),
        ],
    )

    verdict = submit(registered, accounts)
    assert verdict["ruling"] == "NON_COMPLIANT"
    assert verdict["mandate_class"] == "treasury_mandate"
    assert host.nondet_runs[-1]["validator_agreed"] is True

    corpus = json.loads(registered.active_precedent_corpus("acme-dao"))
    rendered = corpus["rendered"]
    for unbound in ("Article II section 2", "Art. II(2)", "above the ceiling",
                    "Wholly different wording"):
        assert unbound not in rendered, (
            f"{unbound!r} is unbound prose and must never reach a later panel"
        )


# ---------------------------------------------------------------------------
# The ruling principle is reported; the case law is derived
# ---------------------------------------------------------------------------
#
# A panel writes one prose sentence, the ruling principle, stating the rule it
# thought the decision turned on. That sentence is free text no validator
# checks, so it is recorded on the ruling for human review and nothing in the
# contract treats it as case law.
#
# It cannot be bound. Five independent panels given the identical prompt, the
# identical pinned constitution and the identical proposal returned the same
# ruling and the same ground and wrote five substantively different rules: a
# per-grant cap, a per-quarter cap, a cap plus a review period, an affirmative
# dispensation, and no figure at all. Generalizing a rule from one decided case
# is underdetermined, so neither comparing the two sentences nor checking the
# leader's sentence against the constitution can settle it: both admit any of
# the five and leave the leader choosing which becomes binding.
#
# So the holding a later panel reads is derived, by `_render_holding`, from the
# three consensus-bound fields plus deterministic on chain data. These tests are
# the proof that no leader prose can move it.

def test_conflicting_principles_cannot_change_the_case_law(host, registered, accounts):
    """
    The reviewer's scenario, run end to end, and shown to be harmless.

    Two panels agree on the ruling and the ground, which is what the validator
    binds, and write irreconcilable rules: one caps a grant at 50,000 USDC, the
    other at 500,000. Under a design that stored the leader's sentence as case
    law, which rule a later panel inherited would depend on which node happened
    to lead, and those two rules decide a 200,000 grant differently.

    Here both runs are accepted and both produce byte-identical case law and an
    identical corpus commitment. Neither cap appears anywhere in it.
    """
    def case_law_after(leader_principle: str, validator_principle: str) -> tuple:
        host.reset()
        host.warp(1_800_000_000)
        host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
        guard = host.deploy(GUARD_SOURCE, "GovernanceMandateGuard",
                            args=["Acme Governance Registry"], sender=accounts["deployer"])
        guard.register_charter("Acme DAO", "Acme Protocol", CONSTITUTION_URL, 0, False, True,
                               sender=accounts["steward"])
        host.mock_llm(
            r"constitutional review panel",
            [
                ruling_payload(ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
                               principle=leader_principle),
                ruling_payload(ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
                               principle=validator_principle),
            ],
        )
        guard.submit_proposal("acme-dao", PROPOSAL_TITLE, PROPOSAL_BODY,
                              sender=accounts["proposer"])
        assert host.nondet_runs[-1]["validator_agreed"] is True
        corpus = json.loads(guard.active_precedent_corpus("acme-dao"))
        return corpus["rendered"], corpus["corpus_digest"]

    low = "A single grant may not exceed 50,000 USDC."
    high = "A single grant may not exceed 500,000 USDC."

    led_by_low = case_law_after(low, high)
    led_by_high = case_law_after(high, low)

    assert led_by_low == led_by_high, (
        "which node led must not change the case law a later panel inherits"
    )
    rendered = led_by_low[0]
    for unchecked in ("50,000", "500,000"):
        assert unchecked not in rendered, (
            f"{unchecked!r} came from one node's unchecked prose and must not "
            f"reach a later panel"
        )


def test_the_holding_is_derived_not_authored(host, registered, accounts):
    """
    The stored holding is exactly what an outside reviewer re-derives from the
    bound fields and the chain, computed here by an independent implementation.
    """
    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
                       principle="A single grant may not exceed 50,000 USDC."),
    )
    verdict = submit(registered, accounts)

    expected = holding_of(ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
                          title=PROPOSAL_TITLE, charter_version=1)
    assert verdict["holding"] == expected

    precedent = json.loads(registered.get_precedent(verdict["precedent_id"]))
    assert precedent["holding"] == expected
    corpus = json.loads(registered.active_precedent_corpus("acme-dao"))
    assert corpus["precedents"][0]["holding"] == expected


def test_the_holding_carries_the_ground_only_when_there_is_one(host, registered, accounts):
    """
    A compliant proposal implicates no provision, so the holding names no ground.
    A proposal decided on a provision names it, because that is the part of the
    decision a later panel reasons from.
    """
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    compliant = submit(registered, accounts)
    assert "on the ground of" not in compliant["holding"]
    assert "was ruled COMPLIANT." in compliant["holding"]

    # Routes are matched in registration order, so the first reply would shadow
    # the second. Clear and re-serve, which is how the registry suite sequences
    # two different rulings against one charter.
    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", AMENDMENT_REPLY)
    amended = submit(registered, accounts, title="Second", body=body_variant(7))
    assert "on the ground of procedural_mandate" in amended["holding"]


def test_the_holding_records_that_the_constitution_had_drifted(host, guard, accounts):
    """
    Drift is one of the three bound fields, so it can safely be stated in the
    case law. A later panel needs to know a precedent was decided against text
    that no longer matched the ratified document.
    """
    host.mock_web(
        r"acme\.example/constitution$",
        [
            {"status": 200, "body": CONSTITUTION},
            {"status": 200, "body": CONSTITUTION},
            {"status": 200, "body": CONSTITUTION.replace("50,000 USDC", "50,000 USDC (see note)")},
        ],
    )
    # `enforce_pinning=False`: this charter permits adjudication to continue
    # against a drifted document and records the drift, which is the only
    # configuration in which a holding can carry the drift note at all. Under
    # enforcement the gate refuses instead, which its own test covers.
    guard.register_charter("Acme DAO", "Acme Protocol", CONSTITUTION_URL, 0, False, False,
                           sender=accounts["steward"])
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    verdict = guard.submit_proposal("acme-dao", PROPOSAL_TITLE, PROPOSAL_BODY,
                                    sender=accounts["proposer"])
    verdict = json.loads(verdict)

    assert verdict["constitution_drift"] is True
    assert "had drifted from the ratified text" in verdict["holding"]
    assert verdict["holding"] == holding_of(drift=True, title=PROPOSAL_TITLE,
                                            charter_version=1)


def test_no_leader_prose_reaches_a_later_panel(host, registered, accounts):
    """
    The complete negative claim, over every free-text field at once.

    Each of the five unbound fields is filled with a phrase that appears nowhere
    else, and none of them may turn up in the case law, in any corpus entry
    value or in the corpus commitment preimage. They are all present on the
    ruling, because they are reported to people; that is the whole of their job.
    """
    markers = {
        "principle": "A grant of up to 999,111 ZEBRA units is always permissible.",
        "clause": "Appendix QQ, the aardvark provision",
        "rationale": "Reasoning that mentions the quokka ledger at length.",
        "amendments": "Rename the treasury to the wombat fund.",
    }
    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(
            ruling="AMENDMENT_REQUIRED", mandate_class="procedural_mandate",
            principle=markers["principle"], clause=markers["clause"],
            rationale=markers["rationale"], amendments=markers["amendments"],
            citations=["acme-dao#r9000"],
        ),
    )
    verdict = submit(registered, accounts)

    ruling = json.loads(registered.get_ruling(verdict["proposal_id"]))
    corpus = json.loads(registered.active_precedent_corpus("acme-dao"))
    entry_text = json.dumps(corpus["precedents"][0])

    for name, text in markers.items():
        assert text in json.dumps(ruling), f"{name} must still be reported on the ruling"
        assert text not in corpus["rendered"], (
            f"{name} is unchecked prose and must not reach a later panel"
        )
        assert text not in entry_text, (
            f"{name} must not appear in a corpus entry a later panel consumes"
        )
    assert "acme-dao#r9000" not in corpus["rendered"]


def test_a_principle_too_short_to_state_a_rule_is_refused(host, registered, accounts):
    """
    "n/a" is not a principle. The sentence is unbound, but it is still reported
    to people, so a reply that cannot carry a rule is an LLM error and forces
    rotation rather than entering an empty line into the registry.
    """
    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(ruling="COMPLIANT", mandate_class="none", principle="n/a"),
    )

    with pytest.raises(ConsensusFailure):
        submit(registered, accounts)
    assert "[LLM_ERROR]" in host.nondet_runs[-1]["leader_error"]
    assert "too short" in host.nondet_runs[-1]["leader_error"]


def test_differently_worded_principles_cost_no_extra_model_call(host, registered, accounts):
    """
    Binding only what can be bound keeps the adjudication to one model call per
    node. Two nodes stating different rules agree, and no second call is made to
    reconcile the prose, because nothing downstream depends on it.
    """
    host.mock_llm(
        r"constitutional review panel",
        [
            ruling_payload(ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
                           principle="A single grant may not exceed 50,000 USDC."),
            ruling_payload(ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
                           principle="Grants above the Article II ceiling are impermissible."),
        ],
    )

    verdict = submit(registered, accounts)
    assert host.nondet_runs[-1]["validator_agreed"] is True
    assert len(host.llm_calls) == 2, "one adjudication per node, nothing more"
    # The leader's sentence is what is reported, verbatim.
    assert verdict["principle"] == "A single grant may not exceed 50,000 USDC."


def test_the_prompt_tells_the_model_the_truth_about_what_binds(host, registered, accounts):
    """
    The prompt must not claim the principle is checked across validators, because
    it is not. Telling a model its sentence will be compared when it will not be
    is both false and a waste of the model's effort.
    """
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    submit(registered, accounts)

    prompt = host.llm_calls[-1]["prompt"]
    assert "WHAT BINDS" in prompt
    assert "does not need to match anyone else's" in prompt
    assert "generated from them by the contract itself" in prompt


def test_an_appeal_derives_its_own_holding(host, registered, accounts):
    """
    An appeal is a full re-adjudication that writes a new precedent, so its
    holding is derived the same way from the re-hearing's own bound output. The
    overruled precedent keeps the holding it was decided under.
    """
    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
                       principle="A single grant may not exceed 50,000 USDC."),
    )
    first = submit(registered, accounts)
    assert first["ruling"] == "NON_COMPLIANT"

    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    appealed = json.loads(registered.appeal_ruling(
        first["proposal_id"],
        "The Council resolution authorising this transfer was cited in the body.",
        sender=accounts["proposer"],
    ))

    assert appealed["ruling"] == "COMPLIANT"
    assert appealed["holding"] == holding_of(ruling="COMPLIANT", mandate_class="none",
                                            title=PROPOSAL_TITLE, charter_version=1)

    original = json.loads(registered.get_precedent(first["precedent_id"]))
    assert original["holding"] == holding_of(
        ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
        title=PROPOSAL_TITLE, charter_version=1)
    assert original["holding"] != appealed["holding"]


# ---------------------------------------------------------------------------
# Everything a later panel reads is bound or deterministic
# ---------------------------------------------------------------------------

def test_every_corpus_field_is_bound_or_deterministic(host, registered, accounts):
    """
    The structural guard on the whole class of defect.

    A corpus entry is the complete set of fields a later adjudication consumes.
    Every key in it must be consensus-bound or deterministic. This test
    enumerates them against that promise, so a field added to `_corpus_entry`
    later cannot quietly reintroduce an unbound, leader-authored input to future
    rulings: the suite fails until the new field is classified.

    `holding` and `holding_digest` are derived: `_render_holding` builds them
    from the bound fields and the chain, with nothing a leader wrote freely as an
    input, which is checked field by field by the derivation tests above.
    """
    consensus_bound = {"ruling", "mandate_class"}
    derived = {"holding", "holding_digest"}
    deterministic = {"precedent_id", "proposal_id", "charter_version",
                     "landmark", "decided_at"}

    host.mock_llm(r"constitutional review panel", NON_COMPLIANT_REPLY)
    submit(registered, accounts)

    corpus = json.loads(registered.active_precedent_corpus("acme-dao"))
    assert corpus["precedents"], "need at least one precedent to inspect"

    for entry in corpus["precedents"]:
        unclassified = set(entry) - consensus_bound - derived - deterministic
        assert not unclassified, (
            f"{sorted(unclassified)} reach a later panel but are neither "
            f"consensus-bound, derived from bound output, nor deterministic"
        )
        for field in consensus_bound | derived | deterministic:
            assert field in entry, f"{field} is classified but no longer present"

    # The unbound prose fields must be absent from the corpus entirely.
    ruling = json.loads(registered.get_ruling("acme-dao#p0"))
    for unbound in ("principle", "rationale", "constitution_clause",
                    "required_amendments", "cited_precedents"):
        assert unbound in ruling, f"{unbound} should still be reported on the ruling"
        assert unbound not in corpus["precedents"][0], (
            f"{unbound} is not consensus-bound and must not reach a later panel"
        )


def test_the_stored_holding_digest_commits_to_the_registry_text(host, registered, accounts):
    """
    The digest is what makes the stored precedent auditable. It is the
    Keccak-256 of the canonical key of the holding, so anyone can recompute it
    from the text in the registry and confirm the registry holds the case law
    the panel's bound output generated rather than something edited in
    afterwards.

    The same digest appears on the ruling, on the precedent and in the corpus
    entry, and it is mixed into the corpus commitment digest.
    """
    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
                       principle="A single grant may not exceed 50,000 USDC."),
    )
    verdict = submit(registered, accounts)

    ruling = json.loads(registered.get_ruling(verdict["proposal_id"]))
    precedent = json.loads(registered.get_precedent(verdict["precedent_id"]))
    corpus = json.loads(registered.active_precedent_corpus("acme-dao"))

    digest = verdict["holding_digest"]
    assert len(digest) == 64
    assert ruling["holding_digest"] == digest
    assert precedent["holding_digest"] == digest
    assert corpus["precedents"][0]["holding_digest"] == digest

    # Recomputed independently of the contract, the way a reviewer would.
    assert digest == principle_digest_of(precedent["holding"])

    # It commits to the derived holding, not to the leader's sentence.
    assert digest != principle_digest_of(ruling["principle"])


def test_the_corpus_digest_tracks_the_holding_and_not_the_prose(host, registered, accounts):
    """
    The corpus commitment covers the derived holding, so it moves when the case
    law moves and stays put when only unchecked prose moves.

    Both halves matter. If the digest ignored the holding it would not be
    evidence of what a past panel decided. If it tracked the prose, a leader
    could move every future node's view of the registry by rewording a sentence
    no one checked.
    """
    def digest_after(ruling: str, mandate_class: str, principle: str, title: str) -> str:
        # An AMENDMENT_REQUIRED ruling that states no amendments is refused by
        # the validator, so supply them for that ruling only.
        amendments = "State the Council resolution number." if ruling == "AMENDMENT_REQUIRED" else ""
        host.reset()
        host.warp(1_800_000_000)
        host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
        guard = host.deploy(GUARD_SOURCE, "GovernanceMandateGuard",
                            args=["Acme Governance Registry"], sender=accounts["deployer"])
        guard.register_charter("Acme DAO", "Acme Protocol", CONSTITUTION_URL, 0, False, True,
                               sender=accounts["steward"])
        host.mock_llm(
            r"constitutional review panel",
            ruling_payload(ruling=ruling, mandate_class=mandate_class, principle=principle,
                           amendments=amendments),
        )
        guard.submit_proposal("acme-dao", title, PROPOSAL_BODY, sender=accounts["proposer"])
        return json.loads(guard.active_precedent_corpus("acme-dao"))["corpus_digest"]

    low = "A single grant may not exceed 50,000 USDC."
    high = "A single grant may not exceed 500,000 USDC."

    base = digest_after("NON_COMPLIANT", "treasury_mandate", low, PROPOSAL_TITLE)

    # Unchecked prose cannot move the commitment.
    assert digest_after("NON_COMPLIANT", "treasury_mandate", high, PROPOSAL_TITLE) == base

    # A different ground of decision is a different holding.
    assert digest_after("NON_COMPLIANT", "scope_mandate", low, PROPOSAL_TITLE) != base

    # So are a different ruling and a different decided proposal.
    assert digest_after("AMENDMENT_REQUIRED", "treasury_mandate", low, PROPOSAL_TITLE) != base
    assert digest_after("NON_COMPLIANT", "treasury_mandate", low, "A different proposal") != base


def test_compliant_rulings_ignore_the_mandate_class(host, registered, accounts):
    """
    A compliant proposal implicates no provision, so the class is forced to
    `none` on both sides. Two nodes that both rule COMPLIANT agree even when
    they name different provisions, because the field carries no decision.
    """
    host.mock_llm(
        r"constitutional review panel",
        [
            ruling_payload(ruling="COMPLIANT", mandate_class="treasury_mandate"),
            ruling_payload(ruling="COMPLIANT", mandate_class="scope_mandate"),
        ],
    )

    verdict = submit(registered, accounts)
    assert verdict["ruling"] == "COMPLIANT"
    assert verdict["mandate_class"] == "none"
    assert host.nondet_runs[-1]["validator_agreed"] is True


# ---------------------------------------------------------------------------
# Leader failures and how validators treat them
# ---------------------------------------------------------------------------

def test_both_nodes_see_a_4xx_and_agree_on_it(host, registered, accounts):
    """
    A 4xx is the server's settled answer. Every node sees it, so the failure is
    classified EXTERNAL and compared for exact equality. Consensus is reached on
    the failure and the transaction reverts with that reason.
    """
    host.clear_routes()
    host.mock_web(r"acme\.example/constitution$", {"status": 404, "body": "gone"})
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)

    with pytest.raises(Exception) as excinfo:
        submit(registered, accounts)

    message = str(getattr(excinfo.value, "message", None) or excinfo.value)
    assert "[EXTERNAL]" in message and "404" in message
    assert host.nondet_runs[-1]["validator_agreed"] is True
    assert registered.get_proposal_count("acme-dao") == 0


def test_two_transient_failures_agree_without_identical_text(host, registered, accounts):
    """
    A 5xx is transient. Validators agree that the source was unreachable without
    requiring identical messages, which is the correct treatment for an outage:
    a 502 on one node and a 503 on another is still the same outage.
    """
    host.clear_routes()
    host.mock_web(
        r"acme\.example/constitution$",
        [{"status": 502, "body": "bad gateway"}, {"status": 503, "body": "unavailable"}],
    )
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)

    with pytest.raises(Exception) as excinfo:
        submit(registered, accounts)

    message = str(getattr(excinfo.value, "message", None) or excinfo.value)
    assert "[TRANSIENT]" in message
    assert host.nondet_runs[-1]["validator_agreed"] is True


def test_a_transient_leader_failure_is_rejected_by_a_working_validator(host, registered, accounts):
    """
    If the leader could not reach the source but the validator could, there is no
    agreement. The block fails and the network rotates rather than recording an
    outage as a ruling.
    """
    host.clear_routes()
    host.mock_web(
        r"acme\.example/constitution$",
        [{"status": 503, "body": "unavailable"}, {"status": 200, "body": CONSTITUTION}],
    )
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)

    with pytest.raises(ConsensusFailure):
        submit(registered, accounts)
    assert host.nondet_runs[-1]["validator_agreed"] is False


def test_a_model_failure_always_forces_rotation(host, registered, accounts):
    """
    An unparseable model reply is classified LLM_ERROR, which never agrees, even
    when the validator's model fails in exactly the same way. Freezing a broken
    reply into a constitutional ruling is the one outcome the gate must not allow.
    """
    host.mock_llm(r"constitutional review panel", "the panel is still deliberating")

    with pytest.raises(ConsensusFailure):
        submit(registered, accounts)
    assert host.nondet_runs[-1]["leader_error"].startswith("[LLM_ERROR]")
    assert host.nondet_runs[-1]["validator_agreed"] is False


def test_an_unrecognised_ruling_is_a_model_failure_not_a_default(host, registered, accounts):
    """
    A verdict the contract cannot map onto the three rulings must fail loudly.
    Defaulting either way would decide a constitutional question by accident.
    """
    host.mock_llm(
        r"constitutional review panel",
        json.dumps({"ruling": "????", "mandate_class": "none", "principle": "p"}),
    )

    with pytest.raises(ConsensusFailure):
        submit(registered, accounts)
    assert "[LLM_ERROR]" in host.nondet_runs[-1]["leader_error"]


def test_amendment_required_without_amendments_is_a_model_failure(host, registered, accounts):
    """
    An AMENDMENT_REQUIRED ruling that does not say what to amend is useless to
    the proposer, so the contract refuses it rather than storing an empty remedy.
    """
    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(ruling="AMENDMENT_REQUIRED", mandate_class="procedural_mandate", amendments=""),
    )

    with pytest.raises(ConsensusFailure):
        submit(registered, accounts)
    assert "required amendments" in host.nondet_runs[-1]["leader_error"]


def test_a_missing_principle_is_a_model_failure(host, registered, accounts):
    """
    The principle is the line that enters the precedent registry. A ruling
    without one contributes nothing to the DAO's case law, so it is refused.
    """
    host.mock_llm(
        r"constitutional review panel",
        json.dumps({"ruling": "COMPLIANT", "mandate_class": "none", "principle": "  "}),
    )

    with pytest.raises(ConsensusFailure):
        submit(registered, accounts)
    assert "ruling principle" in host.nondet_runs[-1]["leader_error"]


# ---------------------------------------------------------------------------
# Damaged but recoverable model output
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(
            "Here is the panel's decision:\n```json\n"
            + COMPLIANT_REPLY
            + "\n```\nThat concludes the review.",
            id="fenced_with_prose",
        ),
        pytest.param(
            '{"ruling": "COMPLIANT", "mandate_class": "none", '
            '"principle": "Within the ceiling.", "rationale": "ok",}',
            id="trailing_comma",
        ),
        pytest.param(
            '{"verdict": "compliant", "ground": "n/a", "holding": "Within the ceiling."}',
            id="aliased_field_names",
        ),
        pytest.param(
            '{"decision": "passes", "category": "none", "rule": "Within the ceiling."}',
            id="alternative_vocabulary",
        ),
    ],
)
def test_damaged_model_output_is_recovered(host, registered, accounts, reply):
    """
    Models rename fields, wrap JSON in prose and leave trailing commas. Every
    field read goes through an alias list and the text through a cleanup pass, so
    cosmetic damage does not cost a transaction. The same recovery runs on the
    validator, so both sides normalize identically.
    """
    host.mock_llm(r"constitutional review panel", reply)
    verdict = submit(registered, accounts)
    assert verdict["ruling"] == "COMPLIANT"
    assert verdict["mandate_class"] == "none"
    assert verdict["principle"]


def test_ruling_vocabulary_is_normalized_consistently(host, registered, accounts):
    """
    The leader says "reject" and the validator says "violates". Both map onto
    NON_COMPLIANT, so the nodes agree despite using different words, which is
    what the normalization exists to guarantee.
    """
    host.mock_llm(
        r"constitutional review panel",
        [
            json.dumps({"ruling": "reject", "mandate_class": "treasury", "principle": "Over the cap."}),
            json.dumps({"ruling": "violates", "mandate_class": "treasury budget", "principle": "Over the cap."}),
        ],
    )
    verdict = submit(registered, accounts)
    assert verdict["ruling"] == "NON_COMPLIANT"
    assert verdict["mandate_class"] == "treasury_mandate"
    assert host.nondet_runs[-1]["validator_agreed"] is True


# ---------------------------------------------------------------------------
# The determinism boundary
# ---------------------------------------------------------------------------

def test_nondeterministic_closures_never_capture_storage(host, registered, accounts):
    """
    The SDK warns when a storage manager is pickled, because storage reads inside
    a nondeterministic block are not supported. Every value the leader needs is
    copied into a local before the block, so cloudpickling the closure must
    produce no warning at all.

    This test turns that warning into an error for the duration of a submission.
    """
    import warnings

    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        verdict = submit(registered, accounts)
    assert verdict["ruling"] == "COMPLIANT"


def test_both_nodes_read_identical_case_law(host, registered, accounts):
    """
    The corpus is selected deterministically before the block and captured in the
    closure, so the leader and the validator are handed byte-identical case law.
    The two prompts for one adjudication must therefore carry the same corpus.
    """
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    submit(registered, accounts)

    host.mock_llm(r"constitutional review panel", NON_COMPLIANT_REPLY)
    submit(registered, accounts, title="Second proposal", body=PROPOSAL_BODY.replace("40,000", "45,000"))

    leader_prompt, validator_prompt = host.llm_calls[-2]["prompt"], host.llm_calls[-1]["prompt"]
    marker = "BINDING CASE LAW"
    assert marker in leader_prompt
    assert leader_prompt[leader_prompt.index(marker):] == validator_prompt[validator_prompt.index(marker):]


def test_the_stored_corpus_digest_matches_what_the_panel_was_given(host, registered, accounts):
    """
    A reviewer reading an old ruling must be able to confirm which precedents
    produced it. The digest published by `active_precedent_corpus` before a
    submission is the digest stored on the resulting proposal.
    """
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    submit(registered, accounts)

    expected = json.loads(registered.active_precedent_corpus("acme-dao"))
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    verdict = submit(registered, accounts, title="Another", body=PROPOSAL_BODY.replace("40,000", "41,000"))

    assert verdict["corpus_digest"] == expected["corpus_digest"]
    assert verdict["corpus_size"] == expected["corpus_size"] == 1
