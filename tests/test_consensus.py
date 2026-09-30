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
    NON_COMPLIANT_REPLY,
    PROPOSAL_BODY,
    PROPOSAL_TITLE,
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


def test_agreement_survives_different_prose(host, registered, accounts):
    """
    Only the decision is bound. Two nodes that agree on the ruling and the ground
    must reach consensus even when their reasoning, clause citation and principle
    are worded completely differently, because prose is reported, never gated.
    """
    host.mock_llm(
        r"constitutional review panel",
        [
            ruling_payload(
                ruling="NON_COMPLIANT",
                mandate_class="treasury_mandate",
                principle="Grants over the Article II ceiling are not permitted.",
                clause="Article II section 2",
                rationale="The amount is above the ceiling.",
            ),
            ruling_payload(
                ruling="NON_COMPLIANT",
                mandate_class="Treasury Mandate",
                principle="The Treasury cannot exceed the fifty thousand dollar grant cap.",
                clause="Art. II(2), grant ceiling",
                rationale="Wholly different wording, same conclusion and same ground.",
            ),
        ],
    )

    verdict = submit(registered, accounts)
    assert verdict["ruling"] == "NON_COMPLIANT"
    assert verdict["mandate_class"] == "treasury_mandate"
    assert host.nondet_runs[-1]["validator_agreed"] is True


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
