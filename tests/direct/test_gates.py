"""Gate tests: evidence, rebuttal, deadlines, races and slashing."""

import pytest

from conftest import (
    ONE_CREDIT,
    assert_invariant,
    hexaddr,
    mock_audit,
    mock_site,
    warp_to,
)

EVIDENCE = (
    "Amazon Reforestation One. Registry: Verra VCS-1730. Quantified offset "
    "volume 120000 tonnes CO2e for the monitoring period 2024. Verification "
    "report issued 2025-02-01 by an accredited body."
)
REBUTTAL_URL = "https://rebuttal.example.org/report.pdf"


def _remock(direct_vm, rebuttal_body: str) -> None:
    """Replace mocks so the second audit sees the new rebuttal evidence.

    The second audit renders the primary URL again, so both mocks are needed.
    """
    direct_vm.clear_mocks()
    mock_site(direct_vm, "registry.example.org", EVIDENCE)
    mock_site(direct_vm, "rebuttal.example.org", rebuttal_body)


def _open(contract, claim_setup, direct_vm, claim_id="c-1"):
    contract, claim_id = claim_setup(
        claim_id=claim_id, requested=100 * ONE_CREDIT, stake=100 * ONE_CREDIT
    )
    mock_site(direct_vm, "registry.example.org", EVIDENCE)
    return contract, claim_id


def _positive(direct_vm, contract, claim_id, score=100, tier="VERIFIED_FULL"):
    mock_audit(direct_vm, claim_id, tier, score)
    contract.verify_offset_claim_live(claim_id)


def test_submit_evidence_only_by_owner(direct_vm, contract, claim_setup, direct_bob):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("not the claim owner"):
        contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)


def test_submit_evidence_rejected_before_an_audit(direct_vm, contract, claim_setup):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    with direct_vm.expect_revert("not waiting for evidence"):
        contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)


def test_submit_evidence_happy_path(direct_vm, contract, claim_setup):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    before = contract.get_claim(claim_id)

    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)

    after = contract.get_claim(claim_id)
    assert after["status"] == "EVIDENCE_SUBMITTED"
    assert after["evidence_url"] == REBUTTAL_URL
    assert after["evidence_hash"] == "b" * 64
    assert after["deadline"] >= before["deadline"]
    assert after["deadline"] == after["updated_at"] + 3600
    assert contract.claim_gate(claim_id)["blocker"] == "second live audit required before deadline"
    assert_invariant(contract)


def test_submit_evidence_after_deadline_rejected(direct_vm, contract, claim_setup):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    deadline = contract.get_claim(claim_id)["deadline"]

    warp_to(direct_vm, deadline)
    with direct_vm.expect_revert("evidence window closed"):
        contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)

    warp_to(direct_vm, deadline - 1)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    assert contract.get_claim(claim_id)["status"] == "EVIDENCE_SUBMITTED"


@pytest.mark.parametrize("url", ["http://x.example.org/a", "https://nodot", "https://a b.org/x"])
def test_submit_evidence_validates_url(direct_vm, contract, claim_setup, url):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    with direct_vm.expect_revert():
        contract.submit_evidence(claim_id, url, "b" * 64)


def test_second_positive_audit_issues_credits(direct_vm, contract, claim_setup, recorder, direct_alice):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    mock_site(direct_vm, "rebuttal.example.org", "Verification report 2025 for 120000 tonnes.")

    _positive(direct_vm, contract, claim_id)

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "CREDITS_ISSUED"
    assert claim["issued_atto"] == 100 * ONE_CREDIT
    assert claim["escrow_atto"] == 0
    assert claim["audit_count"] == 2
    assert claim["closed_at"] > 0
    assert contract.credit_balance(direct_alice) == 100 * ONE_CREDIT
    # The stake is returned to free stake, not slashed.
    assert contract.free_stake(direct_alice) == 100 * ONE_CREDIT
    assert recorder.count_for(contract.get_params()["community_pool"]) == 0
    snapshot = contract.vault_snapshot()
    assert snapshot["credit_supply_atto"] == 100 * ONE_CREDIT
    assert snapshot["issued_total_atto"] == 100 * ONE_CREDIT
    assert snapshot["escrow_atto"] == 0
    assert_invariant(contract)


def test_second_audit_lower_score_rebases_the_escrow(direct_vm, contract, claim_setup, direct_alice):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    _remock(direct_vm, "Verification report 2025 for 40000 tonnes.")

    _positive(direct_vm, contract, claim_id, score=25, tier="PARTIAL_OFFSET")

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "CREDITS_ISSUED"
    assert claim["issued_atto"] == 25 * ONE_CREDIT
    assert contract.credit_balance(direct_alice) == 25 * ONE_CREDIT
    snapshot = contract.vault_snapshot()
    assert snapshot["credit_supply_atto"] == 25 * ONE_CREDIT
    assert snapshot["burned_credits_atto"] == 75 * ONE_CREDIT
    assert_invariant(contract)


def test_second_fraud_finding_revokes_and_slashes(
    direct_vm, contract, claim_setup, recorder, pool, direct_alice
):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    mock_audit(direct_vm, claim_id, "REJECTED_FRAUD", 0)
    contract.verify_offset_claim_live(claim_id)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    _remock(direct_vm, "A page that still proves nothing at all about this project.")

    _positive(direct_vm, contract, claim_id, score=0, tier="REJECTED_FRAUD")

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "REVOKED_FRAUD"
    assert claim["fraud_strikes"] == 2
    assert claim["stake_atto"] == 0
    assert recorder.total_for(pool) == 100 * ONE_CREDIT
    assert recorder.count_for(pool) == 1
    assert recorder.transfers[-1]["on"] == "finalized"
    snapshot = contract.vault_snapshot()
    assert snapshot["slashed_atto"] == 100 * ONE_CREDIT
    assert snapshot["pool_paid_atto"] == 100 * ONE_CREDIT
    assert snapshot["stake_locked_atto"] == 0
    assert contract.free_stake(direct_alice) == 0
    assert contract.credit_balance(direct_alice) == 0
    assert_invariant(contract)


def test_positive_rebuttal_of_fraud_finding_issues_credits(
    direct_vm, contract, claim_setup, direct_alice, recorder, pool
):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    mock_audit(direct_vm, claim_id, "REJECTED_FRAUD", 0)
    contract.verify_offset_claim_live(claim_id)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    _remock(direct_vm, "Full verification report for 120000 tonnes.")

    _positive(direct_vm, contract, claim_id)

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "CREDITS_ISSUED"
    assert claim["fraud_strikes"] == 1
    assert contract.credit_balance(direct_alice) == 100 * ONE_CREDIT
    assert contract.free_stake(direct_alice) == 100 * ONE_CREDIT
    assert recorder.count_for(pool) == 0
    assert_invariant(contract)


def test_resolve_before_deadline_rejected(direct_vm, contract, claim_setup):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    deadline = contract.get_claim(claim_id)["deadline"]
    warp_to(direct_vm, deadline - 1)
    with direct_vm.expect_revert("deadline has not been reached"):
        contract.resolve_expired_claim(claim_id)


def test_resolve_at_deadline_expires_unverified_claim(direct_vm, contract, claim_setup, direct_alice):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    deadline = contract.get_claim(claim_id)["deadline"]
    warp_to(direct_vm, deadline)

    contract.resolve_expired_claim(claim_id)

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "EXPIRED_UNVERIFIED"
    assert claim["stake_atto"] == 0
    assert contract.free_stake(direct_alice) == 100 * ONE_CREDIT
    assert contract.vault_snapshot()["credit_supply_atto"] == 0
    assert_invariant(contract)


def test_resolve_lapsed_hold_burns_credits_and_returns_stake(
    direct_vm, contract, claim_setup, direct_alice
):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    assert contract.get_claim(claim_id)["escrow_atto"] == 100 * ONE_CREDIT
    deadline = contract.get_claim(claim_id)["deadline"]
    warp_to(direct_vm, deadline)

    contract.resolve_expired_claim(claim_id)

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "LAPSED_UNVERIFIED"
    assert claim["escrow_atto"] == 0
    assert claim["issued_atto"] == 0
    assert contract.credit_balance(direct_alice) == 0
    assert contract.free_stake(direct_alice) == 100 * ONE_CREDIT
    snapshot = contract.vault_snapshot()
    assert snapshot["credit_supply_atto"] == 0
    assert snapshot["burned_credits_atto"] == 100 * ONE_CREDIT
    assert_invariant(contract)


def test_resolve_lapsed_contest_slashes(direct_vm, contract, claim_setup, recorder, pool):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    mock_audit(direct_vm, claim_id, "REJECTED_FRAUD", 0)
    contract.verify_offset_claim_live(claim_id)
    deadline = contract.get_claim(claim_id)["deadline"]
    warp_to(direct_vm, deadline)

    contract.resolve_expired_claim(claim_id)

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "REVOKED_FRAUD"
    assert recorder.total_for(pool) == 100 * ONE_CREDIT
    assert contract.vault_snapshot()["slashed_atto"] == 100 * ONE_CREDIT
    assert_invariant(contract)


def test_second_audit_higher_score_releases_the_full_amount(
    direct_vm, contract, claim_setup, direct_alice
):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id, score=25, tier="PARTIAL_OFFSET")
    assert contract.get_claim(claim_id)["escrow_atto"] == 25 * ONE_CREDIT

    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    _remock(direct_vm, "Full verification report for 120000 tonnes.")
    _positive(direct_vm, contract, claim_id)

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "CREDITS_ISSUED"
    assert claim["issued_atto"] == 100 * ONE_CREDIT
    assert contract.credit_balance(direct_alice) == 100 * ONE_CREDIT
    snapshot = contract.vault_snapshot()
    assert snapshot["credit_supply_atto"] == 100 * ONE_CREDIT
    assert snapshot["burned_credits_atto"] == 0
    assert_invariant(contract)


def test_positive_hold_then_fraud_evidence_burns_escrow_and_contests(
    direct_vm, contract, claim_setup, direct_alice
):
    """A provisional winner that files fraudulent evidence loses the escrow."""
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    _remock(direct_vm, "A page that proves nothing about this project at all.")
    _positive(direct_vm, contract, claim_id, score=0, tier="REJECTED_FRAUD")

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "CONTESTED"
    assert claim["fraud_strikes"] == 1
    assert claim["escrow_atto"] == 0
    snapshot = contract.vault_snapshot()
    assert snapshot["burned_credits_atto"] == 100 * ONE_CREDIT
    assert snapshot["credit_supply_atto"] == 0
    assert snapshot["stake_locked_atto"] == 100 * ONE_CREDIT
    assert contract.free_stake(direct_alice) == 0
    assert_invariant(contract)


def test_empty_rebuttal_page_fails_closed(direct_vm, contract, claim_setup):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    _remock(direct_vm, "short")
    mock_audit(direct_vm, claim_id, "VERIFIED_FULL", 100)

    with direct_vm.expect_revert("[EXTERNAL]"):
        contract.verify_offset_claim_live(claim_id)

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "EVIDENCE_SUBMITTED"
    assert claim["escrow_atto"] == 100 * ONE_CREDIT
    assert_invariant(contract)


def test_unreachable_rebuttal_page_fails_closed(direct_vm, contract, claim_setup):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    direct_vm.clear_mocks()
    mock_site(direct_vm, "registry.example.org", EVIDENCE)
    direct_vm.mock_web(
        r".*rebuttal\.example\.org.*",
        {"response": {"status": 503, "headers": {}, "body": b""}},
    )
    mock_audit(direct_vm, claim_id, "VERIFIED_FULL", 100)

    with direct_vm.expect_revert():
        contract.verify_offset_claim_live(claim_id)

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "EVIDENCE_SUBMITTED"
    assert claim["escrow_atto"] == 100 * ONE_CREDIT
    assert_invariant(contract)


def test_resolve_after_rebuttal_without_reaudit_slashes(
    direct_vm, contract, claim_setup, recorder, pool
):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    mock_audit(direct_vm, claim_id, "REJECTED_FRAUD", 0)
    contract.verify_offset_claim_live(claim_id)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    deadline = contract.get_claim(claim_id)["deadline"]
    warp_to(direct_vm, deadline)

    contract.resolve_expired_claim(claim_id)

    assert contract.get_claim(claim_id)["status"] == "REVOKED_FRAUD"
    assert recorder.total_for(pool) == 100 * ONE_CREDIT
    assert_invariant(contract)


def test_resolve_after_rebuttal_without_reaudit_keeps_stake_when_no_fraud(
    direct_vm, contract, claim_setup, direct_alice
):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    deadline = contract.get_claim(claim_id)["deadline"]
    warp_to(direct_vm, deadline)

    contract.resolve_expired_claim(claim_id)

    claim = contract.get_claim(claim_id)
    assert claim["status"] == "LAPSED_UNVERIFIED"
    assert contract.free_stake(direct_alice) == 100 * ONE_CREDIT
    assert_invariant(contract)


def test_resolve_twice_rejected(direct_vm, contract, claim_setup):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    deadline = contract.get_claim(claim_id)["deadline"]
    warp_to(direct_vm, deadline)
    contract.resolve_expired_claim(claim_id)
    with direct_vm.expect_revert("already closed"):
        contract.resolve_expired_claim(claim_id)


def test_no_second_rebuttal_after_two_strikes(direct_vm, contract, claim_setup):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    mock_audit(direct_vm, claim_id, "REJECTED_FRAUD", 0)
    contract.verify_offset_claim_live(claim_id)
    contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    _remock(direct_vm, "Still nothing verifiable in this page, no registry, no volume.")
    _positive(direct_vm, contract, claim_id, score=0, tier="REJECTED_FRAUD")

    with direct_vm.expect_revert():
        contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    with direct_vm.expect_revert():
        contract.verify_offset_claim_live(claim_id)


def test_deadline_race_is_exclusive(direct_vm, contract, claim_setup):
    """At the exact deadline only resolution is legal, never evidence or audit."""
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    _positive(direct_vm, contract, claim_id)
    deadline = contract.get_claim(claim_id)["deadline"]
    mock_audit(direct_vm, claim_id, "VERIFIED_FULL", 100)

    warp_to(direct_vm, deadline)
    with direct_vm.expect_revert("evidence window closed"):
        contract.submit_evidence(claim_id, REBUTTAL_URL, "b" * 64)
    with direct_vm.expect_revert("claim is not open for audit"):
        contract.verify_offset_claim_live(claim_id)

    contract.resolve_expired_claim(claim_id)
    assert contract.get_claim(claim_id)["status"] == "LAPSED_UNVERIFIED"


def test_revoked_claim_cannot_be_audited_again(direct_vm, contract, claim_setup, recorder, pool):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    mock_audit(direct_vm, claim_id, "REJECTED_FRAUD", 0)
    contract.verify_offset_claim_live(claim_id)
    deadline = contract.get_claim(claim_id)["deadline"]
    warp_to(direct_vm, deadline)
    contract.resolve_expired_claim(claim_id)

    with direct_vm.expect_revert("claim is not open for audit"):
        contract.verify_offset_claim_live(claim_id)
    assert recorder.total_for(pool) == 100 * ONE_CREDIT


def test_pool_never_receives_more_than_the_stake(direct_vm, contract, claim_setup, recorder, pool):
    contract, claim_id = _open(contract, claim_setup, direct_vm)
    mock_audit(direct_vm, claim_id, "REJECTED_FRAUD", 0)
    contract.verify_offset_claim_live(claim_id)
    deadline = contract.get_claim(claim_id)["deadline"]
    warp_to(direct_vm, deadline)
    contract.resolve_expired_claim(claim_id)
    with direct_vm.expect_revert("already closed"):
        contract.resolve_expired_claim(claim_id)

    assert recorder.count_for(pool) == 1
    assert recorder.total_for(pool) == 100 * ONE_CREDIT
