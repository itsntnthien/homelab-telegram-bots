import unittest

from release1_execution import Release1Execution, SetupState


def setup(**overrides):
    values = dict(event_key="xau:m1:1", direction=1, created_bar="1",
                  expires_after_bars=3, entry_ticks=10000, sl_ticks=9900,
                  tp2_ticks=10125, family=1, subtype=101, grade=2, quality=80)
    values.update(overrides)
    return SetupState(**values)


class Release1ExecutionTest(unittest.TestCase):
    def test_next_bar_retest_opens_at_minimum_rr(self):
        engine = Release1Execution()
        self.assertTrue(engine.arm(setup()))
        events = engine.on_closed_bar(timestamp="2", open_ticks=10010,
                                      high_ticks=10020, low_ticks=9995,
                                      maximum_chase_ticks=25)
        self.assertEqual([e["event_type"] for e in events], ["PRIMARY_OPEN"])
        self.assertEqual(events[0]["entry_ticks"], 10000)

    def test_rr_below_125_rejects_setup(self):
        engine = Release1Execution()
        engine.arm(setup(tp2_ticks=10124))
        events = engine.on_closed_bar(timestamp="2", open_ticks=10000,
                                      high_ticks=10010, low_ticks=9995,
                                      maximum_chase_ticks=25)
        self.assertEqual(events[0]["event_type"], "SETUP_CANCELLED")
        self.assertEqual(events[0]["reason"], "RR")

    def test_snapshot_restores_without_duplicate_thesis(self):
        engine = Release1Execution()
        engine.arm(setup())
        engine.on_closed_bar(timestamp="2", open_ticks=10000, high_ticks=10010,
                             low_ticks=9995, maximum_chase_ticks=25)
        restored = Release1Execution.restore(engine.snapshot())
        self.assertFalse(restored.arm(setup(event_key="xau:m1:other")))

    def test_two_stage_exit_protects_runner(self):
        engine = Release1Execution(two_stage_exit=True)
        engine.arm(setup(tp2_ticks=10200))
        engine.on_closed_bar(timestamp="2", open_ticks=10000, high_ticks=10005,
                             low_ticks=9995, maximum_chase_ticks=25)
        events = engine.on_tick(timestamp="3", bid_ticks=10100, ask_ticks=10101)
        self.assertEqual([e["event_type"] for e in events], ["TP1", "STOP_PROTECTED"])
        events = engine.on_tick(timestamp="4", bid_ticks=10000, ask_ticks=10001)
        self.assertEqual(events[0]["event_type"], "BE")

    def test_historical_ambiguous_bar_is_stop_first(self):
        engine = Release1Execution()
        engine.arm(setup())
        engine.on_closed_bar(timestamp="2", open_ticks=10000, high_ticks=10005,
                             low_ticks=9995, maximum_chase_ticks=25)
        events = engine.on_closed_bar(timestamp="3", open_ticks=10000,
                                      high_ticks=10125, low_ticks=9900,
                                      maximum_chase_ticks=25)
        self.assertEqual(events[0]["event_type"], "SL")
        self.assertTrue(events[0]["ambiguous"])

    def test_dca_is_disabled_by_default_and_single_use_when_enabled(self):
        disabled = Release1Execution()
        enabled = Release1Execution(trend_dca=True)
        for engine in (disabled, enabled):
            engine.arm(setup(tp2_ticks=10200))
            engine.on_closed_bar(timestamp="2", open_ticks=10000, high_ticks=10005,
                                 low_ticks=9995, maximum_chase_ticks=25)
            engine.campaign.best_ticks = 10150
        candidate = setup(event_key="dca", entry_ticks=10050, sl_ticks=9950,
                          tp2_ticks=10200, grade=2, quality=85)
        self.assertEqual(disabled.consider_dca(timestamp="3", timeframe="m5",
                                               candidate=candidate, trend_strength=80), [])
        event = enabled.consider_dca(timestamp="3", timeframe="m5",
                                     candidate=candidate, trend_strength=80)
        self.assertEqual((event[0]["event_type"], event[0]["risk_share_milli"]),
                         ("DCA_OPEN", 300))
        self.assertEqual(enabled.consider_dca(timestamp="4", timeframe="m5",
                                              candidate=candidate, trend_strength=80), [])

    def test_waiting_setup_requires_real_upgrade(self):
        engine = Release1Execution()
        self.assertTrue(engine.arm(setup(quality=80)))
        self.assertFalse(engine.arm(setup(event_key="weak", quality=84)))
        self.assertTrue(engine.arm(setup(event_key="upgrade", quality=85)))

    def test_entry_bar_fill_then_stop_first(self):
        engine = Release1Execution()
        engine.arm(setup())
        events = engine.on_closed_bar(timestamp="2", open_ticks=10010,
                                      high_ticks=10125, low_ticks=9890,
                                      maximum_chase_ticks=25)
        self.assertEqual([event["event_type"] for event in events], ["PRIMARY_OPEN", "SL"])
        self.assertTrue(events[-1]["ambiguous"])


if __name__ == "__main__":
    unittest.main()
