"""Resolve a claim strictly after its deadline, live on studionet.

The production verification window floor is one hour, so this script is meant
to run in the background after the main scenario has registered its deadline
probe claim. It proves the late side of the deadline boundary on chain, the
main scenario proves the early side.

Usage:
    python scripts/resolve_after_deadline.py [claim_id]

The claim id defaults to deadline-probe-1. Results are written to
evidence/live_late_resolve.json.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from gltest_cli.config.general import get_general_config
from gltest_cli.config.types import PluginConfig
from gltest_cli.config.user import load_user_config

STATE = Path("evidence/live_state.json")
OUT = Path("evidence/live_late_resolve.json")
CLAIM = sys.argv[1] if len(sys.argv) > 1 else "deadline-probe-1"


def setup_config(network: str = "studionet") -> None:
    config = get_general_config()
    config.user_config = load_user_config("gltest.config.yaml")
    plugin = PluginConfig()
    plugin.network_name = network
    config.plugin_config = plugin


def _retry(fn, attempts: int = 5, pause: int = 8):
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as error:  # transport flake only, re-raised at the end
            last = error
            print(f"RETRY {attempt + 1}/{attempts}: {error}", flush=True)
            time.sleep(pause)
    raise last


def main() -> None:
    setup_config()
    from gltest import get_contract_factory
    from gltest.accounts import get_accounts
    from gltest.assertions import tx_execution_succeeded
    from gltest.types import TransactionStatus

    state = json.loads(STATE.read_text(encoding="utf-8"))
    address = state["contract_address"]
    accounts = get_accounts()
    keeper = accounts[1]

    factory = get_contract_factory("CarbonCreditVerisign")
    contract = _retry(
        lambda: factory.build_contract(contract_address=address, account=keeper),
        attempts=8,
        pause=15,
    )
    claim = _retry(lambda: contract.get_claim(args=[CLAIM]).call())
    if claim["status"] not in (
        "PENDING_AUDIT",
        "PROVISIONAL_HOLD",
        "EVIDENCE_SUBMITTED",
        "CONTESTED",
    ):
        record = {
            "claim": CLAIM,
            "address": address,
            "already_closed": True,
            "status": claim["status"],
            "claim_after": claim,
            "invariant": _retry(lambda: contract.accounting_invariant().call()),
            "resolved_at": int(time.time()),
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(json.dumps(record), flush=True)
        return
    deadline = int(claim["deadline"])
    now = int(time.time())
    wait = deadline - now + 30
    print(
        json.dumps(
            {
                "claim": CLAIM,
                "address": address,
                "deadline": deadline,
                "now": now,
                "wait_seconds": max(wait, 0),
            }
        ),
        flush=True,
    )
    if wait > 0:
        time.sleep(wait)

    tx = _retry(
        lambda: contract.connect(keeper).resolve_expired_claim(args=[CLAIM]).transact(
            wait_transaction_status=TransactionStatus.ACCEPTED
        )
    )
    execution = (
        ((tx.get("consensus_data") or {}).get("leader_receipt") or [{}])[0]
    ).get("execution_result")
    record = {
        "claim": CLAIM,
        "address": address,
        "tx_hash": tx.get("hash"),
        "status": tx.get("status_name"),
        "execution": execution,
        "succeeded": tx_execution_succeeded(tx),
        "resolved_at": int(time.time()),
        "claim_after": _retry(lambda: contract.get_claim(args=[CLAIM]).call()),
        "invariant": _retry(lambda: contract.accounting_invariant().call()),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
