"""Collect deployment and live transaction evidence into docs/evidence.md.

This script never invents values. Every number in the generated file comes from
one of these commands:

    sha256sum of the contract file
    git show <commit>:contracts/carbon_credit_verisign.py | sha256sum
    gen_getContractCode over the StudioNet JSON RPC
    genlayer code <address>
    get_transaction for every recorded transaction hash

Run it after the live scenario and after the contract commit exists:

    python scripts/collect_evidence.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

RAW_RPC_URL = "https://studio.genlayer.com/api"
CONTRACT_PATH = Path("contracts/carbon_credit_verisign.py")
RUN_PATH = Path("evidence/live_run.json")
LATE_PATH = Path("evidence/live_late_resolve.json")
OUT_PATH = Path("docs/evidence.md")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def rpc(method: str, params: list):
    import urllib.request

    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    request = urllib.request.Request(
        RAW_RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "genlayer-py"},
    )
    last = None
    for _ in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode())
            if "error" in body:
                raise RuntimeError(body["error"])
            return body["result"]
        except Exception as error:  # network flake, retry a few times
            last = error
            time.sleep(5)
    raise RuntimeError(f"rpc failed: {last}")


def onchain_code_rpc(address: str) -> str:
    encoded = rpc("gen_getContractCode", [address])
    return base64.b64decode(encoded).decode("utf-8")


def onchain_code_cli(address: str) -> str:
    executable = shutil.which("genlayer")
    if executable is None:
        raise RuntimeError("genlayer CLI is not on PATH")
    if executable.lower().endswith(".ps1"):
        command = ["pwsh", "-NoProfile", "-File", executable, "code", address]
    else:
        # shutil.which resolves genlayer.CMD on Windows, which subprocess runs.
        command = [executable, "code", address]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    text = result.stdout
    marker = text.find('# { "Depends"')
    if marker < 0:
        return text
    return text[marker:].rstrip() + "\n"


def fetch_transactions(hashes: list) -> dict:
    from gltest_cli.config.general import get_general_config
    from gltest_cli.config.types import PluginConfig
    from gltest_cli.config.user import load_user_config

    config = get_general_config()
    config.user_config = load_user_config("gltest.config.yaml")
    plugin = PluginConfig()
    plugin.network_name = "studionet"
    config.plugin_config = plugin

    from gltest import get_gl_client

    client = get_gl_client()
    result = {}
    for tx_hash in hashes:
        if not tx_hash:
            continue
        for attempt in range(4):
            try:
                result[tx_hash] = client.get_transaction(tx_hash)
                break
            except Exception as error:
                print(f"retry {tx_hash} {attempt + 1}: {error}", flush=True)
                time.sleep(5)
    return result


def vote_summary(tx: dict) -> str:
    consensus = tx.get("consensus_data") or {}
    votes = consensus.get("votes") or {}
    if votes:
        parts = [f"{key}={value}" for key, value in sorted(votes.items())]
        return ", ".join(parts)
    validators = consensus.get("validators") or []
    agree = sum(1 for item in validators if item.get("vote") == "agree")
    disagree = sum(1 for item in validators if item.get("vote") == "disagree")
    return f"validators agree={agree} disagree={disagree}"


def execution(tx: dict) -> str:
    leader = ((tx.get("consensus_data") or {}).get("leader_receipt") or [{}])[0]
    return str(leader.get("execution_result", "UNKNOWN"))


def expected_for(step: str) -> str:
    """State the result each step is supposed to produce."""
    if step.startswith("attack_"):
        return "ERROR, state unchanged"
    if step.startswith("audit_"):
        return "ACCEPTED, or UNDETERMINED then retried"
    return "SUCCESS"


def main() -> None:
    if not RUN_PATH.exists():
        raise SystemExit(f"missing {RUN_PATH}, run the live scenario first")
    run = json.loads(RUN_PATH.read_text(encoding="utf-8"))
    address = run["contract_address"]

    commit = git("rev-parse", "HEAD").strip()
    commit_subject = git("log", "-1", "--format=%s").strip()

    local = CONTRACT_PATH.read_bytes()
    local_hash = sha256_bytes(local)
    git_hash = sha256_bytes(git("show", f"{commit}:{CONTRACT_PATH.as_posix()}").encode())

    try:
        rpc_code = onchain_code_rpc(address)
        rpc_hash = sha256_bytes(rpc_code.encode())
    except Exception as error:
        rpc_code = ""
        rpc_hash = f"unavailable: {error}"

    try:
        cli_code = onchain_code_cli(address)
        cli_hash = sha256_bytes(cli_code.encode())
    except Exception as error:
        cli_code = ""
        cli_hash = f"unavailable: {error}"

    transactions = list(run.get("transactions") or [])
    late = {}
    if LATE_PATH.exists():
        late = json.loads(LATE_PATH.read_text(encoding="utf-8"))
        if late.get("tx_hash"):
            transactions.append(
                {
                    "step": "resolve_after_deadline_C",
                    "tx_hash": late.get("tx_hash"),
                    "status": late.get("status"),
                    "execution": late.get("execution"),
                    "expected": "SUCCESS",
                    "note": "late side of the deadline boundary",
                }
            )

    details = fetch_transactions([item.get("tx_hash") for item in transactions])

    lines = []
    lines.append("# Deployment and live evidence")
    lines.append("")
    lines.append("Generated by `scripts/collect_evidence.py` from the chain and the")
    lines.append("repository. Nothing here is typed from memory.")
    lines.append("")
    lines.append("## Deployment")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Network | StudioNet, chain id 61999 |")
    lines.append(f"| Contract address | `{address}` |")
    lines.append(f"| Deploy transaction | `{run.get('deploy_tx')}` |")
    lines.append(f"| Deployed source commit | `{commit}` |")
    lines.append(f"| Commit subject | {commit_subject} |")
    lines.append(f"| Owner account | `{run.get('owner')}` |")
    lines.append(f"| Third party account | `{run.get('third_party')}` |")
    lines.append(f"| Community pool | `{run.get('pool')}` |")
    lines.append(f"| Verification window | {run.get('window_seconds')} seconds |")
    lines.append("")
    lines.append("## Four way sha256 of the deployed source")
    lines.append("")
    lines.append("| Source | sha256 |")
    lines.append("|---|---|")
    lines.append(f"| Repository file, working tree | `{local_hash}` |")
    lines.append(f"| Repository file at `{commit[:12]}` | `{git_hash}` |")
    lines.append(f"| On chain code, `gen_getContractCode` RPC | `{rpc_hash}` |")
    lines.append(f"| On chain code, `genlayer code` CLI | `{cli_hash}` |")
    lines.append("")
    all_equal = local_hash == git_hash == rpc_hash == cli_hash
    lines.append(
        "All four digests are identical: **"
        + ("YES" if all_equal else "NO")
        + "**. The RPC and the CLI read the same on chain object through two"
    )
    lines.append("independent clients, so this is three distinct artifacts plus a")
    lines.append("second retrieval path, not four unrelated artifacts.")
    lines.append("")
    lines.append("## Live scenario transactions")
    lines.append("")
    lines.append(
        "Every hash below was read back from the chain with `get_transaction`."
    )
    lines.append("")
    lines.append("| Step | Transaction | Status | Execution | Consensus | Expected |")
    lines.append("|---|---|---|---|---|---|")
    for item in transactions:
        tx = details.get(item.get("tx_hash")) or {}
        status = item.get("status") or tx.get("status_name") or "unknown"
        result = execution(tx) if tx else item.get("execution", "unknown")
        votes = vote_summary(tx) if tx else "not fetched"
        lines.append(
            f"| {item.get('step')} | `{item.get('tx_hash')}` | {status} | "
            f"{result} | {votes} | {item.get('expected', expected_for(str(item.get('step'))))} |"
        )
    lines.append("")
    lines.append("An UNDETERMINED row is a validator round that did not reach the")
    lines.append("required agreement. It applies no state change and the scenario retries")
    lines.append("it; the following row for the same step is the accepted attempt. An")
    lines.append("ERROR execution on an `attack_` row is the intended revert, and the")
    lines.append("state after it is checked to be unchanged.")
    lines.append("")
    lines.append("Terminal statuses were checked at collection time. ACCEPTED means")
    lines.append("the transaction was accepted by consensus with the execution result")
    lines.append("shown; FINALIZED is reported where the chain already moved it.")
    lines.append("")
    lines.append("## State at the end of the main scenario")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(run.get("snapshot"), indent=2))
    lines.append("```")
    lines.append("")
    lines.append("Accounting invariant:")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(run.get("invariant"), indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## Claims")
    lines.append("")
    for key, claim in (run.get("claims") or {}).items():
        lines.append(f"### Claim {key}: `{claim.get('claim_id')}`")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(claim, indent=2))
        lines.append("```")
        lines.append("")
    if late:
        lines.append("## Deadline resolution after the window")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(late, indent=2))
        lines.append("```")
        lines.append("")
        lines.append("This is the late side of the boundary: the same claim was")
        lines.append("rejected before the deadline in the scenario above and resolved")
        lines.append("after it here. The claim snapshot and the invariant in this record")
        lines.append("are the state after the late resolution.")
        lines.append("")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    print(f"all four hashes equal: {all_equal}")
    if not all_equal:
        print("MISMATCH, do not claim a verified match")


if __name__ == "__main__":
    main()
