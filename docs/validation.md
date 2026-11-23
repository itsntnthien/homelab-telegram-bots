# Validation record

Checked on **2026-11-23**. These results describe the packaged repository and a read-only source-capture check. They are not an uptime or trading-performance certification.

## Package verification

| Check | Result |
| --- | --- |
| Trading Python suite | **221 passed** on Windows/Python 3.13 and isolated Linux/Python 3.14. |
| Control Python suite | **7 passed** on Linux, plus the existing command self-test. Host-changing commands and Telegram calls are mocked. |
| Node signer tests | **3 passed** on Node 24, using ephemeral keys and no broker connection. |
| Transport syntax | `node --check exness_transport.mjs` passed. |
| Offline demo | Passed: deduplication, pending state after reopen, and `UNKNOWN` suppression. |
| Python dependencies | Installed from the pinned manifest into a fresh Linux virtual environment. |
| npm dependencies | Lockfile generated; install/audit reported zero known vulnerabilities at verification time. This is not a broader security audit. |
| Package checks | Passed known secret-pattern/file checks, Python import declaration checks and frozen Pine fingerprint validation. |

Total: **231 passing automated test cases on Linux**, plus self-test, demo and package checks. Windows deliberately skips Linux control tests. The GitHub Actions workflow is prepared for Python 3.13/3.14; no hosted workflow result is claimed before publication.

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

## Scope and limits

- No live end-to-end broker-to-phone delivery test or production admin action was performed.
- No claim of profitability, Pine parity, complete broker holiday coverage, 48-hour readiness acceptance or an availability SLO.
- Some calendar providers can be unavailable; cached data is not a guarantee of a complete event calendar.
- Secret checks target common patterns and known file classes; they cannot establish absence of every possible secret format.
- Charts use available fonts; a Linux installation should provide DejaVu Sans for readable labels.
- Systemd units are portable examples and were not installed or enabled on production.

Original source hashes and packaging adjustments are documented in [source-provenance.json](source-provenance.json) and [architecture.md](architecture.md).
