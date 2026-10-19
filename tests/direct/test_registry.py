"""Registry tests: input validation, stake locking and vault operations."""

import pytest

from conftest import (
    MAX_CLAIM,
    MIN_STAKE,
    ONE_CREDIT,
    assert_invariant,
    hexaddr,
    warp_to,
)

URL = "https://registry.example.org/projects/amazon-1"
HASH = "a" * 64


def _register(direct_vm, contract, sender, claim_id="c-1", requested=10 * ONE_CREDIT,
              stake=10 * ONE_CREDIT, name="Amazon One", url=URL, digest=HASH):
    direct_vm.sender = sender
    direct_vm.value = stake
    contract.register_claim(claim_id, name, url, requested, digest)
    direct_vm.value = 0


def test_register_locks_the_whole_stake(direct_vm, contract, direct_alice):
    _register(direct_vm, contract, direct_alice)

    claim = contract.get_claim("c-1")
    assert claim["status"] == "PENDING_AUDIT"
    assert claim["owner"].lower() == hexaddr(direct_alice)
    assert claim["stake_atto"] == 10 * ONE_CREDIT
    assert claim["requested_atto"] == 10 * ONE_CREDIT
    assert claim["evidence_hash"] == HASH
    assert claim["tier"] == ""
    assert claim["metric_score"] == 0
    assert claim["created_at"] == claim["updated_at"]
    assert claim["deadline"] == claim["created_at"] + 3600
    assert contract.free_stake(direct_alice) == 0
    assert contract.vault_snapshot()["stake_locked_atto"] == 10 * ONE_CREDIT
    assert contract.vault_snapshot()["claim_count"] == 1
    assert_invariant(contract)


def test_duplicate_claim_id_rejected(direct_vm, contract, direct_alice):
    _register(direct_vm, contract, direct_alice)
    with direct_vm.expect_revert("already registered"):
        _register(direct_vm, contract, direct_alice)


@pytest.mark.parametrize("claim_id", ["ab", "a" * 65, "bad id", "bad/id", "quote\"id"])
def test_claim_id_validation(direct_vm, contract, direct_alice, claim_id):
    with direct_vm.expect_revert("claim id"):
        _register(direct_vm, contract, direct_alice, claim_id=claim_id)


@pytest.mark.parametrize("name", ["ab", "n" * 129, "   ", "Bad<Name", "Bad{Name}", "line\nbreak"])
def test_project_name_validation(direct_vm, contract, direct_alice, name):
    with direct_vm.expect_revert("project name"):
        _register(direct_vm, contract, direct_alice, name=name)


@pytest.mark.parametrize(
    "url",
    [
        "http://registry.example.org/p",
        "ftp://registry.example.org/p",
        "https://localhost/p",
        "https://registry.example.org/with space",
        "short",
        "https://registry.example.org/{marker}",
        "https://" + "a" * 600 + ".org/p",
    ],
)
def test_url_validation(direct_vm, contract, direct_alice, url):
    with direct_vm.expect_revert():
        _register(direct_vm, contract, direct_alice, url=url)


@pytest.mark.parametrize("digest", ["a" * 63, "a" * 65, "z" * 64, ""])
def test_evidence_hash_validation(direct_vm, contract, direct_alice, digest):
    with direct_vm.expect_revert("evidence hash"):
        _register(direct_vm, contract, direct_alice, digest=digest)


def test_evidence_hash_is_normalised_to_lowercase(direct_vm, contract, direct_alice):
    _register(direct_vm, contract, direct_alice, digest="AB" * 32)
    assert contract.get_claim("c-1")["evidence_hash"] == "ab" * 32


@pytest.mark.parametrize("requested", [0, ONE_CREDIT - 1, MAX_CLAIM + 1])
def test_requested_amount_bounds(direct_vm, contract, direct_alice, requested):
    with direct_vm.expect_revert():
        _register(direct_vm, contract, direct_alice, requested=requested, stake=MAX_CLAIM)


def test_stake_below_minimum_rejected(direct_vm, contract, direct_alice):
    with direct_vm.expect_revert("stake below minimum"):
        _register(direct_vm, contract, direct_alice, requested=ONE_CREDIT, stake=MIN_STAKE - 1)


def test_stake_below_coverage_rejected(direct_vm, contract, direct_alice):
    # 8 credits need at least 2 credits of stake; 1 credit is above the floor
    # but below the coverage ratio.
    with direct_vm.expect_revert("stake below coverage ratio"):
        _register(
            direct_vm,
            contract,
            direct_alice,
            requested=8 * ONE_CREDIT,
            stake=ONE_CREDIT,
        )


def test_exact_coverage_is_accepted(direct_vm, contract, direct_alice):
    _register(direct_vm, contract, direct_alice, requested=4 * ONE_CREDIT, stake=ONE_CREDIT)
    assert contract.get_claim("c-1")["stake_atto"] == ONE_CREDIT


def test_zero_value_registration_rejected(direct_vm, contract, direct_alice):
    with direct_vm.expect_revert("stake below minimum"):
        _register(direct_vm, contract, direct_alice, stake=0)


def test_deposit_and_withdraw_free_stake(direct_vm, contract, direct_alice, recorder):
    direct_vm.sender = direct_alice
    direct_vm.value = 5 * ONE_CREDIT
    contract.deposit_stake()
    direct_vm.value = 0
    assert contract.free_stake(direct_alice) == 5 * ONE_CREDIT

    balance_before = contract.free_stake(direct_alice)
    contract.withdraw_stake(2 * ONE_CREDIT)
    assert contract.free_stake(direct_alice) == balance_before - 2 * ONE_CREDIT
    assert recorder.total_for(direct_alice) == 2 * ONE_CREDIT
    assert recorder.transfers[-1]["on"] == "finalized"
    assert_invariant(contract)


def test_withdraw_more_than_free_rejected(direct_vm, contract, direct_alice, recorder):
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_CREDIT
    contract.deposit_stake()
    direct_vm.value = 0

    with direct_vm.expect_revert("free stake is too small"):
        contract.withdraw_stake(ONE_CREDIT + 1)
    assert recorder.count_for(direct_alice) == 0


@pytest.mark.parametrize("amount", [0, -1])
def test_withdraw_zero_rejected(direct_vm, contract, direct_alice, amount):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("amount must be positive"):
        contract.withdraw_stake(amount)


def test_deposit_zero_rejected(direct_vm, contract, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("deposit must be positive"):
        contract.deposit_stake()


def test_claim_gate_reports_the_audit_blocker(direct_vm, contract, direct_alice):
    _register(direct_vm, contract, direct_alice)
    gate = contract.claim_gate("c-1")
    assert gate["blocked"] is True
    assert gate["blocker"] == "live audit required"
    assert gate["audit_allowed"] is True
    assert gate["resolve_allowed"] is False
    assert gate["rebuttal_allowed"] is False


def test_claim_gate_resolve_flips_after_deadline(direct_vm, contract, direct_alice):
    _register(direct_vm, contract, direct_alice)
    deadline = contract.get_claim("c-1")["deadline"]
    warp_to(direct_vm, deadline)
    gate = contract.claim_gate("c-1")
    assert gate["audit_allowed"] is False
    assert gate["resolve_allowed"] is True


def test_unknown_claim_views_revert(direct_vm, contract, direct_alice):
    with direct_vm.expect_revert("unknown claim"):
        contract.get_claim("nope")
    with direct_vm.expect_revert("unknown claim"):
        contract.claim_gate("nope")


def test_other_owner_is_isolated(direct_vm, contract, direct_alice, direct_bob):
    _register(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = 5 * ONE_CREDIT
    contract.register_claim("c-2", "Other Project", URL, 5 * ONE_CREDIT, HASH)
    direct_vm.value = 0

    assert contract.get_claim("c-1")["owner"].lower() == hexaddr(direct_alice)
    assert contract.get_claim("c-2")["owner"].lower() == hexaddr(direct_bob)
    assert contract.vault_snapshot()["stake_locked_atto"] == 15 * ONE_CREDIT
    assert_invariant(contract)
