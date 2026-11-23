# Personal lot estimates

Each authorized recipient configures their own capital and risk in a private chat:

```text
/von 1000
/ruiro 1%
```

Alternatively, `/ruiro 10usd` selects a fixed USD budget per signal. Values are examples, not defaults or recommended risk levels. `/lot` shows the current settings; `/lot off` deletes them for future entries. The home keyboard includes **💰 Vốn & rủi ro** with these instructions.

The supported scope is standard **XAUUSD quoted in USD**. Enter capital in USD, not the numeric balance of a cent account. This feature does not link to a broker account, size orders in other currencies or submit trades.

## What appears in a signal

The usual Entry/SL/TP signal remains shared. Each recipient additionally receives their own estimated lot size, capital, selected risk budget and modeled loss at SL. No profile is shared across recipients. If capital/risk is missing or instrument specifications cannot be validated, the message explains why no lot is supplied. If the budget is below the minimum lot's modeled risk, it reports that limit instead of rounding up.

Profiles and public instrument specifications are captured when a new entry notification is queued. The lot is calculated from the executable quote already validated immediately before delivery, together with the signal's SL. A later profile edit does not rewrite a queued signal or past messages. Changing capital never changes the simulated trade, the engine's configuration or other users' notifications.

## Calculation and limits

```text
budget = capital × risk_percent / 100   (or a fixed USD amount)
risk_per_lot = (direction × (checked_price − SL)
                + 2 × modeled_slippage + modeled_commission) × contract_size
lots = floor_to_volume_step(min(budget / risk_per_lot, broker_volume_max))
```

The source's bid/ask quote already includes spread; the calculation does not add it twice. Decimal arithmetic is used for budgeting and rounding. Contract size and min/max/step come from the validated collector metadata; no fallback lot specification is guessed.

The current engine's cost model is not a verification of each recipient's commission or margin. Output explicitly notes that account-specific fees and margin are not checked. Gaps, slippage beyond the modeled allowance and the user's actual execution price can exceed the displayed estimate. Capital remains manually maintained; simulated P/L never changes it.

## Storage and compatibility

`user_risk_settings` is an additive SQLite table keyed by the authenticated private chat ID. Commands validate the authorized chat and matching sender before reading or writing settings. Monetary values are stored as decimal strings. An incomplete profile is permitted until both capital and risk have been entered.

Existing trade/event/outbox tables and engine snapshots retain their structure. Existing notifications without sizing metadata keep their previous behavior. New notifications carry a per-recipient snapshot in `delivery_json`; the final calculation is recorded alongside the quote check in `check_json`.

Rollback means restoring the prior code while preserving the latest database. The old readers ignore the new table and metadata. Do not restore an older database or drop the new table as part of code rollback. Production rollout is a separate operational step; the repository does not automatically deploy these changes.
