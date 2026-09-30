"""
The precedent registry: the part that compounds.

Every adjudication writes a precedent, whichever way it went, and later
adjudications are bound by that growing body of case law. These tests cover what
goes in, what the next panel is shown, how landmarks and overruling change that,
and the bounds that keep selection cheap as a registry grows.
"""

import json

import pytest

from conftest import (
    AMENDMENT_REPLY,
    COMPLIANT_REPLY,
    CONSTITUTION,
    NON_COMPLIANT_REPLY,
    PROPOSAL_BODY,
    revert_message,
    ruling_payload,
    submit,
)


def body_variant(index: int) -> str:
    """A distinct proposal body, since identical text is refused as a replay."""
    return (
        f"Allocate {10_000 + index * 137} USDC from the Treasury to working group {index} "
        f"for the coming quarter. Authorised by Council resolution 2027-{index:02d}."
    )


def fill(host, guard, accounts, count: int, reply: str = COMPLIANT_REPLY) -> list[dict]:
    """Adjudicate `count` distinct proposals with the same reply."""
    results = []
    for index in range(count):
        host.mock_llm(r"constitutional review panel", reply)
        results.append(submit(guard, accounts, title=f"Proposal {index}", body=body_variant(index)))
        host.clear_routes()
        host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    return results


# ---------------------------------------------------------------------------
# What gets recorded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "reply,expected_ruling,expected_class",
    [
        (COMPLIANT_REPLY, "COMPLIANT", "none"),
        (NON_COMPLIANT_REPLY, "NON_COMPLIANT", "treasury_mandate"),
        (AMENDMENT_REPLY, "AMENDMENT_REQUIRED", "procedural_mandate"),
    ],
)
def test_every_ruling_becomes_a_precedent(
    host, registered, accounts, reply, expected_ruling, expected_class
):
    """
    Acceptances are recorded as well as rejections. A registry that only kept
    rejections would teach future panels that nothing has ever been allowed.
    """
    host.mock_llm(r"constitutional review panel", reply)
    verdict = submit(registered, accounts)

    assert registered.get_precedent_count("acme-dao") == 1
    precedent = json.loads(registered.get_precedent(verdict["precedent_id"]))
    assert precedent["ruling"] == expected_ruling
    assert precedent["mandate_class"] == expected_class
    assert precedent["proposal_id"] == verdict["proposal_id"]
    assert precedent["principle"] == verdict["principle"]
    assert precedent["charter_version"] == 1
    assert precedent["landmark"] is False
    assert precedent["overruled"] is False


def test_the_principle_is_the_line_future_panels_read(host, registered, accounts):
    """
    The one-line principle recorded by a ruling appears verbatim in the case law
    handed to the next adjudication. That is the whole mechanism by which the
    registry accumulates meaning rather than volume.
    """
    principle = "A grant that names no Council resolution is procedurally defective."
    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(ruling="NON_COMPLIANT", mandate_class="procedural_mandate", principle=principle),
    )
    first = submit(registered, accounts)

    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    submit(registered, accounts, title="Second", body=body_variant(99))

    prompt = host.llm_calls[-1]["prompt"]
    assert principle in prompt
    assert first["precedent_id"] in prompt
    assert "NON_COMPLIANT" in prompt


def test_the_first_proposal_is_told_there_is_no_case_law(host, registered, accounts):
    """An empty registry says so explicitly rather than presenting an empty list."""
    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    submit(registered, accounts)
    assert "No prior rulings exist for this DAO" in host.llm_calls[0]["prompt"]


def test_precedent_ids_and_proposal_ids_are_sequential_and_scoped(host, registered, accounts):
    """Identifiers carry the DAO, so two registries never collide."""
    results = fill(host, registered, accounts, 3)
    assert [r["proposal_id"] for r in results] == ["acme-dao#p0", "acme-dao#p1", "acme-dao#p2"]
    assert [r["precedent_id"] for r in results] == ["acme-dao#r0", "acme-dao#r1", "acme-dao#r2"]
    for index in range(3):
        assert registered.get_proposal_id_at("acme-dao", index) == f"acme-dao#p{index}"
        assert registered.get_precedent_id_at("acme-dao", index) == f"acme-dao#r{index}"


# ---------------------------------------------------------------------------
# Corpus selection
# ---------------------------------------------------------------------------

def test_the_corpus_is_capped_and_takes_the_most_recent(host, registered, accounts):
    """
    Selection is bounded, so a DAO with a long history still adjudicates in
    predictable gas. Beyond the cap the most recent rulings are the ones applied.
    """
    fill(host, registered, accounts, 15)
    corpus = json.loads(registered.active_precedent_corpus("acme-dao"))

    assert registered.get_precedent_count("acme-dao") == 15
    assert corpus["corpus_size"] == corpus["max_corpus"] == 12
    ids = [entry["precedent_id"] for entry in corpus["precedents"]]
    assert ids[0] == "acme-dao#r14", "newest first"
    assert ids == [f"acme-dao#r{n}" for n in range(14, 2, -1)]


def test_landmarks_are_taken_first_and_labelled_binding(host, registered, accounts):
    """
    A landmark is pulled into the corpus ahead of ordinary rulings and is
    presented as binding, so a foundational early ruling is never pushed out by
    the volume of later routine decisions.
    """
    fill(host, registered, accounts, 15)
    registered.mark_landmark("acme-dao#r0", sender=accounts["steward"])

    corpus = json.loads(registered.active_precedent_corpus("acme-dao"))
    assert corpus["precedents"][0]["precedent_id"] == "acme-dao#r0"
    assert corpus["precedents"][0]["landmark"] is True
    assert "LANDMARK, binding" in corpus["rendered"]
    assert corpus["corpus_size"] == 12

    ids = [entry["precedent_id"] for entry in corpus["precedents"]]
    assert len(ids) == len(set(ids)), "a landmark must not be listed twice"


def test_landmark_slots_are_bounded_so_ordinary_rulings_still_appear(host, registered, accounts):
    """
    Landmarks cannot crowd out the recent record entirely. At most half the
    corpus is drawn from the landmark index, so the panel always sees current
    practice alongside the foundational rulings.
    """
    fill(host, registered, accounts, 14)
    for index in range(8):
        registered.mark_landmark(f"acme-dao#r{index}", sender=accounts["steward"])

    corpus = json.loads(registered.active_precedent_corpus("acme-dao"))
    landmarks = [e for e in corpus["precedents"] if e["landmark"]]
    ordinary = [e for e in corpus["precedents"] if not e["landmark"]]
    assert len(landmarks) == corpus["max_landmarks"] == 6
    assert len(ordinary) == 6
    assert ordinary[0]["precedent_id"] == "acme-dao#r13"


def test_overruled_precedents_leave_the_corpus_but_not_the_record(host, registered, accounts):
    """
    Overruling retires a ruling from active case law without deleting it. A court
    that could erase its own history would not be one, so the entry, the reason
    and the timestamp remain readable.
    """
    fill(host, registered, accounts, 3)
    before = json.loads(registered.active_precedent_corpus("acme-dao"))
    assert "acme-dao#r1" in [e["precedent_id"] for e in before["precedents"]]

    host.warp(1_800_050_000)
    result = json.loads(
        registered.overrule_precedent(
            "acme-dao#r1", "Superseded by the Council's revised funding policy",
            sender=accounts["steward"],
        )
    )
    assert result["overruled"] is True

    after = json.loads(registered.active_precedent_corpus("acme-dao"))
    assert "acme-dao#r1" not in [e["precedent_id"] for e in after["precedents"]]
    assert after["corpus_digest"] != before["corpus_digest"]

    record = json.loads(registered.get_precedent("acme-dao#r1"))
    assert record["overruled"] is True
    assert record["overruled_reason"] == "Superseded by the Council's revised funding policy"
    assert record["overruled_at"] == 1_800_050_000
    assert registered.get_precedent_count("acme-dao") == 3, "the count is history, not a live set"


def test_an_overruled_landmark_is_excluded_too(host, registered, accounts):
    """Retiring a landmark removes it from the landmark pass as well."""
    fill(host, registered, accounts, 2)
    registered.mark_landmark("acme-dao#r0", sender=accounts["steward"])
    registered.overrule_precedent("acme-dao#r0", "No longer good law", sender=accounts["steward"])

    corpus = json.loads(registered.active_precedent_corpus("acme-dao"))
    assert [e["precedent_id"] for e in corpus["precedents"]] == ["acme-dao#r1"]


def test_the_corpus_digest_changes_with_the_corpus(host, registered, accounts):
    """
    The digest commits to the ordered set of precedents and their principles, so
    any change to the case law changes it and an unchanged corpus reproduces it.
    """
    fill(host, registered, accounts, 2)
    first = json.loads(registered.active_precedent_corpus("acme-dao"))["corpus_digest"]
    assert json.loads(registered.active_precedent_corpus("acme-dao"))["corpus_digest"] == first

    registered.mark_landmark("acme-dao#r0", sender=accounts["steward"])
    second = json.loads(registered.active_precedent_corpus("acme-dao"))["corpus_digest"]
    assert second != first, "reordering the corpus must change its commitment"


def test_registries_are_isolated_between_daos(host, guard, accounts, serve_constitution):
    """
    The registry is multi-tenant. One DAO's case law must never reach another's
    panel, because a precedent is an interpretation of one specific charter.
    """
    host.mock_web(r"other\.example/charter$", {"status": 200, "body": "Article I. Anything goes."})
    guard.register_charter("Acme DAO", "Acme", "https://acme.example/constitution", 0, False, True,
                           sender=accounts["steward"])
    guard.register_charter("Other DAO", "Other", "https://other.example/charter", 0, False, True,
                           sender=accounts["outsider"])

    host.mock_llm(
        r"constitutional review panel",
        ruling_payload(ruling="NON_COMPLIANT", mandate_class="treasury_mandate",
                       principle="Acme specific holding about the treasury ceiling."),
    )
    submit(guard, accounts)

    host.mock_llm(r"constitutional review panel", COMPLIANT_REPLY)
    json.loads(guard.submit_proposal("other-dao", "Unrelated",
        "Fund a translation effort with 5,000 USDC from the Other DAO treasury this quarter.",
        sender=accounts["proposer"]))

    prompt = host.llm_calls[-1]["prompt"]
    assert "Acme specific holding" not in prompt
    assert "No prior rulings exist for this DAO" in prompt
    assert guard.get_precedent_count("acme-dao") == 1
    assert guard.get_precedent_count("other-dao") == 1


# ---------------------------------------------------------------------------
# Registry maintenance rules
# ---------------------------------------------------------------------------

def test_only_the_steward_may_landmark_or_overrule(host, registered, accounts):
    """Case law management is a charter power, not a public one."""
    fill(host, registered, accounts, 1)

    with pytest.raises(Exception) as excinfo:
        registered.mark_landmark("acme-dao#r0", sender=accounts["outsider"])
    assert "Only the charter steward" in revert_message(excinfo)

    with pytest.raises(Exception) as excinfo:
        registered.overrule_precedent("acme-dao#r0", "because", sender=accounts["outsider"])
    assert "Only the charter steward" in revert_message(excinfo)

    assert json.loads(registered.get_precedent("acme-dao#r0"))["landmark"] is False


def test_landmarking_is_idempotent_by_rejection(host, registered, accounts):
    """
    A second landmark call is refused rather than appending a duplicate index
    entry, which would let one ruling consume several corpus slots.
    """
    fill(host, registered, accounts, 1)
    registered.mark_landmark("acme-dao#r0", sender=accounts["steward"])
    with pytest.raises(Exception) as excinfo:
        registered.mark_landmark("acme-dao#r0", sender=accounts["steward"])
    assert "already a landmark" in revert_message(excinfo)
    assert json.loads(registered.get_charter("acme-dao"))["landmark_total"] == 1


def test_an_overruled_precedent_cannot_be_made_a_landmark(host, registered, accounts):
    """Retired law cannot be promoted to binding law."""
    fill(host, registered, accounts, 1)
    registered.overrule_precedent("acme-dao#r0", "Bad law", sender=accounts["steward"])
    with pytest.raises(Exception) as excinfo:
        registered.mark_landmark("acme-dao#r0", sender=accounts["steward"])
    assert "overruled precedent cannot be made a landmark" in revert_message(excinfo)


def test_overruling_requires_a_reason_and_happens_once(host, registered, accounts):
    """The reason is the record, so it is mandatory."""
    fill(host, registered, accounts, 1)

    with pytest.raises(Exception) as excinfo:
        registered.overrule_precedent("acme-dao#r0", "   ", sender=accounts["steward"])
    assert "reason is required" in revert_message(excinfo)

    registered.overrule_precedent("acme-dao#r0", "Superseded", sender=accounts["steward"])
    with pytest.raises(Exception) as excinfo:
        registered.overrule_precedent("acme-dao#r0", "Again", sender=accounts["steward"])
    assert "already overruled" in revert_message(excinfo)


def test_unknown_precedent_ids_are_rejected(host, registered, accounts):
    for method, args in [
        ("mark_landmark", ("acme-dao#r99",)),
        ("overrule_precedent", ("acme-dao#r99", "reason")),
        ("get_precedent", ("acme-dao#r99",)),
    ]:
        with pytest.raises(Exception) as excinfo:
            getattr(registered, method)(*args, sender=accounts["steward"])
        assert "Unknown precedent" in revert_message(excinfo)


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------

def test_list_precedents_pages_oldest_first_and_flags_retired_entries(host, registered, accounts):
    fill(host, registered, accounts, 5)
    registered.overrule_precedent("acme-dao#r2", "Superseded", sender=accounts["steward"])
    registered.mark_landmark("acme-dao#r1", sender=accounts["steward"])

    page = json.loads(registered.list_precedents("acme-dao", 0, 3))
    assert page["total"] == 5
    assert page["returned"] == 3
    assert [e["precedent_id"] for e in page["precedents"]] == ["acme-dao#r0", "acme-dao#r1", "acme-dao#r2"]
    assert page["precedents"][1]["landmark"] is True
    assert page["precedents"][2]["overruled"] is True

    tail = json.loads(registered.list_precedents("acme-dao", 3, 50))
    assert [e["precedent_id"] for e in tail["precedents"]] == ["acme-dao#r3", "acme-dao#r4"]

    assert json.loads(registered.list_precedents("acme-dao", 99, 10))["returned"] == 0


def test_paging_limit_is_capped(host, registered, accounts):
    """A caller cannot request an unbounded page."""
    fill(host, registered, accounts, 2)
    page = json.loads(registered.list_precedents("acme-dao", 0, 100_000))
    assert page["returned"] == 2
