"""Portable entry point; defaults to isolated shadow processing without Telegram."""
import os
from pathlib import Path


def configure():
    defaults = {
        'SIGNAL_ENGINE': 'intraday-shadow', 'DRY_RUN': 'true',
        'SYMBOL': 'XAUUSD', 'MARKET_SOURCE': 'exness',
        'MARKET_DB': 'state/market-exness.db',
        'MARKET_SCHEDULE_FILE': 'state/exness-schedule.json',
        'INTRADAY_SHADOW_DB': 'state/shadow.db',
        'TRADES_DB': 'state/trades.db', 'NEWS_DB': 'state/news.db',
        'BOT_HEALTH_PORT': '18766',
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)
    if os.environ['SIGNAL_ENGINE'] not in {'intraday-shadow', 'intraday-live'}:
        raise SystemExit('This launcher supports intraday-shadow and intraday-live only')
    for name in ('MARKET_DB', 'MARKET_SCHEDULE_FILE'):
        if not Path(os.environ[name]).is_file():
            raise SystemExit(f'{name} is missing; start the collector first (see docs/operations.md)')
    for name in ('INTRADAY_SHADOW_DB', 'TRADES_DB', 'NEWS_DB'):
        Path(os.environ[name]).parent.mkdir(parents=True, exist_ok=True)


if __name__ == '__main__':
    configure()
    from intraday_live import run
    run()
