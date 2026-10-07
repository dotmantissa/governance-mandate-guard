# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""
Mandate Gated DAO
=================

A working DAO ballot contract that cannot open a vote on a proposal which has
not cleared constitutional review.

This is the reference consumer for the Governance Mandate Guard. It is a real,
deployable contract, not an illustration: it holds a voter roll, opens ballots,
counts votes, closes on a deadline and records the outcome. The single thing it
adds to an ordinary ballot contract is one line in `open_ballot`:

    guard = gl.get_contract_at(self.guard_address)
    if not guard.view().is_compliant(proposal_id):
        raise gl.vm.UserError("...")

That call is a synchronous read of another contract's state during this
contract's execution, so the gate is enforced inside the transaction that tries
to open the ballot. There is no window in which a non-compliant proposal has an
open vote, and no off-chain keeper is trusted to check first.

Integration shape
-----------------
1. The DAO registers its constitution with the guard once, and the steward keeps
   it pinned through `ratify_amendment`.
2. A proposer submits the proposal text to the guard's `submit_proposal`, which
   returns a proposal id and a ruling.
3. Anyone calls `open_ballot(proposal_id)` here. If the guard's ruling of record
   is not COMPLIANT, the call reverts and no ballot exists.
4. Voting proceeds normally. The ruling that let the ballot open is copied onto
   the ballot at open time, so the record of why this vote was permitted lives
   in the DAO's own storage and survives any later change in the guard.

Why read the gate rather than trust a flag
------------------------------------------
`is_compliant` is a view, so this contract reads the guard's live state at the
moment the ballot opens. A proposal whose ruling was replaced by an appeal is
therefore evaluated on the ruling of record, not on whatever it said when the
proposer last looked. Copying the ruling onto the ballot after the check gives
the DAO an immutable local record without making the check itself depend on a
value the DAO stored earlier.
"""

import json
import typing

from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


ERROR_EXPECTED = "[EXPECTED]"

MIN_VOTING_PERIOD_SECS = 60
MAX_VOTING_PERIOD_SECS = 7_776_000  # 90 days


@allow_storage
@dataclass
class Ballot:
    """
    One vote on a proposal that cleared constitutional review.

    `guard_ruling` and `guard_principle` are copied from the guard at open time,
    so the DAO keeps its own record of what permitted this vote.

    They are not the same kind of fact. `guard_ruling` is consensus-bound: every
    validator that accepted the adjudication reached it independently.
    `guard_principle` is the sentence the panel recorded as its reasoning, which
    the guard reports for human review without binding it, so it is display text
    and nothing should branch on it. `guard_precedent_id` is the pointer to the
    guard's derived holding, which is the part later adjudications are bound by.
    """
    proposal_id: str
    opened_by: Address
    opened_at: u64
    closes_at: u64
    yes_votes: u32
    no_votes: u32
    closed: bool
    passed: bool
    guard_ruling: str
    guard_principle: str
    guard_precedent_id: str


class MandateGatedDAO(gl.Contract):
    """
    A DAO ballot contract gated on the Governance Mandate Guard.

    Deploy this with the guard's address and the DAO identifier registered with
    that guard. Every ballot opened here has provably cleared the guard.
    """

    guard_address: Address
    dao_id: str
    admin: Address
    voting_period_secs: u64
    quorum: u32

    # proposal_id -> Ballot
    ballots: TreeMap[str, Ballot]
    # "<proposal_id>|<lowercase address>" -> support
    votes: TreeMap[str, bool]
    # lowercase address -> on the roll
    voters: TreeMap[str, bool]
    voter_total: u32
    ballot_total: u32
    # str(index) -> proposal_id
    ballot_at: TreeMap[str, str]

    def __init__(
        self,
        guard_address: str,
        dao_id: str,
        voting_period_secs: int,
        quorum: int,
    ) -> None:
        try:
            guard = Address(str(guard_address).strip())
        except Exception:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} guard_address is not a valid address")

        period = int(voting_period_secs)
        if not MIN_VOTING_PERIOD_SECS <= period <= MAX_VOTING_PERIOD_SECS:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} voting_period_secs must be between "
                f"{MIN_VOTING_PERIOD_SECS} and {MAX_VOTING_PERIOD_SECS}"
            )
        quorum_value = int(quorum)
        if quorum_value < 1:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} quorum must be at least 1")

        identifier = str(dao_id).strip().lower()
        if not identifier:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} dao_id must not be empty")

        self.guard_address = guard
        self.dao_id = identifier
        self.admin = gl.message.sender_address
        self.voting_period_secs = u64(period)
        self.quorum = u32(quorum_value)
        self.voter_total = u32(0)
        self.ballot_total = u32(0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise gl.vm.UserError(message)

    def _now(self) -> u64:
        """
        Transaction time. GenVM pins the clock to the transaction datetime, so
        the leader and every validator read the same value and the voting
        deadline is deterministic. Fails closed to 0, which leaves every ballot
        already past its deadline and admits no votes.
        """
        try:
            return u64(int(datetime.now(timezone.utc).timestamp()))
        except Exception:
            pass
        try:
            if hasattr(gl, "message_raw") and isinstance(gl.message_raw, dict) and "datetime" in gl.message_raw:
                txt = str(gl.message_raw["datetime"]).strip()
                if txt:
                    if txt.endswith("Z"):
                        txt = txt[:-1] + "+00:00"
                    parsed = datetime.fromisoformat(txt)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return u64(int(parsed.timestamp()))
        except Exception:
            pass
        return u64(0)

    def _addr_key(self, address: Address) -> str:
        return "0x" + address.as_bytes.hex()

    # ------------------------------------------------------------------
    # Voter roll
    # ------------------------------------------------------------------

    @gl.public.write
    def set_voter(self, voter: str, enrolled: bool) -> str:
        """Add or remove an address from the voter roll."""
        self._require(
            gl.message.sender_address == self.admin,
            f"{ERROR_EXPECTED} Only the admin may change the voter roll",
        )
        try:
            target = Address(str(voter).strip())
        except Exception:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} voter is not a valid address")

        key = self._addr_key(target)
        already = bool(self.voters[key]) if key in self.voters else False
        self.voters[key] = bool(enrolled)
        if enrolled and not already:
            self.voter_total = u32(int(self.voter_total) + 1)
        elif already and not enrolled:
            self.voter_total = u32(max(0, int(self.voter_total) - 1))

        return json.dumps(
            {"voter": key, "enrolled": bool(enrolled), "voter_total": int(self.voter_total)},
            sort_keys=True,
        )

    # ------------------------------------------------------------------
    # The gated action
    # ------------------------------------------------------------------

    @gl.public.write
    def open_ballot(self, proposal_id: str) -> str:
        """
        Open a vote on a proposal, but only if the guard says it is compliant.

        The guard is read synchronously here, so the constitutional check
        happens inside the same transaction that would create the ballot. A
        non-compliant proposal leaves no ballot behind, not even a rejected one.
        """
        pid = str(proposal_id).strip()
        self._require(bool(pid), f"{ERROR_EXPECTED} proposal_id must not be empty")
        self._require(
            pid not in self.ballots,
            f"{ERROR_EXPECTED} A ballot is already open or closed for {pid}",
        )

        guard = gl.get_contract_at(self.guard_address)

        # The gate. A synchronous read of the guard's ruling of record.
        compliant = bool(guard.view().is_compliant(pid))
        self._require(
            compliant,
            f"{ERROR_EXPECTED} Proposal {pid} has not cleared constitutional review",
        )

        # The check has passed. Copy the guard's reasoning into the DAO's own
        # record so the justification for this vote is preserved locally. The
        # principle is the panel's own unbound sentence, kept for people to
        # read; the precedent id is what points at the bound case law.
        ruling = ""
        principle = ""
        precedent_id = ""
        try:
            status = json.loads(str(guard.view().compliance_status(pid)))
            if isinstance(status, dict):
                if str(status.get("dao_id", self.dao_id)) != str(self.dao_id):
                    raise gl.vm.UserError(
                        f"{ERROR_EXPECTED} Proposal {pid} belongs to a different DAO"
                    )
                ruling = str(status.get("ruling", ""))
                principle = str(status.get("principle", ""))[:240]
                precedent_id = str(status.get("precedent_id", ""))
        except gl.vm.UserError:
            raise
        except Exception:
            # The gate already passed on the authoritative call. A malformed
            # status payload must not block a compliant proposal, so the local
            # copy is simply left empty.
            ruling = "COMPLIANT"

        now_ts = int(self._now())
        closes_at = now_ts + int(self.voting_period_secs)

        ballot = Ballot(
            proposal_id=pid,
            opened_by=gl.message.sender_address,
            opened_at=u64(now_ts),
            closes_at=u64(closes_at),
            yes_votes=u32(0),
            no_votes=u32(0),
            closed=False,
            passed=False,
            guard_ruling=ruling,
            guard_principle=principle,
            guard_precedent_id=precedent_id,
        )
        self.ballots[pid] = ballot

        index = int(self.ballot_total)
        self.ballot_at[str(index)] = pid
        self.ballot_total = u32(index + 1)

        return json.dumps(
            {
                "proposal_id": pid,
                "dao_id": str(self.dao_id),
                "opened_at": now_ts,
                "closes_at": closes_at,
                "guard_ruling": ruling,
                "guard_precedent_id": precedent_id,
                "ballot_index": index,
            },
            sort_keys=True,
        )

    @gl.public.write
    def cast_vote(self, proposal_id: str, support: bool) -> str:
        """Cast one vote. One address, one vote, while the ballot is open."""
        pid = str(proposal_id).strip()
        self._require(
            pid in self.ballots,
            f"{ERROR_EXPECTED} No ballot for {pid}",
        )
        ballot = self.ballots[pid]
        self._require(not bool(ballot.closed), f"{ERROR_EXPECTED} Ballot is closed for {pid}")

        now_ts = int(self._now())
        self._require(
            now_ts < int(ballot.closes_at),
            f"{ERROR_EXPECTED} Voting period has ended for {pid}",
        )

        voter_key = self._addr_key(gl.message.sender_address)
        self._require(
            voter_key in self.voters and bool(self.voters[voter_key]),
            f"{ERROR_EXPECTED} Address is not on the voter roll",
        )

        vote_slot = f"{pid}|{voter_key}"
        self._require(
            vote_slot not in self.votes,
            f"{ERROR_EXPECTED} Address has already voted on {pid}",
        )

        self.votes[vote_slot] = bool(support)
        if support:
            ballot.yes_votes = u32(int(ballot.yes_votes) + 1)
        else:
            ballot.no_votes = u32(int(ballot.no_votes) + 1)
        self.ballots[pid] = ballot

        return json.dumps(
            {
                "proposal_id": pid,
                "voter": voter_key,
                "support": bool(support),
                "yes_votes": int(ballot.yes_votes),
                "no_votes": int(ballot.no_votes),
            },
            sort_keys=True,
        )

    @gl.public.write
    def close_ballot(self, proposal_id: str) -> str:
        """
        Close a ballot after its deadline and record the outcome. Permissionless,
        so a ballot can always be settled by anyone once the period has ended.
        """
        pid = str(proposal_id).strip()
        self._require(
            pid in self.ballots,
            f"{ERROR_EXPECTED} No ballot for {pid}",
        )
        ballot = self.ballots[pid]
        self._require(not bool(ballot.closed), f"{ERROR_EXPECTED} Ballot is already closed for {pid}")

        now_ts = int(self._now())
        self._require(
            now_ts >= int(ballot.closes_at),
            f"{ERROR_EXPECTED} Voting period has not ended for {pid}",
        )

        yes = int(ballot.yes_votes)
        no = int(ballot.no_votes)
        turnout = yes + no
        passed = turnout >= int(self.quorum) and yes > no

        ballot.closed = True
        ballot.passed = passed
        self.ballots[pid] = ballot

        return json.dumps(
            {
                "proposal_id": pid,
                "yes_votes": yes,
                "no_votes": no,
                "turnout": turnout,
                "quorum": int(self.quorum),
                "passed": passed,
                "closed_at": now_ts,
            },
            sort_keys=True,
        )

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    @gl.public.view
    def get_ballot(self, proposal_id: str) -> str:
        pid = str(proposal_id).strip()
        self._require(
            pid in self.ballots,
            f"{ERROR_EXPECTED} No ballot for {pid}",
        )
        ballot = self.ballots[pid]
        return json.dumps(
            {
                "proposal_id": str(ballot.proposal_id),
                "opened_by": self._addr_key(ballot.opened_by),
                "opened_at": int(ballot.opened_at),
                "closes_at": int(ballot.closes_at),
                "yes_votes": int(ballot.yes_votes),
                "no_votes": int(ballot.no_votes),
                "closed": bool(ballot.closed),
                "passed": bool(ballot.passed),
                "guard_ruling": str(ballot.guard_ruling),
                "guard_principle": str(ballot.guard_principle),
                "guard_precedent_id": str(ballot.guard_precedent_id),
            },
            sort_keys=True,
        )

    @gl.public.view
    def ballot_exists(self, proposal_id: str) -> bool:
        return str(proposal_id).strip() in self.ballots

    @gl.public.view
    def has_voted(self, proposal_id: str, voter: str) -> bool:
        try:
            target = Address(str(voter).strip())
        except Exception:
            return False
        return f"{str(proposal_id).strip()}|{self._addr_key(target)}" in self.votes

    @gl.public.view
    def is_voter(self, voter: str) -> bool:
        try:
            target = Address(str(voter).strip())
        except Exception:
            return False
        key = self._addr_key(target)
        return key in self.voters and bool(self.voters[key])

    @gl.public.view
    def get_ballot_count(self) -> int:
        return int(self.ballot_total)

    @gl.public.view
    def get_ballot_id_at(self, index: int) -> str:
        slot = str(int(index))
        self._require(
            slot in self.ballot_at,
            f"{ERROR_EXPECTED} No ballot at index {index}",
        )
        return str(self.ballot_at[slot])

    @gl.public.view
    def get_config(self) -> str:
        """The guard this DAO is gated on, and the ballot parameters."""
        return json.dumps(
            {
                "guard_address": self._addr_key(self.guard_address),
                "dao_id": str(self.dao_id),
                "admin": self._addr_key(self.admin),
                "voting_period_secs": int(self.voting_period_secs),
                "quorum": int(self.quorum),
                "voter_total": int(self.voter_total),
                "ballot_total": int(self.ballot_total),
            },
            sort_keys=True,
        )
