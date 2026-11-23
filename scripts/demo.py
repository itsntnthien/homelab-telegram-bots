"""Offline demonstration of the real SQLite trade store. No Telegram or market calls."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'apps/trading-bot'))
from trades import TradeStore


def demonstrate():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/'demo.db'
        store = TradeStore(path)
        trade, created = store.create(event_key='synthetic:entry:1', timeframe='M5',
                                      signal='MAIN BUY', entry=100, sl=99, tp=102)
        duplicate, repeated = store.create(event_key='synthetic:entry:1', timeframe='M5',
                                          signal='MAIN BUY', entry=100, sl=99, tp=102)
        assert created and not repeated and trade.id == duplicate.id
        print('Trade saved; duplicate event did not create a second trade.')
        store.queue_notification('synthetic:notification:1', 'Synthetic demo only', 'demo-chat')
        store.close()
        store = TradeStore(path)
        assert len(store.pending_notifications()) == 1
        print('Restart simulated; pending notification survived in SQLite.')
        store.notification_uncertain('synthetic:notification:1', 'simulated timeout after send')
        assert not store.pending_notifications()
        assert store.notification_counts() == {'UNKNOWN': 1}
        print('Ambiguous send marked UNKNOWN; automatic retry suppressed.')
        store.close()
    print('Demo complete. Temporary data removed; no external requests made.')


if __name__ == '__main__':
    demonstrate()
