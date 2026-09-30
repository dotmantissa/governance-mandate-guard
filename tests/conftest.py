"""
Shared fixtures for the Governance Mandate Guard suite.

Every test runs the real contract source through the real GenLayer SDK using the
in-process GenVM host in `genvm_host.py`. Nothing about the contract is stubbed:
the only substituted surfaces are the two the node itself owns, the web fetch
and the model call, which is what `direct_vm.mock_web` and `direct_vm.mock_llm`
substitute in GenLayer's own tooling.
"""

from __future__ import annotations

import json
import sys

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from genvm_host import bootstrap, ConsensusFailure, NoRouteError  # noqa: E402

GUARD_SOURCE = ROOT / "contracts" / "governance_mandate_guard.py"
DAO_SOURCE = ROOT / "contracts" / "mandate_gated_dao.py"

CONSTITUTION_URL = "https://acme.example/constitution"
AMENDED_URL = "https://acme.example/constitution-v2"

# A realistic governing document. Numeric limits, an authority clause and an
# amendment procedure, because those are the three provisions proposals most
# often collide with.
CONSTITUTION = """
ACME PROTOCOL CONSTITUTION, ratified 12 March 2024

Article I. Purpose.
The Protocol exists to operate a decentralised over-collateralised lending
market and to fund work that directly sustains that market.

Article II. Treasury.
1. The Treasury may not allocate more than ten percent of reserves to a single
   counterparty in any calendar quarter. Reserves means the total assets held by
   the Treasury at the opening of that quarter.
2. No single grant may exceed 50,000 USDC.
3. The Treasury may not acquire assets that are not directly used by the market.

Article III. Authority.
1. Only the elected Council may authorise a transfer from the Treasury.
2. The Council may not delegate that authority to a single signer.

Article IV. Token holder rights.
1. No proposal may dilute existing token holders without offering them a
   pro rata subscription right.

Article V. Amendment.
This Constitution may be amended only by a proposal that passes with at least a
two thirds supermajority of votes cast, after a review period of no fewer than
fourteen days.
"""

AMENDED_CONSTITUTION = CONSTITUTION.replace(
    "No single grant may exceed 50,000 USDC.",
    "No single grant may exceed 250,000 USDC.",
)

# A fixed point in time. Every test that cares about the clock warps explicitly.
T0 = 1_800_000_000  # 2027-01-15T08:00:00Z


def ruling_payload(
    ruling: str = "COMPLIANT",
    mandate_class: str = "none",
    principle: str = "A grant within the stated ceiling and signed by the Council is permissible.",
    clause: str = "Article II, section 2.",
    rationale: str = "The proposal falls inside the stated limit.",
    amendments: str = "",
    citations: list | None = None,
) -> str:
    """A well-formed adjudication reply, as JSON text."""
    return json.dumps(
        {
            "ruling": ruling,
            "mandate_class": mandate_class,
            "principle": principle,
            "constitution_clause": clause,
            "rationale": rationale,
            "required_amendments": amendments,
            "cited_precedents": citations if citations is not None else [],
        }
    )


COMPLIANT_REPLY = ruling_payload()
NON_COMPLIANT_REPLY = ruling_payload(
    ruling="NON_COMPLIANT",
    mandate_class="treasury_mandate",
    principle="A grant above the 50,000 USDC ceiling in Article II is impermissible.",
    rationale="The requested amount exceeds the stated ceiling.",
)
AMENDMENT_REPLY = ruling_payload(
    ruling="AMENDMENT_REQUIRED",
    mandate_class="procedural_mandate",
    principle="A treasury proposal must name the Council resolution that authorises the transfer.",
    rationale="The substance is permissible but the authorising resolution is not identified.",
    amendments="State the Council resolution number and the signing quorum in the proposal body.",
)

PROPOSAL_TITLE = "Fund the documentation working group"
PROPOSAL_BODY = (
    "Allocate 40,000 USDC from the Treasury to the documentation working group for "
    "the next two quarters. The transfer is authorised by Council resolution 2027-04 "
    "and signed under the standard multisig quorum."
)
OVERSIZED_BODY = (
    "Allocate 1,200,000 USDC from the Treasury to Acme Labs as a strategic partnership "
    "tranche in this calendar quarter. Opening reserves for the quarter were 6,000,000 USDC."
)


@pytest.fixture(scope="session")
def genvm():
    """The GenVM host. One per session, since the SDK imports are process wide."""
    return bootstrap()


@pytest.fixture
def host(genvm):
    """A clean host for each test: fresh storage, no routes, no call log."""
    genvm.reset()
    genvm.warp(T0)
    return genvm


@pytest.fixture
def accounts(host):
    """Named test addresses."""
    return {
        "deployer": host.new_address(),
        "steward": host.new_address(),
        "proposer": host.new_address(),
        "outsider": host.new_address(),
        "voter_a": host.new_address(),
        "voter_b": host.new_address(),
    }


@pytest.fixture
def serve_constitution(host):
    """Serve the ratified constitution at its URL."""
    host.mock_web(r"acme\.example/constitution$", {"status": 200, "body": CONSTITUTION})
    return CONSTITUTION


@pytest.fixture
def guard(host, accounts):
    """A deployed, unregistered guard."""
    return host.deploy(
        GUARD_SOURCE,
        "GovernanceMandateGuard",
        args=["Acme Governance Registry"],
        sender=accounts["deployer"],
    )


@pytest.fixture
def registered(host, guard, accounts, serve_constitution):
    """A guard with the Acme charter registered, no cooldown, open proposers."""
    guard.register_charter(
        "Acme DAO",
        "Acme Protocol",
        CONSTITUTION_URL,
        0,
        False,
        True,
        sender=accounts["steward"],
    )
    return guard


def adjudicate_with(host, reply, pattern: str = r"constitutional review panel"):
    """Register the adjudication reply the panel will return."""
    host.mock_llm(pattern, reply)


def submit(guard, accounts, title: str = PROPOSAL_TITLE, body: str = PROPOSAL_BODY, sender=None):
    """Submit a proposal and return the parsed verdict."""
    return json.loads(
        guard.submit_proposal(
            "acme-dao",
            title,
            body,
            sender=sender or accounts["proposer"],
        )
    )


def revert_message(excinfo) -> str:
    """The revert reason from a caught contract error."""
    exc = excinfo.value
    return str(getattr(exc, "message", None) or exc)
