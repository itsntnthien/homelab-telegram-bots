# Portfolio and CV notes

## Suggested project entry

**Homelab Telegram Automation — Python, SQLite, Node.js, Docker, Linux/systemd**

- Built and self-hosted two Telegram services on a Lenovo M720q: a market-signal publisher and a restricted Linux/Docker administration bot.
- Implemented persistent event deduplication, resumable processing and a SQLite notification outbox with expiry, retry and ambiguous-delivery handling.
- Added subsystem health checks, authenticated command dispatch, confirmation-gated operations and automated offline tests.

Add the public GitHub URL after publication. Use the verified test count from `validation.md` if you want a measurable bullet. Avoid claiming profitability, exactly-once delivery, zero downtime or TradingView parity.

## Interview walkthrough

1. Run `python scripts/demo.py` to show persistence, duplicate suppression and ambiguous send handling without credentials.
2. Explain the diagram in the root README and why the two bot processes have separate tokens and state.
3. Walk through the transaction and event-key design in `trades.py`, then the send-time checks in `delivery.py`.
4. Show the control bot's allowlist and chat-bound confirmation tests.
5. Discuss the source-capture incident: the signal process was alive while the feed was stale. A later check confirmed quote/frame recovery while uncertain delivery still kept overall readiness degraded. The repository sync imported the running collector's session-parser changes and added regression coverage; it did not perform the production recovery.
6. Describe what would justify moving beyond one Linux host and SQLite, rather than adding infrastructure preemptively.

## Suggested GitHub metadata

- Name: `homelab-telegram-bots`
- Description: `Two self-hosted Telegram bots for market signals and Linux homelab operations, with durable SQLite state, health checks and offline tests.`
- Topics: `python`, `telegram-bot`, `homelab`, `sqlite`, `systemd`, `docker`, `websocket`, `automation`
