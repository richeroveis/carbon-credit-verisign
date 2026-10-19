"""Scenario, accounting invariant and no backdoor tests."""

import re
from pathlib import Path

from conftest import (
    CONTRACT,
    ONE_CREDIT,
    assert_invariant,
    mock_audit,
    mock_site,
    warp_to,
)

EVIDENCE = (
    "Amazon Reforestation One. Registry: Verra VCS-1730. Quantified offset "
    "volume 120000 tonnes CO2e. Verification report issued 2025-02-01."
)
OTHER = (
    "Second project. Registry: Gold Standard GS-991. Quantified offset volume "
    "40000 tonnes CO2e. Verification report issued 2025-01-15."
)


def test_mixed_scenario_keeps_the_invariant_after_every_step(
    direct_vm, contract, direct_alice, direct_bob, recorder, pool
):
    # Step 1: Alice opens a claim, stake is locked.
    direct_vm.sender = direct_alice
    direct_vm.value = 50 * ONE_CREDIT
    contract.register_claim(
        "alice-1",
        "Amazon Reforestation One",
        "https://registry.example.org/projects/amazon-1",
        50 * ONE_CREDIT,
        "a" * 64,
    )
    direct_vm.value = 0
    assert_invariant(contract)

    # Step 2: Bob opens a second claim with free stake on the side.
    direct_vm.sender = direct_bob
    direct_vm.value = 20 * ONE_CREDIT
    contract.deposit_stake()
    direct_vm.value = 30 * ONE_CREDIT
    contract.register_claim(
        "bob-1",
        "Second Project",
        "https://registry.example.org/projects/second-1",
        30 * ONE_CREDIT,
        "b" * 64,
    )
    direct_vm.value = 0
    assert_invariant(contract)

    # Step 3: Alice full audit, escrow is minted but not spendable.
    mock_site(direct_vm, "registry.example.org/projects/amazon-1", EVIDENCE)
    mock_audit(direct_vm, "alice-1", "VERIFIED_FULL", 100)
    contract.verify_offset_claim_live("alice-1")
    snapshot = contract.vault_snapshot()
    assert snapshot["escrow_atto"] == 50 * ONE_CREDIT
    assert snapshot["credit_supply_atto"] == 50 * ONE_CREDIT
    assert snapshot["stake_free_atto"] == 20 * ONE_CREDIT
    assert snapshot["stake_locked_atto"] == 80 * ONE_CREDIT
    assert_invariant(contract)

    # Step 4: Bob partial audit at 25 percent of 30 credits, so 7.5 in escrow.
    bob_escrow = 30 * ONE_CREDIT * 25 // 100
    mock_site(direct_vm, "registry.example.org/projects/second-1", OTHER)
    mock_audit(direct_vm, "bob-1", "PARTIAL_OFFSET", 25)
    contract.verify_offset_claim_live("bob-1")
    snapshot = contract.vault_snapshot()
    assert snapshot["escrow_atto"] == 50 * ONE_CREDIT + bob_escrow
    assert snapshot["credit_supply_atto"] == 50 * ONE_CREDIT + bob_escrow
    assert_invariant(contract)

    # Step 5: Alice submits evidence and passes the second audit.
    direct_vm.sender = direct_alice
    contract.submit_evidence("alice-1", "https://rebuttal.example.org/alice", "c" * 64)
    direct_vm.clear_mocks()
    mock_site(direct_vm, "registry.example.org/projects/amazon-1", EVIDENCE)
    mock_site(direct_vm, "rebuttal.example.org/alice", EVIDENCE)
    mock_audit(direct_vm, "alice-1", "VERIFIED_FULL", 100)
    contract.verify_offset_claim_live("alice-1")
    assert contract.credit_balance(direct_alice) == 50 * ONE_CREDIT
    assert contract.free_stake(direct_alice) == 50 * ONE_CREDIT
    assert_invariant(contract)

    # Step 6: Alice transfers part of the credits and retires a slice.
    direct_vm.sender = direct_alice
    contract.transfer_credits(direct_bob, 20 * ONE_CREDIT)
    contract.retire_credits(5 * ONE_CREDIT)
    snapshot = contract.vault_snapshot()
    assert snapshot["credit_supply_atto"] == 45 * ONE_CREDIT + bob_escrow
    assert snapshot["burned_credits_atto"] == 5 * ONE_CREDIT
    assert snapshot["escrow_atto"] == bob_escrow
    assert_invariant(contract)

    # Step 7: Bob never files evidence, his escrow lapses at the deadline.
    bob_deadline = contract.get_claim("bob-1")["deadline"]
    warp_to(direct_vm, bob_deadline)
    contract.resolve_expired_claim("bob-1")
    snapshot = contract.vault_snapshot()
    assert snapshot["escrow_atto"] == 0
    assert snapshot["credit_supply_atto"] == 45 * ONE_CREDIT
    assert snapshot["burned_credits_atto"] == 5 * ONE_CREDIT + bob_escrow
    assert snapshot["stake_locked_atto"] == 0
    assert snapshot["stake_free_atto"] == 100 * ONE_CREDIT
    assert contract.free_stake(direct_bob) == 50 * ONE_CREDIT
    assert_invariant(contract)

    # Step 8: Bob withdraws his stake, the vault pays it out explicitly.
    direct_vm.sender = direct_bob
    contract.withdraw_stake(50 * ONE_CREDIT)
    assert contract.free_stake(direct_bob) == 0
    assert recorder.total_for(direct_bob) == 50 * ONE_CREDIT
    assert recorder.count_for(pool) == 0
    assert_invariant(contract)

    # Final supply check: only what is actually held plus burned.
    total = contract.vault_snapshot()
    assert total["credit_supply_atto"] == contract.credit_balance(direct_alice) + contract.credit_balance(direct_bob)
    assert total["open_claims"] == 0


def test_no_admin_surface_in_the_write_abi():
    """No setter, no pause, no ownership transfer, no upgrade entry point."""
    source = Path(CONTRACT).read_text(encoding="utf-8")
    names = re.findall(r"@gl\.public\.write(?:\.[a-z]+)?\n\s+def ([a-z_]+)\(", source)
    assert len(names) == 8, names
    for name in names:
        for banned in ("admin", "pause", "upgrade", "owner", "sweep", "rescue"):
            assert banned not in name, name
        assert not name.startswith("set_"), name


def test_parameters_are_written_only_in_the_constructor():
    source = Path(CONTRACT).read_text(encoding="utf-8")
    for field in (
        "self.community_pool = ",
        "self.verification_window_seconds = ",
        "self.max_claim_atto = ",
        "self.min_stake_atto = ",
    ):
        assert source.count(field) == 1, field
    init_body = source.split("def __init__", 1)[1]
    for field in (
        "self.community_pool = ",
        "self.verification_window_seconds = ",
        "self.max_claim_atto = ",
        "self.min_stake_atto = ",
    ):
        assert field in init_body, field


def test_only_one_sender_authority_check_exists():
    source = Path(CONTRACT).read_text(encoding="utf-8")
    comparisons = re.findall(r"gl\.message\.sender_address\s*(==|!=)\s*[^\n]+", source)
    assert comparisons == ["!="], comparisons
    assert "!= claim.owner" in source


def test_runner_is_pinned():
    first_line = Path(CONTRACT).read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith('# { "Depends": "py-genlayer:')
    assert "test" not in first_line.split(":")[-1].strip('" }')
    assert "latest" not in first_line


def test_public_write_surface_is_expected():
    source = Path(CONTRACT).read_text(encoding="utf-8")
    names = re.findall(r"@gl\.public\.write(?:\.[a-z]+)?\n\s+def ([a-z_]+)\(", source)
    assert sorted(names) == sorted(
        [
            "deposit_stake",
            "withdraw_stake",
            "register_claim",
            "submit_evidence",
            "verify_offset_claim_live",
            "resolve_expired_claim",
            "transfer_credits",
            "retire_credits",
        ]
    )
