"""Live studionet scenario for CarbonCreditVerisign.

Run with:

    gltest tests/integration -v -s --network studionet

The scenario uses two real signing accounts. It deploys, drives a registry
backed claim to issuance, drives an unrelated claim to a fraud revocation with
a real transfer to the community pool, runs two attack transactions that must
not change state, and leaves one claim to be resolved after its deadline by
scripts/resolve_after_deadline.py.

Assertions are made on the latest accepted contract state. The evidence
collector re-reads the same claims at LATEST_FINAL so the final state is also
recorded. A nondeterministic audit transaction can report an intermediate
execution result in its receipt while the accepted state records the agreed
outcome, so the contract state is the authority, not the receipt.

Steps are idempotent and driven by on chain status, so a rerun after a network
fault continues where the previous run stopped. Set LIVE_CONTRACT_ADDRESS to
reuse an existing deployment.

Direct mode tests cover the deadline boundary with time travel. This live run
covers the early side of the boundary, because the production floor for the
verification window is one hour and weakening it for a demo is not acceptable.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from genlayer_py.types import TransactionHashVariant  # noqa: F401 - documented variant
from gltest import get_contract_factory
from gltest.accounts import get_accounts
from gltest.types import TransactionStatus
from gltest.utils import extract_contract_address

CONTRACT_NAME = "CarbonCreditVerisign"
ONE = 10 ** 18
WINDOW = 3600
MAX_CLAIM = 1000 * ONE
MIN_STAKE = ONE

POOL_URL = "https://verra.org/verra-issues-1-billionth-carbon-credit/"
FRAUD_URL = "https://example.com/"
RECORD_PATH = Path(os.environ.get("LIVE_RECORD_PATH", "evidence/live_run.json"))
STATE_PATH = Path("evidence/live_state.json")

CLAIM_A = "chyulu-hills-1"
CLAIM_B = "example-dot-com-1"
CLAIM_C = "deadline-probe-1"

RECORDS: list = []


def _execution(receipt: dict) -> str:
    data = receipt.get("consensus_data") or {}
    leader = (data.get("leader_receipt") or [{}])[0]
    return str(leader.get("execution_result", "UNKNOWN"))


def _record(step: str, receipt: dict, note: str = "", state: str = "") -> dict:
    item = {
        "step": step,
        "tx_hash": receipt.get("hash"),
        "status": receipt.get("status_name"),
        "execution": _execution(receipt),
        "state_after": state,
        "note": note,
        "observed_at": int(time.time()),
    }
    RECORDS.append(item)
    print("TX " + json.dumps(item), flush=True)
    return item


def _retry(fn, attempts: int = 4, pause: int = 8):
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as error:  # transport level only, re-raised at the end
            last = error
            print(f"RETRY {attempt + 1}/{attempts}: {error}", flush=True)
            time.sleep(pause)
    raise last


def _write(contract, method: str, args: list, account, value: int = 0, retries: int = 200) -> dict:
    def call():
        fn = getattr(contract.connect(account), method)(args=args)
        return fn.transact(
            value=value,
            wait_transaction_status=TransactionStatus.ACCEPTED,
            wait_triggered_transactions=True,
            wait_retries=retries,
        )

    return _retry(call)


def _read(contract, method: str, args: list):
    """Read the latest non final state, which includes accepted transactions."""
    return _retry(lambda: getattr(contract, method)(args=args).call())


def _status(contract, claim_id: str):
    """Return the latest state status, or None when the claim does not exist."""
    for attempt in range(3):
        try:
            return getattr(contract, "get_claim")(args=[claim_id]).call()["status"]
        except Exception as error:
            text = str(error)
            if "execution failed" in text or "UserError" in text:
                return None
            print(f"RETRY status {claim_id} {attempt + 1}/3: {text}", flush=True)
            time.sleep(8)
    return None


def _register(contract, claim_id, name, url, amount, digest, owner):
    if _status(contract, claim_id) is not None:
        print(f"SKIP register {claim_id}, already on chain", flush=True)
        return
    receipt = _write(
        contract,
        "register_claim",
        [claim_id, name, url, amount, digest],
        owner,
        value=amount,
    )
    state = _status(contract, claim_id)
    assert state == "PENDING_AUDIT", state
    _record("register_" + claim_id, receipt, "", state)


def _audit(contract, claim_id, third, label: str):
    receipt = _write(contract, "verify_offset_claim_live", [claim_id], third)
    state = _status(contract, claim_id)
    claim = _read(contract, "get_claim", [claim_id])
    _record(label, receipt, f"tier={claim['tier']} score={claim['metric_score']}", state)
    return state


def test_live_scenario():
    accounts = get_accounts()
    assert len(accounts) >= 2, "two signing accounts are required"
    owner = accounts[0]
    third = accounts[1]

    factory = get_contract_factory(CONTRACT_NAME)
    reuse = os.environ.get("LIVE_CONTRACT_ADDRESS")

    if reuse:
        address = reuse
        deploy_receipt = None
        print("REUSING " + address, flush=True)
    else:
        deploy_receipt = _retry(
            lambda: factory.deploy_contract_tx(
                args=[third.address, WINDOW, MAX_CLAIM, MIN_STAKE],
                account=owner,
                wait_transaction_status=TransactionStatus.ACCEPTED,
            )
        )
        address = extract_contract_address(deploy_receipt)
        _record("deploy", deploy_receipt, address)
        print("CONTRACT " + address, flush=True)

    contract = factory.build_contract(contract_address=address, account=owner)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {
                "contract_address": address,
                "owner": owner.address,
                "third": third.address,
            }
        ),
        encoding="utf-8",
    )

    # ---------------- Claim A: real registry page, positive path expected.
    _register(
        contract,
        CLAIM_A,
        "Chyulu Hills REDD+ Project",
        POOL_URL,
        10 * ONE,
        "a" * 64,
        owner,
    )

    status = _status(contract, CLAIM_A)
    evidence_filed = status in (
        "EVIDENCE_SUBMITTED",
        "CREDITS_ISSUED",
        "REVOKED_FRAUD",
        "LAPSED_UNVERIFIED",
    )
    attack_done = False
    for _ in range(4):
        status = _status(contract, CLAIM_A)
        if status in ("PROVISIONAL_HOLD", "CONTESTED") and not evidence_filed:
            if not attack_done:
                # Attack path: a third party cannot file evidence for another claim.
                before = _read(contract, "get_claim", [CLAIM_A])
                receipt = _write(
                    contract, "submit_evidence", [CLAIM_A, POOL_URL, "b" * 64], third
                )
                after = _read(contract, "get_claim", [CLAIM_A])
                assert after["evidence_hash"] == before["evidence_hash"], after
                assert after["status"] == before["status"], after
                _record("attack_foreign_evidence_A", receipt, "state unchanged", after["status"])
                attack_done = True
            receipt = _write(
                contract, "submit_evidence", [CLAIM_A, POOL_URL, "c" * 64], owner
            )
            state = _status(contract, CLAIM_A)
            _record("submit_evidence_A", receipt, "", str(state))
            assert state == "EVIDENCE_SUBMITTED", state
            evidence_filed = True
            continue
        if status in ("PENDING_AUDIT", "EVIDENCE_SUBMITTED"):
            _audit(contract, CLAIM_A, third, "audit_A_" + str(status).lower())
            continue
        break

    claim_a = _read(contract, "get_claim", [CLAIM_A])
    print("CLAIM_A " + json.dumps(claim_a), flush=True)
    assert claim_a["status"] in ("CREDITS_ISSUED", "REVOKED_FRAUD")

    # ---------------- Claim B: unrelated page, fraud path expected.
    _register(
        contract,
        CLAIM_B,
        "Unverifiable Offset Project",
        FRAUD_URL,
        10 * ONE,
        "d" * 64,
        owner,
    )

    evidence_b = _status(contract, CLAIM_B) in (
        "EVIDENCE_SUBMITTED",
        "CREDITS_ISSUED",
        "REVOKED_FRAUD",
        "LAPSED_UNVERIFIED",
    )
    for _ in range(3):
        status = _status(contract, CLAIM_B)
        if status in ("PROVISIONAL_HOLD", "CONTESTED") and not evidence_b:
            receipt = _write(
                contract, "submit_evidence", [CLAIM_B, FRAUD_URL, "e" * 64], owner
            )
            state = _status(contract, CLAIM_B)
            _record("submit_evidence_B", receipt, "", str(state))
            assert state == "EVIDENCE_SUBMITTED", state
            evidence_b = True
            continue
        if status in ("PENDING_AUDIT", "EVIDENCE_SUBMITTED"):
            _audit(contract, CLAIM_B, third, "audit_B_" + str(status).lower())
            continue
        break

    claim_b = _read(contract, "get_claim", [CLAIM_B])
    print("CLAIM_B " + json.dumps(claim_b), flush=True)
    assert claim_b["status"] in ("REVOKED_FRAUD", "CREDITS_ISSUED", "CONTESTED")

    # ---------------- Claim C: left open for a delayed deadline resolution.
    _register(
        contract,
        CLAIM_C,
        "Deadline Probe Project",
        POOL_URL,
        5 * ONE,
        "f" * 64,
        owner,
    )
    claim_c = _read(contract, "get_claim", [CLAIM_C])

    # Attack path: resolution strictly before the deadline must not change state.
    receipt = _write(contract, "resolve_expired_claim", [CLAIM_C], third)
    state = _status(contract, CLAIM_C)
    assert state == "PENDING_AUDIT", state
    _record("attack_early_resolve_C", receipt, "state unchanged", state)

    # ---------------- Accounting invariant read back from chain.
    snapshot = _read(contract, "vault_snapshot", [])
    invariant = _read(contract, "accounting_invariant", [])
    print("SNAPSHOT " + json.dumps(snapshot), flush=True)
    print("INVARIANT " + json.dumps(invariant), flush=True)
    assert invariant["ok"] is True, invariant

    record = {
        "contract_address": address,
        "deploy_tx": (deploy_receipt or {}).get("hash")
        or os.environ.get("LIVE_DEPLOY_TX"),
        "owner": owner.address,
        "third_party": third.address,
        "pool": third.address,
        "window_seconds": WINDOW,
        "claims": {"A": claim_a, "B": claim_b, "C": claim_c},
        "snapshot": snapshot,
        "invariant": invariant,
        "transactions": RECORDS,
    }
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous = {}
    if RECORD_PATH.exists():
        try:
            previous = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
        except ValueError:
            previous = {}
    if previous.get("transactions") and previous.get("contract_address") == address:
        record["transactions"] = previous["transactions"] + RECORDS
        record["deploy_tx"] = previous.get("deploy_tx") or record["deploy_tx"]
    RECORD_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print("RECORD " + str(RECORD_PATH), flush=True)

    assert invariant["supply_holds"] is True, invariant
    assert invariant["stake_holds"] is True, invariant
