import unittest

from release1_context import *


class Release1ContextTest(unittest.TestCase):
    def test_dow_full_alignment_and_countertrend_severity(self):
        bull = dow_frame(1, 7, 1)
        self.assertEqual((bull.alignment, bull.buy_effect, bull.sell_effect),
                         (DOW_ALIGNMENT_FULL_BULL, SUPPORT_STRONG, CONFLICT_STRONG))
        conflict = dow_frame(0, 7, -1)
        self.assertEqual((conflict.alignment, conflict.buy_severity, conflict.sell_severity),
                         (DOW_ALIGNMENT_CONFLICTING, 2, 2))

    def test_wyckoff_freshness_leaning_and_dual_neutralization(self):
        spring = wyckoff_frame(regime_eligible=True, latest_spring_confirm_bar=10,
                               latest_upthrust_confirm_bar=5, spring_event=True)
        self.assertEqual((spring.leaning, spring.buy_effect, spring.sell_effect),
                         (WYCKOFF_LEANING_ACCUMULATION, SUPPORT, CONFLICT))
        dual = wyckoff_frame(regime_eligible=True, latest_spring_confirm_bar=10,
                             latest_upthrust_confirm_bar=11, spring_event=True,
                             upthrust_event=True)
        self.assertEqual((dual.buy_effect, dual.sell_effect), (NEUTRAL, NEUTRAL))

    def test_acceptance_direct_retest_failure_and_terminal(self):
        state = arm_acceptance(AcceptanceState(), reference_id="r1", direction=1,
                               level_ticks=100, bar_index=10, regime=6)
        direct = acceptance_step(state, bar_index=11, close_ticks=101,
                                 high_ticks=102, low_ticks=101)
        self.assertEqual((direct.state.fsm, direct.acceptance_event),
                         (AR_ACCEPTED, AR_ACCEPT_DIRECT))
        failed = acceptance_step(direct.state, bar_index=12, close_ticks=100,
                                 high_ticks=102, low_ticks=99)
        self.assertEqual((failed.state.fsm, failed.rejection_event),
                         (AR_FAILED, AR_REJECT_FAILED_ACCEPTANCE))

        state = arm_acceptance(AcceptanceState(), reference_id="r2", direction=-1,
                               level_ticks=100, bar_index=20, regime=6)
        retest = acceptance_step(state, bar_index=21, close_ticks=99,
                                 high_ticks=100, low_ticks=98)
        self.assertEqual(retest.acceptance_event, AR_ACCEPT_RETEST)

        state = arm_acceptance(AcceptanceState(), reference_id="r3", direction=1,
                               level_ticks=100, bar_index=30, regime=6)
        rejected = acceptance_step(state, bar_index=31, close_ticks=99,
                                   high_ticks=100, low_ticks=98)
        self.assertEqual(rejected.rejection_event, AR_REJECT_PENDING_FAILURE)
        terminal = acceptance_step(rejected.state, bar_index=33, close_ticks=99,
                                   high_ticks=100, low_ticks=98)
        self.assertEqual(terminal.state.fsm, AR_TERMINAL)


if __name__ == "__main__":
    unittest.main()
