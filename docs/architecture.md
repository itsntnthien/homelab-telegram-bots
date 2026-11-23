# Architecture and design decisions

## Two applications, independent lifecycles

The trading bot and control bot share a repository for review and testing. They do not share a Telegram token, polling loop or database. Each is deployed as a separate systemd service. The trading application additionally runs a market collector and an optional calendar worker.

## Market data and signal processing

`exness_transport.mjs` signs read-only GET requests with Ed25519 and subscribes to quotes over WebSocket. Host validation restricts credentials to the expected broker domains. The Python collector validates candle ordering, geometry and timestamps before committing data. `tick_journal.py` provides an ordered stream that the engine resumes by cursor.

`intraday_live.py` combines the CURRENT, Q100 and SCALP strategies under one position owner. M5/M30 bars drive setups; M15/H1 provide context. It stores indicator state, pending setups, active modeled positions and cursor progress. Transactions keep a committed event consistent with its saved state. Config hashes prevent silently resuming incompatible engine state.

The supplied configuration is the deployed experimental release, not a claim of a profitable strategy. Compatibility modules are included where the active bot imports them; older standalone collectors and services are omitted. The frozen Pine file is a source fingerprint fixture, not evidence of verified TradingView parity.

## Persistence and delivery

`trades.py` owns trade/event tables, user-specific signal numbering and the Telegram outbox. Unique event keys collapse repeated events. Entry and exit notifications can have dependency ordering. `delivery.py` checks whether an entry is still timely and valid at send time.

A timeout can occur after Telegram has accepted a request. Such an outcome is recorded as `UNKNOWN`, rather than assuming failure and blindly duplicating the message. This is explicit ambiguity handling, **not exactly-once delivery**. Telegram acknowledgement does not prove that a phone received or displayed the message.

`setup_view.py`, `telegram_views.py` and `smc_chart.py` render saved signal evidence. Chat-scoped lookups restrict which stored setup a recipient can view. Chart generation uses Pillow and reuses recorded structure state.

## Administration

The control bot polls Telegram and ignores messages outside `TELEGRAM_ADMIN_CHAT_ID`. It accepts fixed commands only. Restart/reboot requests create an in-memory, chat-bound confirmation valid for 60 seconds. Poll offsets persist with private file permissions; a Linux file lock prevents concurrent owners of the same state directory.

Docker operations run as the service user. Docker daemon access is a powerful host capability; the Telegram allowlist and system account boundary must both be maintained. Reboot also requires an explicitly configured sudo rule. The example service does not grant that permission.

## Health and failure behavior

The trading health endpoint reports `alive`, `ready`, `degraded_reasons`, feed age, frame checkpoints, outbox state and worker heartbeats. A running process with a stale feed is not healthy for new signals. Source failures and uncertain notification delivery remain visible.

SQLite and systemd suit a single-host homelab with one owner per state file. This package does not add a broker, distributed workers, high availability or automatic account trading.

## Packaging changes

- Preserved the deployed trading algorithms and configuration hash.
- Added `launch.py` for portable paths and explicit shadow/live startup; missing input files stop startup before workers run.
- Made control-bot DNS, container, volume and state paths configurable. Backup ownership follows the current user's UID/GID.
- Added dependency manifests, systemd templates, package checks, a synthetic demo and additional tests.
- Recorded original file hashes in `source-provenance.json`; production credentials and databases were never copied into the package.
- Kept two frozen historical engine fixtures and changed their test paths to repository-local files, so regression comparisons work after cloning.
- Normalize ordinary text to LF in Git while preserving exact Pine bytes. Provenance records both raw and newline-normalized hashes.
