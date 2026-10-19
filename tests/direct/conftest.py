"""Shared fixtures for direct mode tests.

Direct mode runs the leader path only. Validator agreement is proven separately
by the live studionet scenario recorded in docs/evidence.md.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile

import pytest

CONTRACT = "contracts/carbon_credit_verisign.py"
WINDOW = 3600
MAX_CLAIM = 10 ** 24
MIN_STAKE = 10 ** 18
ONE_CREDIT = 10 ** 18


def _install_windows_stdin_shim() -> None:
    """Keep the injected message file alive for the process lifetime.

    The upstream direct loader unlinks the temporary stdin file right after
    duplicating it onto fd 0. Windows refuses to delete a file that is still
    open, so the whole suite errors out before the first deploy. This shim is
    test harness plumbing only, it does not touch the contract or the
    assertions. Files are removed when the test session ends.
    """
    if os.name != "nt":
        return

    from gltest.direct import loader as direct_loader

    if getattr(direct_loader, "_windows_stdin_shim", False):
        return

    def _inject(vm) -> None:
        from genlayer.py import calldata
        from genlayer.py.types import Address

        sender = vm.sender
        if isinstance(sender, bytes):
            sender = Address(sender)
        contract_addr = vm._contract_address
        if isinstance(contract_addr, bytes):
            contract_addr = Address(contract_addr)
        origin = vm.origin
        if isinstance(origin, bytes):
            origin = Address(origin)

        payload = {
            "contract_address": contract_addr,
            "sender_address": sender,
            "origin_address": origin,
            "stack": [],
            "value": vm._value,
            "datetime": vm._datetime,
            "is_init": False,
            "chain_id": vm._chain_id,
            "entry_kind": 0,
            "entry_data": b"",
            "entry_stage_data": None,
        }
        encoded = calldata.encode(payload)
        fd, path = tempfile.mkstemp()
        os.write(fd, encoded)
        os.lseek(fd, 0, os.SEEK_SET)
        vm._original_stdin_fd = os.dup(0)
        os.dup2(fd, 0)
        os.close(fd)
        pending = getattr(vm, "_pending_stdin_files", None)
        if pending is None:
            pending = []
            vm._pending_stdin_files = pending
        pending.append(path)
        _PENDING_STDIN_FILES.append(path)

    direct_loader._inject_message_to_fd0 = _inject
    direct_loader._windows_stdin_shim = True


_PENDING_STDIN_FILES: list = []
_install_windows_stdin_shim()


def pytest_sessionfinish(session, exitstatus):
    for path in _PENDING_STDIN_FILES:
        try:
            os.unlink(path)
        except OSError:
            pass


def iso(epoch: int) -> str:
    stamp = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")


def warp_to(vm, epoch: int) -> None:
    vm.warp(iso(epoch))


def verdict(tier: str, score: int, summary: str = "audit summary", flags=None) -> str:
    return json.dumps(
        {
            "tier": tier,
            "metric_score": score,
            "summary": summary,
            "red_flags": flags if flags is not None else [],
        }
    )


def mock_site(vm, url_fragment: str, body: str, method: str = "GET") -> None:
    vm.mock_web(re.escape(url_fragment), {"status": 200, "body": body, "method": method})


def mock_audit(vm, claim_id: str, tier: str, score: int) -> None:
    vm.mock_llm(
        r"(?s).*claim_id: " + re.escape(claim_id) + r"\b.*",
        verdict(tier, score),
    )


def hexaddr(value) -> str:
    """Lowercase 0x prefixed hex for an Address, bytes or string address."""
    if hasattr(value, "as_bytes"):
        return "0x" + value.as_bytes.hex()
    if isinstance(value, str):
        text = value.lower()
        return text if text.startswith("0x") else "0x" + text
    return "0x" + bytes(value).hex()


@pytest.fixture
def direct_alice():
    return bytes([0xAA]) * 20


@pytest.fixture
def direct_bob():
    return bytes([0xBB]) * 20


@pytest.fixture
def direct_charlie():
    return bytes([0xCC]) * 20


@pytest.fixture
def direct_dave():
    return bytes([0xDD]) * 20


class TransferRecorder:
    """Captures emit_transfer messages so tests can assert on real transfers."""

    def __init__(self) -> None:
        self.transfers: list = []

    def install(self, vm) -> "TransferRecorder":
        def hook(call_vm, request):
            if isinstance(request, dict) and "PostMessage" in request:
                message = request["PostMessage"]
                self.transfers.append(
                    {
                        "to": hexaddr(message["address"]),
                        "value": int(message["value"]),
                        "on": message["on"],
                    }
                )
                return {"ok": None}
            return None

        vm._gl_call_hook = hook
        return self

    def total_for(self, address) -> int:
        target = hexaddr(address)
        return sum(item["value"] for item in self.transfers if item["to"] == target)

    def count_for(self, address) -> int:
        target = hexaddr(address)
        return len([item for item in self.transfers if item["to"] == target])


@pytest.fixture
def recorder(direct_vm) -> TransferRecorder:
    return TransferRecorder().install(direct_vm)


@pytest.fixture
def pool(direct_charlie):
    return direct_charlie


@pytest.fixture
def deploy(direct_deploy, pool):
    def _deploy(window: int = WINDOW, max_claim: int = MAX_CLAIM, min_stake: int = MIN_STAKE):
        return direct_deploy(CONTRACT, pool, window, max_claim, min_stake)

    return _deploy


@pytest.fixture
def contract(deploy):
    return deploy()


@pytest.fixture
def claim_setup(direct_vm, contract, recorder, direct_alice):
    """Register a funded claim owned by Alice and return (contract, claim_id)."""

    def _setup(
        claim_id: str = "amazon-reforest-1",
        requested: int = 100 * ONE_CREDIT,
        stake: int = 100 * ONE_CREDIT,
        url: str = "https://registry.example.org/projects/amazon-1",
    ):
        direct_vm.sender = direct_alice
        direct_vm.value = stake
        contract.register_claim(
            claim_id,
            "Amazon Reforestation One",
            url,
            requested,
            "a" * 64,
        )
        direct_vm.value = 0
        return contract, claim_id

    return _setup


def assert_invariant(contract) -> None:
    snapshot = contract.accounting_invariant()
    assert snapshot["ok"] is True, snapshot
    assert snapshot["supply_holds"] is True, snapshot
    assert snapshot["stake_holds"] is True, snapshot
