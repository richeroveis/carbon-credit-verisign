# Adversarial security review

Reviewed file: `contracts/carbon_credit_verisign.py`
Method: state machine walk, liveness of every locked value, race ordering,
accounting invariants, input validation, and consensus safety. Every finding
below points at a test that proves the fix, or states plainly that it is open.

## 1. State machine and authority

| Status | Who may act | Method | Next | Funds |
|---|---|---|---|---|
| `PENDING_AUDIT` | anyone | `verify_offset_claim_live` before the deadline | `PROVISIONAL_HOLD`, `CONTESTED` | stake locked |
| `PENDING_AUDIT` | anyone | `resolve_expired_claim` at or after the deadline | `EXPIRED_UNVERIFIED` | stake returned |
| `PROVISIONAL_HOLD` | claim owner | `submit_evidence` before the deadline | `EVIDENCE_SUBMITTED` | stake locked, escrow held |
| `PROVISIONAL_HOLD` | anyone | `resolve_expired_claim` at or after the deadline | `LAPSED_UNVERIFIED` | stake returned, escrow burned |
| `CONTESTED` | claim owner | `submit_evidence` before the deadline | `EVIDENCE_SUBMITTED` | stake locked |
| `CONTESTED` | anyone | `resolve_expired_claim` at or after the deadline | `REVOKED_FRAUD` | stake to the pool |
| `EVIDENCE_SUBMITTED` | anyone | `verify_offset_claim_live` before the deadline | `CREDITS_ISSUED`, `CONTESTED`, `REVOKED_FRAUD` | on issue: credits released, stake returned |
| `EVIDENCE_SUBMITTED` | anyone | `resolve_expired_claim` at or after the deadline | `REVOKED_FRAUD` or `LAPSED_UNVERIFIED` | as before |
| terminal statuses | none | none | none | settled |

There is exactly one authority comparison in the contract,
`gl.message.sender_address != claim.owner`, and it is per claim. There is no
owner field, no admin method, no pause, no upgrade and no sweep. Proven by
`test_only_one_sender_authority_check_exists`, `test_no_admin_surface_in_the_write_abi`
and `test_parameters_are_written_only_in_the_constructor`.

## 2. Liveness of locked value

| Locked value | Exit 1 | Exit 2 | Stuck if |
|---|---|---|---|
| claim stake in `PENDING_AUDIT` | anyone audits | anyone resolves after the deadline | nobody calls either; both are permissionless and cheap |
| claim stake in `PROVISIONAL_HOLD` | owner files evidence and passes the second audit | anyone resolves after the deadline | nobody calls either; both are permissionless |
| claim stake in `CONTESTED` | owner files a rebuttal and passes the audit | anyone resolves after the deadline | nobody calls either |
| escrow in `PROVISIONAL_HOLD` or `EVIDENCE_SUBMITTED` | released on a positive second audit | burned at the deadline | nobody calls either |
| free stake | owner calls `withdraw_stake` at any time | not applicable | the owner never calls it |

No status requires the cooperation of a single party without a deadline and a
permissionless recovery path. `test_resolve_*` and `test_deadline_race_is_exclusive`
cover the boundary.

## 3. Race conditions

* `submit_evidence` and `verify_offset_claim_live` require `now < deadline`.
  `resolve_expired_claim` requires `now >= deadline`. The two sides cannot both
  succeed on the same timestamp. `test_deadline_race_is_exclusive` and
  `test_audit_rejects_after_deadline` prove both sides, including the exact
  boundary.
* Two audits on the same claim: the second sees a status outside
  `PENDING_AUDIT` or `EVIDENCE_SUBMITTED` and reverts.
  `test_audit_rejects_wrong_status`.
* Two resolutions on the same claim: the second sees a terminal status and
  reverts. `test_resolve_twice_rejected`, `test_pool_never_receives_more_than_the_stake`.
* Withdraw against a new claim: free stake and claim stake are separate
  buckets. `test_other_owner_is_isolated`, `test_deposit_and_withdraw_free_stake`.
* Double credit: issuance only happens from `EVIDENCE_SUBMITTED` and sets a
  terminal status in the same call. `test_second_positive_audit_issues_credits`.

## 4. Accounting invariants

```
credit_supply_atto == sum(credit_balances) + sum(open claim escrow)
locked_stake_total == sum(stake_atto of open claims)
```

`locked_stake_total` is a running storage counter, and the right hand side is
recomputed by iterating every claim, so the two terms are independent. The
invariant is exposed by `accounting_invariant()` and asserted after every step
of `test_mixed_scenario_keeps_the_invariant_after_every_step` and of every
gate test through `assert_invariant`.

Checked separately: zero amounts, overpayment (`test_withdraw_more_than_free_rejected`),
underpayment (`test_stake_below_coverage_rejected`), rounding (the score is on a
five value ladder, so the issued amount is one of four exact ratios),
double slash (`test_pool_never_receives_more_than_the_stake`), burn on lapse
(`test_resolve_lapsed_hold_burns_credits_and_returns_stake`).

## 5. Input validation

| Input | Floor | Ceiling | Format |
|---|---|---|---|
| verification window | 3600 s | 90 days | integer |
| max claim | 1 credit | 1e27 atto | integer |
| min stake | 0.1 GEN | max claim | integer |
| claimed tonnage | 1 credit | max claim | integer |
| stake | min stake | none, it is the caller's own value | attached value |
| claim id | 3 chars | 64 chars | `[A-Za-z0-9_-]` |
| project name | 3 chars | 128 chars | no `<>{}`, no control characters |
| evidence url | 12 chars | 512 chars | `https`, host with a dot, no whitespace, no `<>{}` |
| evidence hash | 32 bytes | 32 bytes | lowercase hex |
| transfer amount | 1 atto | the caller's balance | integer |
| transfer recipient | not zero | not this contract | address |

Every window has both a floor and a ceiling, and every value is rejected, not
clamped. `test_window_bounds`, `test_max_claim_bounds`, `test_min_stake_bounds`,
`test_claim_id_validation`, `test_project_name_validation`, `test_url_validation`,
`test_evidence_hash_validation`, `test_requested_amount_bounds`.

## 6. Consensus safety

* The tier is a fixed set of three strings, derived from the metric score by a
  pure function. The score is on a five value ladder, so the issued amount is
  derived deterministically from five anchors and never from a free form number.
* `prompt_comparative` is configured with a principle that requires an exact
  match on tier and on metric score. A difference of one point fails.
* Each validator reruns the same fetch and the same prompt. Nothing the owner
  submits is trusted, including the on chain evidence hash.
* Fail closed: an empty or unreachable page, a non object answer, an unknown
  tier, a score off the ladder, or a missing score all revert the transaction.
  There is no default score and no default tier.
  `test_malformed_verdicts_fail_closed` covers twelve shapes, and
  `test_tier_only_answer_fails_closed` pins the tier-only case.
* A self reported tier can never override the score: the tier stored on chain is
  recomputed from the score. `test_score_wins_over_a_self_reported_tier`.
* Prompt injection: the fetched page and the operator supplied project name are
  both placed inside marked untrusted blocks, and the prompt states that neither
  block is an instruction. `test_prompt_injection_in_page_cannot_set_the_verdict`
  and `test_operator_text_is_framed_as_untrusted_data` cover the wording. This
  is mitigation, not a proof of immunity.
* Direct mode starts from local mocks only. Validator agreement is proven by the
  live studionet run in `docs/evidence.md`.

## Findings

| # | Finding | Status |
|---|---|---|
| 1 | The stake invariant compared a value with itself, so it could never fail | FIXED, added a `locked_stake_total` storage counter. `test_mixed_scenario_keeps_the_invariant_after_every_step` |
| 2 | The project name was placed in the audit prompt as trusted metadata, a free injection channel for a claim owner | FIXED, name and url are inside the untrusted claim block, `<>{}` and control characters rejected. `test_operator_text_is_framed_as_untrusted_data`, `test_project_name_validation` |
| 3 | A nondeterministic audit transaction can report an intermediate `execution_result` in its receipt while the accepted state records the agreed outcome | OPEN, documented. The live scenario asserts on the latest accepted contract state and the evidence collector re-reads the same claims at final state. No contract change can fix a receipt reporting detail |
| 4 | If the community pool address cannot accept native value, the queued slash transfer fails and the slashed value stays in the contract with no recovery method | OPEN, operational assumption. Deploy with an externally owned account or a receiver contract as the pool. The live run uses an EOA |
| 5 | `accounting_invariant()` is O(number of claims) | OPEN, intended for off chain audit views, never called by a write path |
| 6 | Prompt injection resistance is argued, not proven | OPEN, stated as a limitation in the README |
| 7 | `transfer_credits` to the caller's own address is a no-op that still writes | OPEN, harmless, no value created or destroyed |
| 8 | A tier-only LLM answer (no `metric_score`) was mapped to the middle of its band, which minted 50 percent of the claim on a malformed answer | FIXED, `_parse_verdict` now rejects a missing score and there is no default. `test_tier_only_answer_fails_closed`, `test_malformed_verdicts_fail_closed` |

The contract is not described as bug free. Items 3 to 7 are open with reasons.
