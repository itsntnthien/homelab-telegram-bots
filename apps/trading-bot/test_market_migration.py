import sqlite3
import tempfile
import unittest

from market_migration import migrate_market_db


class MarketMigrationTest(unittest.TestCase):
    def test_preserves_legacy_and_deduplicates_canonical_bars(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/market.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE bars(symbol TEXT,timeframe TEXT,timestamp TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)")
            row = ("XAUUSD", "m1", "2026-10-09T19:00:00.0000000Z", 1, 2, 0, 1.5, 10)
            db.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)", row)
            db.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)", (row[0], row[1], "2026-10-09T19:00:00+00:00", *row[3:]))
            db.commit(); db.close()
            migrate_market_db(path)
            db = sqlite3.connect(path)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bars").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bars_legacy").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM market_anomalies").fetchone()[0], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
