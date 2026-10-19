"""Credit ledger tests: transfer, retire, zero values and supply accounting."""

import pytest

from conftest import ONE_CREDIT, assert_invariant, mock_audit, mock_site

EVIDENCE = (
    "Amazon Reforestation One. Registry: Verra VCS-1730. Quantified offset "
    "volume 120000 tonnes CO2e. Verification report issued 2025-02-01."
)


def _issue(direct_vm, contract, direct_alice, amount=100 * ONE_CREDIT, claim_id="c-1"):
    direct_vm.sender = direct_alice
    direct_vm.value = amount
    contract.register_claim(
        claim_id,
        "Amazon Reforestation One",
        "https://registry.example.org/projects/amazon-1",
        amount,
        "a" * 64,
    )
    direct_vm.value = 0
    mock_site(direct_vm, "registry.example.org", EVIDENCE)
    mock_audit(direct_vm, claim_id, "VERIFIED_FULL", 100)
    contract.verify_offset_claim_live(claim_id)
    contract.submit_evidence(claim_id, "https://rebuttal.example.org/report", "b" * 64)
    mock_site(direct_vm, "rebuttal.example.org", EVIDENCE)
    mock_audit(direct_vm, claim_id, "VERIFIED_FULL", 100)
    contract.verify_offset_claim_live(claim_id)
    return claim_id


def test_transfer_credits(direct_vm, contract, direct_alice, direct_bob):
    _issue(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_alice
    contract.transfer_credits(direct_bob, 30 * ONE_CREDIT)

    assert contract.credit_balance(direct_alice) == 70 * ONE_CREDIT
    assert contract.credit_balance(direct_bob) == 30 * ONE_CREDIT
    assert contract.vault_snapshot()["credit_supply_atto"] == 100 * ONE_CREDIT
    assert_invariant(contract)


def test_transfer_more_than_balance_rejected(direct_vm, contract, direct_alice, direct_bob):
    _issue(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("credit balance is too small"):
        contract.transfer_credits(direct_bob, 101 * ONE_CREDIT)
    assert contract.credit_balance(direct_alice) == 100 * ONE_CREDIT


@pytest.mark.parametrize("amount", [0, -5])
def test_transfer_zero_rejected(direct_vm, contract, direct_alice, direct_bob, amount):
    _issue(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("amount must be positive"):
        contract.transfer_credits(direct_bob, amount)


def test_transfer_to_zero_address_rejected(direct_vm, contract, direct_alice):
    _issue(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("zero address"):
        contract.transfer_credits(bytes(20), ONE_CREDIT)


def test_transfer_to_the_contract_itself_rejected(direct_vm, contract, direct_alice):
    _issue(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_alice
    own = direct_vm._contract_address
    with direct_vm.expect_revert("this contract"):
        contract.transfer_credits(bytes(own), ONE_CREDIT)


def test_sender_without_credits_cannot_transfer(direct_vm, contract, direct_alice, direct_bob):
    _issue(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("credit balance is too small"):
        contract.transfer_credits(direct_alice, ONE_CREDIT)


def test_retire_burns_supply(direct_vm, contract, direct_alice):
    _issue(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_alice
    contract.retire_credits(25 * ONE_CREDIT)

    assert contract.credit_balance(direct_alice) == 75 * ONE_CREDIT
    snapshot = contract.vault_snapshot()
    assert snapshot["credit_supply_atto"] == 75 * ONE_CREDIT
    assert snapshot["burned_credits_atto"] == 25 * ONE_CREDIT
    assert_invariant(contract)


def test_retire_more_than_balance_rejected(direct_vm, contract, direct_alice):
    _issue(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("credit balance is too small"):
        contract.retire_credits(101 * ONE_CREDIT)


def test_retire_zero_rejected(direct_vm, contract, direct_alice):
    _issue(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("amount must be positive"):
        contract.retire_credits(0)


def test_escrowed_credits_are_not_spendable(direct_vm, contract, direct_alice, direct_bob):
    """A claim in PROVISIONAL_HOLD has escrow but the owner cannot move it."""
    direct_vm.sender = direct_alice
    direct_vm.value = 10 * ONE_CREDIT
    contract.register_claim(
        "c-9",
        "Amazon Reforestation One",
        "https://registry.example.org/projects/amazon-1",
        10 * ONE_CREDIT,
        "a" * 64,
    )
    direct_vm.value = 0
    mock_site(direct_vm, "registry.example.org", EVIDENCE)
    mock_audit(direct_vm, "c-9", "VERIFIED_FULL", 100)
    contract.verify_offset_claim_live("c-9")

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("credit balance is too small"):
        contract.transfer_credits(direct_bob, ONE_CREDIT)
    assert contract.vault_snapshot()["escrow_atto"] == 10 * ONE_CREDIT
    assert_invariant(contract)
