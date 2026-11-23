import importlib, os, tempfile, unittest

class NewsWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["NEWS_DB"] = os.path.join(cls.tmp.name, "news.db")
        cls.news = importlib.import_module("news_worker")

    @classmethod
    def tearDownClass(cls):
        cls.news.db.close()
        cls.tmp.cleanup()

    def test_filter_and_stable_deduplication(self):
        self.assertFalse(self.news.relevant("Euro Short Term Rate"))
        self.assertTrue(self.news.relevant("Consumer Price Index"))
        args = ("BLS", "cpi", "2026-10-14T12:30:00+00:00", "Consumer Price Index")
        self.news.put(*args)
        self.news.put(*args)
        self.assertEqual(self.news.db.execute("SELECT COUNT(*) FROM news").fetchone()[0], 1)

    def test_missing_values_are_not_invented(self):
        self.news.put("ISM", "pmi", "2026-10-01T15:00:00+00:00", "ISM Manufacturing PMI")
        self.assertEqual(self.news.db.execute("SELECT actual,forecast,previous FROM news WHERE source='ISM'").fetchone(), (None, None, None))

if __name__ == "__main__": unittest.main()
