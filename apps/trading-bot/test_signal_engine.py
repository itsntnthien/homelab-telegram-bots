import unittest
import json
from datetime import datetime, timedelta, timezone

from signal_engine import BIAS_BULLISH, BIAS_NEUTRAL, CloudFrame, CloudMachine, DIR_BEARISH, DIR_BULLISH, DisplayFilter, Liquidity, POLARITY_BEAR, POLARITY_BULL, POLARITY_TRANSITION, PolarityDisplay, SE_BOS_BULLISH, SE_CHOCH_BEARISH, Bar, REGIME_RANGE_ACTIVE, REGIME_UPTREND, _contain_cloud_zones, atr, cloud_plan, ema, ema_rsi_policy, market_snapshots, official_bias_snapshots, pine_timeframe_family_authorized, plan_trade, population_stdev, replay_cloud_events, replay_diagnostics, replay_main_candidates, replay_main_events, rolling_extreme, sma, structure_snapshots, to_price, to_ticks, vsa_adjustment, wilder_rsi


class SignalMathTest(unittest.TestCase):
    def test_pine_compatibility_defaults(self):
        from signal_engine import PINE_CONFIG_FINGERPRINT, PINE_CORE_VERSION, PINE_MINIMUM_RR_MILLI, PINE_SPREAD_PRICE
        self.assertEqual((PINE_CORE_VERSION, PINE_CONFIG_FINGERPRINT), (1603001, 8211403))
        self.assertEqual(PINE_MINIMUM_RR_MILLI, 1250)
        self.assertEqual(PINE_SPREAD_PRICE, 0.0)

    def test_timeframe_family_authorization_matches_pine(self):
        self.assertTrue(pine_timeframe_family_authorized("m1", 1))
        self.assertFalse(pine_timeframe_family_authorized("m1", 3))
        self.assertTrue(pine_timeframe_family_authorized("m3", 3))
        self.assertFalse(pine_timeframe_family_authorized("m5", 3))
        self.assertTrue(pine_timeframe_family_authorized("m15", 3))
        self.assertFalse(pine_timeframe_family_authorized("m15", 1))
        for timeframe in ("m30", "h1"):
            self.assertTrue(pine_timeframe_family_authorized(timeframe, 1))
            self.assertTrue(pine_timeframe_family_authorized(timeframe, 2))
            self.assertTrue(pine_timeframe_family_authorized(timeframe, 3))

    def test_wilder_rsi_and_soft_ema_rsi_policy(self):
        values = list(range(1, 16))
        result = wilder_rsi(values, 14)
        self.assertAlmostEqual(result[-1], 100.0)
        aligned = ema_rsi_policy(DIR_BULLISH, 103.0, 100.0, 1.0, 0.1, 60.0, 1.0, 1)
        opposed = ema_rsi_policy(DIR_BULLISH, 97.0, 100.0, 1.0, -0.1, 40.0, -1.0, 1)
        self.assertEqual((aligned.adjustment, aligned.ema_adjustment, aligned.rsi_adjustment, aligned.veto), (14, 8, 6, False))
        self.assertEqual((opposed.adjustment, opposed.ema_adjustment, opposed.rsi_adjustment, opposed.veto), (-14, -8, -6, False))

    def test_vsa_adjustment_neutral_without_volume_and_directional_with_volume(self):
        self.assertEqual(vsa_adjustment(DIR_BULLISH, 1, 1, True, False, False, False, False), 15)
        self.assertEqual(vsa_adjustment(DIR_BULLISH, -1, 1, True, False, False, False, False), -15)
        self.assertEqual(vsa_adjustment(DIR_BULLISH, 1, False, False, False, False, False, False), 0)
        self.assertEqual(vsa_adjustment(DIR_BULLISH, 1, 1, True, True, True, False, False), 5)

    def test_cloud_risk_presets(self):
        self.assertEqual(cloud_plan("M1", DIR_BULLISH, 420000), (419600, 420800, 400, 200))
        self.assertEqual(cloud_plan("M15", DIR_BEARISH, 420000), (421000, 418000, 1000, 750))

    def test_legacy_event_metadata_does_not_claim_release1(self):
        from signal_engine import SignalEvent
        event = SignalEvent("k", "USTEC", "M1", "CLOUD BUY", "t", "t", 100, 90, 120, "CLOUD")
        self.assertEqual((event.engine_version, event.config_hash), ("LEGACY", None))
    def test_display_filter_caps_motion_deterministically(self):
        expected = [(90.0, 110.0), (92.5, 117.5), (97.5, 132.5)]
        inputs = [(90, 110, 20), (110, 170, 20), (110, 170, 20)]
        for _ in range(2):
            display = DisplayFilter()
            self.assertEqual([display.step(*values) for values in inputs], expected)
            self.assertIsNone(display.step(100, 100, 20))
            self.assertEqual(display.step(95, 105, 20), (95.0, 105.0))

    def test_polarity_requires_confirmation_and_pinches_on_reversal(self):
        display = PolarityDisplay()
        step = lambda bull, bear: display.step(100, 20, 0.5, bull, bear, bull, bear,
                                                REGIME_RANGE_ACTIVE, 0, True)
        step(0.8, 0.1)
        step(0.8, 0.1)
        self.assertEqual(display.state, POLARITY_BULL)
        step(0.3, 0.7)
        self.assertEqual(display.state, POLARITY_TRANSITION)
        step(0.1, 0.8)
        lower, upper = step(0.1, 0.8)
        self.assertAlmostEqual(upper - lower, 3.0)
        step(0.1, 0.8)
        self.assertEqual(display.state, POLARITY_BEAR)

    def test_cloud_context_is_shifted_outside_price_and_polarity(self):
        zones = _contain_cloud_zones(Bar("t", 100, 105, 95, 100, 1), 10,
                                     (99, 101), (100, 102), (98, 100))
        self.assertEqual(zones, ((99, 101), (106.0, 108.0), (92.0, 94.0)))

    def test_pine_style_series_math(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(sma(values, 3), [None, None, 2.0, 3.0, 4.0])
        self.assertEqual(ema(values, 3), [None, None, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(population_stdev(values, 3)[-1], (2 / 3) ** 0.5)
        self.assertEqual(rolling_extreme(values, 3), [None, None, 3.0, 4.0, 5.0])
        self.assertEqual(rolling_extreme(values, 3, False), [None, None, 1.0, 2.0, 3.0])
        self.assertEqual(to_ticks(4221.055), 422106)
        self.assertEqual(to_ticks(-1.005), -101)
        self.assertEqual(to_price(422106), 4221.06)

    def test_wilder_atr(self):
        bars = [Bar(str(i), i, i + 2, i - 1, i + 1, 10) for i in range(1, 6)]
        self.assertEqual(atr(bars, 3), [None, None, 3.0, 3.0, 3.0])

    def test_market_state_warmup(self):
        bars = [Bar(f"{i:02}", 100, 101, 99, 100, 10) for i in range(25)]
        snapshots = market_snapshots(bars)
        self.assertFalse(snapshots[19].warmup_valid)
        self.assertTrue(snapshots[20].warmup_valid)
        self.assertEqual(snapshots[-1].regime, REGIME_RANGE_ACTIVE)
        self.assertEqual(snapshots[-1].cloud_core, 100)

    def test_completed_htf_buckets_and_bias_hysteresis(self):
        start = datetime(2026, 9, 28, tzinfo=timezone.utc)
        bars = []
        for i in range(11):
            timestamp = (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z")
            bars.append(Bar(timestamp, 100 + i, 102 + i, 99 + i, 101 + i, 10))
        snapshots = official_bias_snapshots(bars, "m1")
        self.assertTrue(snapshots[5].confirmation_committed)
        self.assertEqual(snapshots[5].confirmation.open, 100)
        self.assertEqual(snapshots[5].confirmation.close, 105)
        self.assertEqual(snapshots[5].official_bias, BIAS_NEUTRAL)
        self.assertEqual(snapshots[10].official_bias, BIAS_BULLISH)

        missing = official_bias_snapshots(bars[:3] + bars[4:6], "m1")
        self.assertFalse(any(item.confirmation_committed for item in missing))

    def test_pine_asymmetric_confirmed_swings(self):
        highs = [1, 2, 3, 4, 5, 10, 9, 8, 7, 6, 5]
        lows = [10, 9, 8, 7, 6, 1, 2, 3, 4, 5, 6]
        bars = [Bar(str(i), 5, high, low, 5, 1) for i, (high, low) in enumerate(zip(highs, lows))]
        snapshot = market_snapshots(bars)[-1]
        self.assertEqual(snapshot.swing_high_ticks, 1000)
        self.assertEqual(snapshot.swing_low_ticks, 100)
        self.assertEqual(snapshot.swing_high_source_timestamp, "5")
        self.assertEqual(snapshot.swing_low_confirmation_timestamp, "10")

    def test_bos_then_choch(self):
        highs = [1, 2, 3, 4, 5, 10, 9, 8, 7, 6, 5, 11, 9, 8, 7, 6, 5, 4]
        lows = [5, 4, 3, 2, 1, 0, 1, 2, 3, 4, 5, 4, 3, 2, 1, -1, -2, -3]
        closes = [5] * len(highs)
        closes[11] = 11
        closes[17] = -3
        bars = [Bar(str(i), 5, high, low, close, 1) for i, (high, low, close) in enumerate(zip(highs, lows, closes))]
        structures = structure_snapshots(bars)
        self.assertEqual(structures[11].event_type, SE_BOS_BULLISH)
        self.assertEqual(structures[11].trend, DIR_BULLISH)
        self.assertEqual(structures[17].event_type, SE_CHOCH_BEARISH)

    def test_b04_plan_selects_nearest_liquidity_and_rr(self):
        pools = [Liquidity(DIR_BEARISH, 2, 3, 10200), Liquidity(DIR_BEARISH, 1, 2, 10150)]
        plan = plan_trade(DIR_BULLISH, 10000, 100, 9900, pools, 10, 3)
        self.assertTrue(plan.entry_ready)
        self.assertEqual(plan.sl_ticks, 9900)
        self.assertEqual(plan.desired_tp_ticks, 10200)
        self.assertEqual(plan.effective_tp_ticks, 10150)
        self.assertEqual(plan.effective_rr_milli, 1500)
        self.assertEqual(plan.selected_liquidity_id, 1)

    def test_b04_plan_requires_new_pine_minimum_rr(self):
        pools = [Liquidity(DIR_BEARISH, 1, 2, 10120)]
        plan = plan_trade(DIR_BULLISH, 10000, 100, 9900, pools, 10, 3)
        self.assertFalse(plan.entry_ready)

    def test_candidate_replay_is_deterministic(self):
        start = datetime(2026, 9, 28, tzinfo=timezone.utc)
        bars = [Bar((start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
                    100 + i / 10, 101 + i / 10, 99 + i / 10, 100.5 + i / 10, 10)
                for i in range(40)]
        first = replay_main_candidates(bars, "m1")
        second = replay_main_candidates(bars, "m1")
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(bars))
        self.assertEqual(replay_main_events(bars, "m1"), replay_main_events(bars, "m1"))
        self.assertEqual(replay_cloud_events(bars, "m1"), replay_cloud_events(bars, "m1"))
        diagnostics = replay_diagnostics(bars, "m1")
        self.assertEqual(diagnostics, replay_diagnostics(bars, "m1"))
        self.assertEqual(len(diagnostics), len(bars))
        self.assertIn("cloud_reason", diagnostics[-1])
        self.assertEqual(diagnostics[-1]["pine_core_version"], 1603001)
        self.assertEqual(diagnostics[-1]["pine_config_fingerprint"], 8211403)
        self.assertIn("policy_quality", diagnostics[-1])
        self.assertIn("vsa_adjustment", diagnostics[-1])
        self.assertIn("ema200_adjustment", diagnostics[-1])
        self.assertIn("rsi_adjustment", diagnostics[-1])
        for event in replay_main_events(bars, "m1"):
            self.assertIn(event.signal, ("MAIN BUY", "MAIN SELL"))
            self.assertLess(event.sl_ticks, event.entry_ticks) if event.signal.endswith("BUY") else self.assertLess(event.entry_ticks, event.sl_ticks)
            json.dumps(event.__dict__)
        for event in replay_cloud_events(bars, "m1"):
            self.assertEqual((event.entry_ticks, event.sl_ticks, event.tp_ticks), (None, None, None))

    def test_qualified_cloud_locks_duplicate_interaction(self):
        frame = CloudFrame("1", REGIME_UPTREND, 101.5, 99.5, 101.2, 100.5, 1, 100,
                           DIR_BULLISH, 0.2, 99.8, 101, 103, 104, 96, 97,
                           SE_BOS_BULLISH)
        machine = CloudMachine()
        first = machine.step(frame)
        second = machine.step(frame)
        self.assertTrue(first.qualified)
        self.assertFalse(second.qualified)
        self.assertEqual((first.epoch, second.epoch), (1, 1))


if __name__ == "__main__":
    unittest.main()
