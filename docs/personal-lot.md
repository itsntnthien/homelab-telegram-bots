# Personal lot estimates

Each authorized recipient configures their own account in a private chat, entirely through buttons:

1. Open **💰 Vốn & rủi ro** from the home keyboard.
2. Choose **💵 Tài khoản USD** or **🪙 Tài khoản Cent**.
3. Press **💰 Nhập vốn**, then type only the numeric balance.
4. Press **📉 Rủi ro theo %** or **💸 Rủi ro cố định**, then type only the numeric risk.
5. Use **👁 Xem cài đặt** to review, **↩️ Hủy / Quay lại** to cancel input, or **🏠 Mở bảng điều khiển** to return home. Deletion has a separate confirmation button.

The current home keyboard can be reopened using its existing home button. No slash command is needed for this flow. Legacy `/von`, `/ruiro` and `/lot` handlers remain compatible but are no longer advertised; legacy capital commands retain their original USD meaning.

## Account units

| Choice | Balance and fixed risk input | Output volume |
| --- | --- | --- |
| USD | USD | Standard XAUUSD lots |
| Exness Standard Cent | USC, where 100 USC = 1 USD | XAUUSDc cent lots |

One active account type is stored per recipient. Existing users remain USD. Switching type preserves the real monetary amount: 1,000 USD displays as 100,000 USC, and fixed risk is converted the same way. It changes the volume unit, not the money. Enter the new balance when switching to a different actual account. Percentage risk is unchanged. Balances are maintained manually; simulated P/L never adjusts them.

### Cent reference model

Exness documents [Standard Cent](https://get.exness.help/hc/en-us/articles/17537782786588-Standard-Cent-account) as using cent balances, a cent lot equal to 0.01 standard lot, and XAUUSDc. Its published account limits are 0.01–200 cent lots. The [gold specification](https://get.exness.help/hc/en-us/articles/17854173039388-Commodities) gives 100 troy ounces per standard XAUUSD lot. Checked 2026-11-23.

The versioned Cent model derives **1 troy ounce per cent lot** from those two specifications and uses a **0.01 cent-lot sizing grid**. That grid is a model assumption, not a verified specification fetched from the recipient's trading server. The account panel and each Cent signal explicitly show it. Account-specific platform specifications take precedence; this is not a universal conversion for other brokers or cent currencies.

The collector supplies a checked XAUUSD bid/ask, **not a separate XAUUSDc quote**. Cent results therefore remain reference estimates: actual Cent spread, execution price, account-specific commission, margin, and server volume constraints are not verified. The bot neither connects to the recipient's account nor places an order. Cent outputs label both cent lots and their equivalent standard lots and tell the recipient to check the XAUUSDc price/specification before execution.

## Calculation and delivery

Canonical capital and fixed risk are stored in USD using decimal strings. USC inputs are divided by 100 exactly; percentage inputs are never divided by 100 twice.

```text
budget_usd = capital_usd × risk_percent / 100   (or fixed risk in USD)
risk_per_lot = (direction × (checked_price − SL)
                + 2 × modeled_slippage + modeled_commission) × contract_size
lots = floor_to_volume_step(min(budget_usd / risk_per_lot, volume_max))
```

USD uses validated collector contract/min/max/step metadata. Cent uses the explicit versioned reference model above, and floors on the cent-lot grid before calculating the standard-lot equivalent. A budget below the minimum lot's modeled risk produces an explanation instead of rounding up. Unknown account types, incomplete settings and invalid specifications do not produce guessed lots.

The checked bid/ask already includes its source spread; it is not added twice. Fees and slippage beyond the model, market gaps and the actual execution price can exceed the displayed estimate. Equal real capital and risk have equal modeled loss budgets across account types, though their different volume grids can produce different rounded exposure.

Capital, risk, account type and the Cent model (if selected) are snapshotted per recipient when a new entry is queued. Delivery calculates volume from the freshly checked quote and signal SL. Later settings changes do not rewrite queued or historical notifications. Old queued profiles with no account type retain USD behavior. Existing notifications with no sizing metadata retain their old behavior.

## Persistence and migration

`user_risk_settings` retains its original schema and canonical USD values. The additive `user_risk_ui` table stores the selected account and pending numeric input per authenticated private chat. This input state survives restarts. Invalid input leaves settings unchanged; navigation cancels pending input. Only the authorized matching sender can change a private profile.

Trade/event/outbox tables and engine snapshots retain their structure. The final sizing result is recorded in `check_json`. Neither settings nor balances are exported to GitHub.

For rollback, preserve the latest database and poll cursor. A pre-Cent release can read the canonical USD amounts but cannot honor Cent lot semantics. **Do not roll back to pre-Cent code while Cent profiles or queued Cent notifications exist**; use a forward fix or a compatible release. Never restore an older runtime database or drop user settings as part of code rollback.
