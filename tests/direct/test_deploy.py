"""Constructor and parameter tests: floors and ceilings on every window."""

import hashlib
from pathlib import Path

import pytest

from conftest import CONTRACT, MAX_CLAIM, MIN_STAKE, WINDOW


def test_params_are_immutable_and_visible(direct_deploy, pool):
    contract = direct_deploy(CONTRACT, pool, WINDOW, MAX_CLAIM, MIN_STAKE)
    params = contract.get_params()
    assert params["community_pool"].lower() == "0x" + bytes(pool).hex()
    assert params["verification_window_seconds"] == WINDOW
    assert params["max_claim_atto"] == MAX_CLAIM
    assert params["min_stake_atto"] == MIN_STAKE
    assert params["attos_per_credit"] == 10 ** 18
    assert params["max_strikes"] == 2


def test_zero_pool_rejected(direct_deploy):
    with pytest.raises(Exception, match="zero address"):
        direct_deploy(CONTRACT, bytes(20), WINDOW, MAX_CLAIM, MIN_STAKE)


def test_self_as_pool_rejected(direct_deploy):
    own_address = hashlib.sha256(str(Path(CONTRACT).resolve()).encode()).digest()[:20]
    with pytest.raises(Exception, match="this contract"):
        direct_deploy(CONTRACT, own_address, WINDOW, MAX_CLAIM, MIN_STAKE)


@pytest.mark.parametrize("window", [3599, 0, 90 * 24 * 3600 + 1])
def test_window_bounds(direct_deploy, pool, window):
    with pytest.raises(Exception, match="verification window out of range"):
        direct_deploy(CONTRACT, pool, window, MAX_CLAIM, MIN_STAKE)


@pytest.mark.parametrize("window", [3600, 90 * 24 * 3600])
def test_window_boundary_values_accepted(direct_deploy, pool, window):
    contract = direct_deploy(CONTRACT, pool, window, MAX_CLAIM, MIN_STAKE)
    assert contract.get_params()["verification_window_seconds"] == window


@pytest.mark.parametrize("cap", [0, 10 ** 18 - 1, 10 ** 27 + 1])
def test_max_claim_bounds(direct_deploy, pool, cap):
    with pytest.raises(Exception, match="max claim out of range"):
        direct_deploy(CONTRACT, pool, WINDOW, cap, MIN_STAKE)


@pytest.mark.parametrize("stake", [0, 10 ** 17 - 1, MAX_CLAIM + 1])
def test_min_stake_bounds(direct_deploy, pool, stake):
    with pytest.raises(Exception, match="min stake out of range"):
        direct_deploy(CONTRACT, pool, WINDOW, MAX_CLAIM, stake)


def test_contract_address_matches_deployment_hash(direct_vm, contract):
    expected = hashlib.sha256(str(Path(CONTRACT).resolve()).encode()).digest()[:20]
    assert bytes(direct_vm._contract_address) == expected
