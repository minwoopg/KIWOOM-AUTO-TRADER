# 2026-09-07 safety review

Base: `a5106dfba031ab1440d0ba9c4f0238fffd1bdfa7` (`main`).
These changes have only been exercised offline. They do not certify live trading
readiness or profitability. Strategy thresholds and the configured paper mode
are unchanged.

**2026-09-07 owner review (민우) — one item reverted before adoption.** The
original audit patch also changed the global `consecutive_losses` counter to
increment on every completed losing exit, including repeats of the same
symbol. That reverses a deliberate 2026-06-15 decision (one symbol's repeated
bad luck must not halt the whole account; only a *new* symbol's first loss
counts toward the account-wide `max_consecutive_losses` halt, while repeats of
one symbol are capped separately via `symbol_loss_count_today`). The owner
re-confirmed the 2026-06-15 policy and asked to keep it. Only the other half of
this fix — a process restart is not a new trading day, so `consecutive_losses`
and other daily counters must not reset mid-day on restart — was kept. See
`domain/service/trading_service.py::_apply_deferred_sell_side_effects` (the
`if cnt == 1:` guard is intentional, not a leftover) and
`test_review_safety_regressions.py::test_symbol_loss_count_ignores_duplicate_observation_global_counter_kept_2026_06_15_policy`.

## Behavior changes

| Area | Behavior | Tradeoff |
| --- | --- | --- |
| Holdings | Every positive account holding is processed, even outside entry targets or after entry exclusion. | More price reads if the account has additional holdings. |
| Account uncertainty | No new entry while another order is pending, orphaned or in ERROR. A failed live balance read blocks BUY. | Entries are serialized until reconciliation; signals may be missed. |
| Price cache | An expired/future cache timestamp cannot size a new BUY. | A price endpoint outage can suppress entries even with fresh minute bars. |
| Pending balance | Unresolved orders bypass the account's normal 180-second cache. | More account reads; verify rate limits in a controlled paper session. |
| Closing window | Outstanding orders continue read-only reconciliation outside the order window. | Does not submit or retry liquidation orders after the window. |
| Restart | An order intent is saved before submission. Unresolved intents/journal records restore ERROR blocks. | Recovery is conservative and requires broker order and holding verification. |
| State file | Atomic replace preserves the last valid file if writing fails. | Requires writable storage; failed intent persistence prevents submission. |
| Duplicate process | One application per state-file lock on the same machine. | Does not coordinate other machines, paths or external trading software. |
| Daily limits | A persisted KST date controls rollover (process restart is not a new trading day). The global consecutive-loss counter still increments only on a symbol's first daily loss (2026-06-15 policy, reaffirmed 2026-09-07) — repeats of one symbol are capped separately by `symbol_loss_count_today`. | Undated legacy state preserves limits conservatively on first load. |
| Broker responses | Malformed order success responses remain ambiguous; incomplete holdings pages fail visibly. Negative available cash becomes zero buying power while holdings remain available. | Unknown response shapes can stop automated processing. |
| Mock broker | Requested partial sales retain residual holdings; oversells reject; filled orders support status lookup. | Still immediate-fill only; asynchronous tests use `PendingBroker`. |

## Recovery limits

`STARTUP_ORDER_RECOVERY` means that the previous submission outcome is unknown.
Startup balances alone do not establish that an old order is terminal. Restored
ERROR blocks same-symbol BUY and SELL; uncertainty also blocks new BUY elsewhere.
An unrelated, reconciled holding can still exit.

The existing `commands/ack_error_<symbol>.json` operator mechanism is retained.
Before using it, reconcile the original order, any cancellation/amendment chain,
all fills, and the current holding in the broker. A zero balance alone is
insufficient. Also reconcile daily entry/loss counters and the entry timestamp;
pending side-effect contexts and execution costs are not automatically replayed.
Never clear uncertainty merely because time elapsed or a process restarted.

A corrupt/unreadable journal prevents all automatic submissions because affected
symbols cannot be identified reliably. This requires manual account review.
The patch does not implement automatic cancellation, partial-order recovery, an
account-wide distributed lock, or an execution ledger with exactly-once costs.

## Important unresolved issues

- Daily realized P&L still consumes accepted order rows and signal prices.
  It does not represent actual execution P&L after fees and taxes.
- Consecutive-loss signs still use the exit signal price and account average,
  without actual execution costs. This is unrelated to, and not fixed by,
  the restart-day-reset fix above.
- First-fill time is the time the application observes a quantity increase,
  not a broker execution timestamp. Polling snapshots cannot uniquely attribute
  external/manual trades or out-of-order account observations.
- Partial/cancelled/amended order response shapes still need real broker fixtures.
  The existing status parser deliberately returns UNKNOWN for unproven shapes.
- Market gates now use KST and exclude weekends. Holidays/special sessions and
  full timestamp migration remain unsupported. Existing state/log timestamps
  still include naive host-local times; do not change host timezone mid-session.
- A price outage can still delay price-based exits; an account outage can stop
  a whole cycle. The patch cannot guarantee liquidation during API failures.
- Read-only reconciliation after 15:20 does not guarantee liquidation. Review
  the closing/cancellation policy separately before live use.

## Validation

Install the existing requirements, then run:

```sh
python -m pip install -r requirements.txt
python test_review_safety_regressions.py
python run_regression_tests.py
python -m compileall -q app domain infra utils tools/audit_trade_bundles.py test_review_safety_regressions.py
git diff --check
```

Review regressions: 25 tests pass. Official runner: 30 files, 28 pass, 2 fail.
Baseline before changes: 29 files, 26 pass, 3 fail. The broker read-only wiring
failure is repaired. Remaining baseline failures require absent data:

- `test_broker_order_status.py`: missing recorded order reconciliation JSONL.
- `test_replay_time_axis.py`: missing historical minute-bar dataset; ten
  data-dependent assertions fail.

The runner separately skips `test_legacy_fixture_structure.py` for an existing
missing fixture, and includes two copies of the stale-sell test. File counts
are not counts of independent market scenarios. No missing real data was
replaced with invented measurements. The Windows lock branch was not executed.

## Offline bundle analysis

```sh
python tools/audit_trade_bundles.py --input /path/to/extracted_bundles --output /path/to/audit_results
```

Input directories must have `bundle_YYYYMMDD/raw` and `metadata`. This tool
supports the exported simple BUY/SELL pairs and stops on overlapping lots,
unmatched quantities or unclosed pairs. Output filenames and fields label
order-price estimates explicitly. The 0.35%/0.90% scenarios mirror the existing
project analysis assumptions; they are not verified actual brokerage charges.
Quote horizons and shadow checkpoints are descriptive comparisons, not
executable backtests. Raw personal trading data is not included in this change.
