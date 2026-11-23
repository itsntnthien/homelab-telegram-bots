import unittest
import random
import json
import tempfile
from pathlib import Path
from intraday_research import load_file,build_signals,epoch,metrics,without_best
from intraday_execution import Profile,simulate,execution_exit,make_position


def row(t,op=100,hi=101,lo=99,cl=100,minute=600):
    return dict(time=t,bid=(op,hi,lo,cl),ask=(op+.2,hi+.2,lo+.2,cl+.2),day='2026-06-01',ny_minute=minute,london_minute=minute+300)

def candidate(t,d=1):
    return dict(time=t,direction=d,entry=100,sl=98 if d==1 else 102,atr=2,family=1,quality=90)

COST=dict(slippage=0.,commission=0.,spread_floor=0.)


class IntradayTests(unittest.TestCase):
    def test_long_ask_entry_and_bid_stop(self):
        p=make_position(candidate(0),row(0),Profile(),COST)
        self.assertAlmostEqual(p['entry'],100.2)
        self.assertEqual(execution_exit(p,row(60,hi=101,lo=97))[1],'SL')

    def test_short_ask_stop(self):
        p=make_position(candidate(0,-1),row(0),Profile(),COST)
        self.assertEqual(execution_exit(p,row(60,hi=101.9,lo=99))[1],'SL')

    def test_ambiguous_two_policies(self):
        p=dict(direction=1,sl=98,tp=104)
        self.assertEqual(execution_exit(p,row(0,hi=105,lo=97)),(98,'SL',True))
        self.assertEqual(execution_exit(p,row(0,hi=105,lo=97),'target_first'),(104,'TP',True))

    def test_stop_gap_does_not_fill_at_stale_stop(self):
        self.assertEqual(execution_exit(dict(direction=1,sl=98,tp=104),row(0,op=95,hi=96,lo=94,cl=95)),(95,'SL_GAP',False))

    def test_no_future_signal_or_prefill_target(self):
        trades,_=simulate([row(0,hi=105,lo=100),row(60,op=101,hi=105,lo=99),row(120)],{60:candidate(60)},Profile(maximum_chase_atr=0),COST)
        self.assertEqual(trades[0]['entered'],60)
        self.assertNotEqual(trades[0]['reason'],'TP')

    def test_forced_day_close_and_no_new_entry(self):
        trades,_=simulate([row(0),row(60,minute=960)],{0:candidate(0),60:candidate(60)},Profile(),COST)
        self.assertEqual(len(trades),1);self.assertEqual(trades[0]['reason'],'DAY_CLOSE')

    def test_expires_after_one_signal_bar(self):
        rows=[row(t,op=103,hi=104,lo=102,cl=103) for t in [0,60,1800,1860]]
        rows[-1]=row(1860)
        trades,stats=simulate(rows,{0:candidate(0)},Profile(maximum_chase_atr=0),COST)
        self.assertEqual(trades,[]);self.assertEqual(stats['expired'],1)

    def test_prefix_causality(self):
        # A future bar/signal cannot change the first completed trade.
        first=[row(0),row(60,hi=101,lo=97),row(120)]
        a,_=simulate(first,{0:candidate(0)},Profile(),COST)
        b,_=simulate(first+[row(180)],{0:candidate(0),180:candidate(180)},Profile(),COST)
        self.assertEqual(a[0],b[0])

    def test_commission_and_slippage_change_r(self):
        rows=[row(0),row(60,hi=101,lo=97)]
        a,_=simulate(rows,{0:candidate(0)},Profile(),COST)
        self.assertAlmostEqual(a[0]['r'],-1.)
        cost=dict(slippage=.05,commission=.1,spread_floor=.3)
        b,_=simulate(rows,{0:candidate(0)},Profile(),cost)
        self.assertAlmostEqual(b[0]['r'],-1.)
        self.assertGreater(b[0]['risk'],a[0]['risk'])

    def test_hold_limit(self):
        trades,_=simulate([row(0),row(60)],{0:candidate(0)},Profile(maximum_hold_minutes=1),COST)
        self.assertEqual(trades[0]['reason'],'TIME_EXIT')

    def test_invalid_cost_profile_and_order_rejected(self):
        with self.assertRaises(ValueError):Profile(stop_atr_floor=0)
        with self.assertRaises(ValueError):simulate([row(0)],{},Profile(),dict(COST,commission=-1))
        with self.assertRaises(ValueError):simulate([row(60),row(0)],{},Profile(),COST)

    def test_one_position_max_daily_entries_and_no_normal_overnight(self):
        rows=[row(i*60,hi=105,lo=97,minute=8*60+i) for i in range(8*60+1)]
        signals={r['time']:candidate(r['time']) for r in rows}
        trades,_=simulate(rows,signals,Profile(),COST)
        self.assertLessEqual(len(trades),3)
        self.assertTrue(all(t['held_minutes']<=240 for t in trades))

    def test_missing_next_bar_open_does_not_retroactively_enter(self):
        trades,_=simulate([row(60),row(120)],{0:candidate(0)},Profile(),COST)
        self.assertEqual(trades,[])

    def test_holiday_gap_is_not_interpolated(self):
        a=row(0);b=row(7200,op=95,hi=96,lo=94,cl=95,minute=18*60)
        trades,st=simulate([a,b],{0:candidate(0)},Profile(),COST)
        self.assertEqual(trades[0]['reason'],'DATA_GAP');self.assertEqual(st['overnight'],1)
        self.assertLess(trades[0]['r'],-1.)

    def test_randomized_target_first_never_worse_for_same_position(self):
        rng=random.Random(42)
        for _ in range(1000):
            d=rng.choice([-1,1]);p=dict(direction=d,sl=100-d*2,tp=100+d*4)
            r=row(0,hi=100+rng.random()*7,lo=100-rng.random()*7)
            a=execution_exit(p,r);b=execution_exit(p,r,'target_first')
            if a and b:self.assertGreaterEqual((b[0]-a[0])*d,0)

    def test_conflicting_duplicates_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'bars.jsonl'
            a=dict(time='2026-05-01T00:00:00Z',open=100,high=101,low=99,close=100,volume=1)
            p.write_text(json.dumps(a)+'\n'+json.dumps(dict(a,close=100.5)))
            with self.assertRaises(ValueError):load_file(p)

    def test_feature_prefix_is_not_changed_by_future_bars(self):
        with tempfile.TemporaryDirectory() as td:
            from datetime import datetime,timezone
            p=Path(td)/'m15-bid.jsonl';rng=random.Random(4);bars=[];price=100.
            for i in range(500):
                op=price;price+=rng.uniform(-1,1)
                bars.append(dict(time=datetime.fromtimestamp(1775001600+i*900,timezone.utc).isoformat(),
                    open=op,high=max(op,price)+.2,low=min(op,price)-.2,close=price,volume=1))
            p.write_text('\n'.join(map(json.dumps,bars[:400])))
            a,_=build_signals(Path(td),'m15')
            p.write_text('\n'.join(map(json.dumps,bars)))
            b,_=build_signals(Path(td),'m15')
            boundary=epoch(bars[399]['time'])+900
            self.assertEqual(a,{t:c for t,c in b.items() if t<=boundary})

    def test_reproducible_ledger_and_cost_rejection(self):
        rows=[row(0),row(60,hi=105,lo=99)]
        a=simulate(rows,{0:candidate(0)},Profile(),COST)
        self.assertEqual(a,simulate(rows,{0:candidate(0)},Profile(),COST))
        tr,_=simulate(rows,{0:candidate(0)},Profile(),dict(COST,spread_floor=10))
        self.assertEqual(tr,[])

    def test_gap_beyond_anchor_cancels_setup(self):
        self.assertIsNone(make_position(candidate(0),row(0,op=97,hi=98,lo=96),Profile(),COST))

    def test_daily_two_r_loss_block(self):
        rows=[row(i*60,hi=101,lo=97,minute=480+i) for i in range(180)]
        signals={r['time']:candidate(r['time']) for r in rows}
        trades,_=simulate(rows,signals,Profile(),COST)
        self.assertEqual(len(trades),2)

    def test_hand_calculated_performance(self):
        m=metrics([dict(r=r,held_minutes=10) for r in [2.,-1.,1.]])
        self.assertEqual(m['total_r'],2.);self.assertEqual(m['profit_factor'],3.)
        self.assertEqual(m['drawdown_r'],1.);self.assertAlmostEqual(m['win_rate'],2/3)

    def test_cannot_use_later_exit_to_enter_at_same_minute_open(self):
        rows=[row(t) for t in range(0,1801,60)]
        rows[-1]=row(1800,hi=101,lo=97)
        trades,_=simulate(rows,{0:candidate(0),1800:candidate(1800)},Profile(timeframe='m15'),COST)
        self.assertEqual(len(trades),1)

    def test_removing_best_preserves_chronology(self):
        tr=[dict(r=r,held_minutes=10,index=i) for i,r in enumerate([2,-1,1,-1,2,-1])]
        remaining=without_best(tr,2)
        self.assertEqual([t['index'] for t in remaining],[1,2,3,5])
        self.assertEqual(metrics(remaining)['drawdown_r'],2.)


if __name__=='__main__':unittest.main()
