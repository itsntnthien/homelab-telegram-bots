# Validation record

Checked on **2026-11-23**. These results describe the packaged repository and a read-only source-capture check. They are not an uptime or trading-performance certification.

## Package verification

| Check | Result |
| --- | --- |
| Trading Python suite | **253 passed** on Windows/Python 3.13 and Linux CI/Python 3.13–3.14. |
| Control Python suite | **7 passed** on Linux, plus the existing command self-test. Host-changing commands and Telegram calls are mocked. |
| Node signer/session tests | **8 passed** on Node 24, using ephemeral keys and synthetic calendar data with no broker connection. |
| Transport syntax | `node --check exness_transport.mjs` passed. |
| Offline demo | Passed: deduplication, pending state after reopen, and `UNKNOWN` suppression. |
| Python dependencies | Installed from the pinned manifest into a fresh Linux virtual environment. |
| npm dependencies | Lockfile generated; install/audit reported zero known vulnerabilities at verification time. This is not a broader security audit. |
| Package checks | Passed known secret-pattern/file checks, Python import declaration checks and frozen Pine fingerprint validation. |

Current suite: **268 passing automated test cases on Linux**, plus self-test, demo and package checks (240 before personal sizing; 252 before account buttons). Windows deliberately skips Linux control tests. The initial 231-test package passed [hosted CI on Python 3.13/3.14](https://github.com/itsntnthien/homelab-telegram-bots/actions/runs/45125355051). See the [workflow runs](https://github.com/itsntnthien/homelab-telegram-bots/actions) for the result corresponding to the current commit.

Linux verification ran from an isolated temporary copy using a fresh virtual environment. Node tests ran in an existing Node 24 container with networking disabled. No production database, Telegram token or broker credential was passed to the tests. The two regression comparisons retain independent historical engine fixtures; they were not removed or replaced with self-comparisons to make packaging pass.

Run the same checks from a clone with:

```bash
python -m pip install -r requirements.txt
npm ci --ignore-scripts --prefix apps/trading-bot
python scripts/check.py --require-linux
```

## Source-capture observations

Read-only checks confirmed that both the XAUUSD bot and control bot systemd services were `active/running`. The trading launcher pointed to release `xau-intraday-scalp-combined-v2`, configuration hash:

```text
da1d22e744aa4fee876d696a7bc5def4e795dabbac1a0feaa1712651f009f1c7
```

The configured market collector was failed. Trading health reported `alive=true`, `ready=false`, `STALE_FEED` and uncertain-delivery degradation. Therefore this repository says **deployed**, not continuously healthy or production-certified. Packaging did not restart services, fix broker access, resend messages or change production configuration.

## Feed recovery and collector sync

A read-only recheck on **2026-11-23 at 13:45 Vietnam time** found that the collector had been running since 13:37. Quote age was about 0.8 seconds with no queued ticks; M5/M15/M30/H1 were ready and M5/M15 checkpoints advanced during observation. `STALE_FEED` was absent. Overall readiness remained false with `DELIVERY_UNKNOWN` (38 stored uncertain notifications).

This confirms market-data recovery, not a new trading signal or phone delivery. At that check, no new signal had been persisted after recovery and no new notification had been queued in the preceding two hours.

Compared with the initial snapshot, 63 of 65 recorded service source files were unchanged. `exness_collector.py` and `exness_transport.mjs` had changed and `exness_sessions.mjs` had been added. These three files were copied from the running service into this repository; original/updated hashes and the sync timestamp are recorded in `source-provenance.json`. Engine configuration and control-bot behavior are unchanged.

The added regression cases cover alternate session formatting, invalid or missing boundaries, inclusive session ends, disabled sessions, invalid-calendar rollback and transport termination on a blocked schedule. Before the sync, the alternate-format test reproduced a `ValueError` in the original parser. The captured live calendar happened to use a format both versions could parse, so these checks do not attribute the production outage or recovery exclusively to the parser change.

The example market service also adopts the running service's `RestartPreventExitStatus=78` so blocked configuration errors do not trigger automatic restart loops.

## Scope and limits

Personal sizing adds 12 tests covering decimal rounding, min/max lots, invalid inputs, separate private recipients, durable preferences, additive schema preservation, entry snapshots and actual mocked Telegram delivery with distinct lots. A separate old-code → new-code → old-code check preserved the synthetic ledger, pending outbox, poll cursor and new preferences. Tests use fake capital and no real user balances. These checks do not establish live deployment or broker execution.

Nine additional account-button tests cover USD/USC conversion, standard/cent lot units, fixed-money and percentage risk, account switching, persistent input, invalid-input atomicity, cancel/navigation, private-owner authorization, delete confirmation, existing-USD migration, queued account snapshots and real polling with only button labels/numbers. The Cent reference grid and XAUUSD reference quote are explicit assumptions; these tests do not verify a recipient's actual XAUUSDc server specifications or execution.

An additional display regression separates the chosen risk budget from estimated SL loss, checks the standard/Cent calculations independently, verifies rounded money display (including sub-cent amounts), and confirms emphasized lot text does not alter the exact audit values.

Six history-date tests cover Vietnamese midnight boundaries, filtering by opening rather than closing date, recipient isolation, retained date pagination, leap days/month navigation, malformed callbacks, empty dates, quick filters around UTC midnight and the real polling route with synthetic button callbacks. The history filter adds no database migration.

- No live end-to-end broker-to-phone delivery test or production admin action was performed.
- No claim of profitability, Pine parity, complete broker holiday coverage, 48-hour readiness acceptance or an availability SLO.
- Some calendar providers can be unavailable; cached data is not a guarantee of a complete event calendar.
- Secret checks target common patterns and known file classes; they cannot establish absence of every possible secret format.
- Charts use available fonts; a Linux installation should provide DejaVu Sans for readable labels.
- Systemd units are portable examples and were not installed or enabled on production.

Original source hashes and packaging adjustments are documented in [source-provenance.json](source-provenance.json) and [architecture.md](architecture.md).
