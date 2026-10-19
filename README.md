# CarbonCreditVerisign

An Intelligent Contract for a carbon offset registry that issues credits only
after a live, validator agreed audit of external evidence, and that slashes
fraudulent claims into a community pool.

The contract lives in one file:

- [contracts/carbon_credit_verisign.py](contracts/carbon_credit_verisign.py)
- runner pinned on line 1 to `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
- classic SDK: `from genlayer import *`, `gl.Contract`, `@allow_storage`, `@dataclass`, `gl.message.sender_address`

## What the contract owns, and what it does not

| Layer | Owns |
|---|---|
| GenLayer contract | the credit ledger, the collateral vault, the audit verdict, the deadline gates, the slash, and every state transition |
| External sources | the raw registry pages and reports. They are fetched independently by the leader and by each validator, and are always treated as untrusted text |
| Frontend or indexer | display, search, cached previews. It has no authority over state |

There is no owner, no admin, no pause switch, no upgrade hook and no rescue
function. The constructor arguments (community pool address, verification
window, maximum claim size, minimum stake) are written once and never change.

## Lifecycle

Eight statuses, eight write methods. Ownership checks are per claim, never global.

| Status | Who may act | Method | Next status | Funds |
|---|---|---|---|---|
| `PENDING_AUDIT` | anyone | `verify_offset_claim_live` before the deadline | `PROVISIONAL_HOLD` or `CONTESTED` | stake locked, no credits |
| `PENDING_AUDIT` | anyone | `resolve_expired_claim` at or after the deadline | `EXPIRED_UNVERIFIED` | stake returned, no credits |
| `PROVISIONAL_HOLD` | claim owner | `submit_evidence` before the deadline | `EVIDENCE_SUBMITTED` | stake locked, credits in escrow |
| `PROVISIONAL_HOLD` | anyone | `resolve_expired_claim` at or after the deadline | `LAPSED_UNVERIFIED` | stake returned, escrow burned |
| `CONTESTED` | claim owner | `submit_evidence` before the deadline | `EVIDENCE_SUBMITTED` | stake locked, no credits |
| `CONTESTED` | anyone | `resolve_expired_claim` at or after the deadline | `REVOKED_FRAUD` | stake transferred to the community pool |
| `EVIDENCE_SUBMITTED` | anyone | `verify_offset_claim_live` before the deadline | `CREDITS_ISSUED` or `CONTESTED` or `REVOKED_FRAUD` | on issue: credits released, stake returned |
| `EVIDENCE_SUBMITTED` | anyone | `resolve_expired_claim` at or after the deadline | `REVOKED_FRAUD` after a fraud finding, otherwise `LAPSED_UNVERIFIED` | as above |
| `CREDITS_ISSUED`, `LAPSED_UNVERIFIED`, `EXPIRED_UNVERIFIED`, `REVOKED_FRAUD` | none | none | terminal | settled |

Every status in which value is locked has a permissionless exit at a deadline.
The guards are mutually exclusive by status and by time comparison:
`submit_evidence` and `verify_offset_claim_live` require `now < deadline`,
`resolve_expired_claim` requires `now >= deadline`, so no pair can both succeed
on the same block.

## The live audit

`verify_offset_claim_live(claim_id)`:

1. renders the primary evidence URL with `gl.nondet.web.render(..., mode="text")`,
   and the rebuttal URL as well once the owner has filed one;
2. caps each page at 12000 characters, rejects an empty page as `[EXTERNAL]`;
3. sends a fixed rubric to `gl.nondet.exec_prompt(..., response_format="json")`;
4. returns through `gl.eq_principle.prompt_comparative` with a principle that
   demands an exact match on `tier` and `metric_score`.

The answer surface is deliberately small:

| Tier | Allowed `metric_score` | Credits issued |
|---|---|---|
| `VERIFIED_FULL` | 100 | `requested * 100 / 100` |
| `PARTIAL_OFFSET` | 25, 50, 75 | `requested * score / 100` |
| `REJECTED_FRAUD` | 0 | none |

The score is on a five value ladder (0, 25, 50, 75, 100) and the tier is derived
from the score by a pure function, so validators that agree on the score cannot
disagree on the tier. The issued amount is therefore one of four exact ratios of
the requested tonnage. There is no numeric tolerance anywhere: the comparative
principle says that a difference of one point means the answers are not
equivalent.

## Fail closed rules

- A malformed, missing or inconsistent answer raises `[LLM_ERROR]` and reverts
  the transaction. Nothing is stored and nothing is issued.
- An empty or unreachable page raises `[EXTERNAL]` or `[TRANSIENT]` and reverts.
- Credits are never minted straight to a balance. The first positive audit mints
  into escrow and opens an evidence window.
- Escrow is released only after the owner files evidence and a second live audit
  still returns a positive tier. If the owner never files, or the re-audit never
  runs, the deadline burns the escrow and returns the stake. No credits.
- A fraud finding opens a rebuttal window. A second fraud finding, or a
  rebuttal that was never audited by the deadline, revokes the claim and
  transfers the stake to the community pool.
- Every fetch is done by each validator independently; nothing the owner
  submits is trusted, including the on chain evidence hash, which is an audit
  trail commitment and not a trust anchor.

## Accounting invariants

Exposed by `accounting_invariant()` and asserted after every step of the direct
test scenarios:

```
credit_supply_atto == sum(credit_balances) + sum(open claim escrow)
locked_stake_total == sum(stake_atto of open claims)
```

Native value only leaves the contract through `emit_transfer` to the caller's
own free stake or to the immutable community pool address.

## Repository layout

```
contracts/carbon_credit_verisign.py   the contract
tests/direct/                         direct mode tests, mocks only
tests/integration/                    live studionet scenario
docs/evidence.md                      deploy and live transaction evidence
docs/security-review.md               adversarial review notes
scripts/                              deploy and evidence helpers
```

## Tooling

```bash
pip install -r requirements.txt
genvm-lint check contracts/carbon_credit_verisign.py
pytest tests/direct -v
gltest tests/integration -v -s --network studionet
```

`tests/direct` runs the leader path in process and does not exercise validator
agreement. Validator agreement, real web rendering and the real model are
covered only by `tests/integration` on studionet.

## Deployment

| Field | Value |
|---|---|
| Network | StudioNet, chain id 61999 |
| Contract address | see [docs/evidence.md](docs/evidence.md) |
| Deploy transaction | see [docs/evidence.md](docs/evidence.md) |
| Contract sha256 | see [docs/evidence.md](docs/evidence.md) |
| Commit | see [docs/evidence.md](docs/evidence.md) |

## Honest limitations

- The audit verdict depends on external pages and on the validators' model. The
  live run records the tier that actually came back; it is not scripted.
- Prompt injection is mitigated by framing the fetched text as data and by a
  strict parser that only accepts five score anchors and a derived tier. A direct
  test feeds an injection payload and checks that the payload cannot set the
  verdict by itself. That is not a proof of injection immunity.
- The one hour deadline floor is a production floor. The live run exercises the
  early side of the boundary (an early resolution must revert); the late side is
  exercised in direct mode with time travel.
