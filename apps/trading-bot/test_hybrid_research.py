import unittest
from dataclasses import replace
from hybrid_research import configurations,scalp_candidate,scalp_signals,merge_signals,neighbors
from intraday_execution import Profile,simulate
from intraday_live import PROFILE
from test_intraday_research import row,COST
from test_ensemble_research import c


class HybridTests(unittest.TestCase):
    def spec(self):return configurations()[0]
    def context(self,t,d=1):return dict(time=t,regime=7 if d==1 else 8 if d==-1 else 2)

    def test_registered_grid_is_small_and_fixed(self):
        specs=configurations();self.assertEqual(len(specs),64)
        self.assertEqual(len({s['id'] for s in specs}),64)
        for s in specs:
            self.assertEqual(s['profile']['timeframe'],'m5')
            self.assertIsNone(s['profile']['maximum_entries_daily'])
            self.assertIsNone(s['profile']['daily_loss_limit_r'])
        self.assertEqual(sum(neighbors(specs[0],s) for s in specs),4)

    def test_scap_needs_closed_fresh_m15_h1(self):
        spec=self.spec();cs=[c(3600)]
        self.assertIsNotNone(scalp_candidate(cs,self.context(3600),self.context(3600),spec,3600))
        for a,b in [(self.context(3601),self.context(3600)),(self.context(2700),self.context(3600)),
                    (self.context(3600),self.context(0)),(None,self.context(3600))]:
            self.assertIsNone(scalp_candidate(cs,a,b,spec,3600))

    def test_opposition_quality_and_neutral_policy(self):
        s=self.spec();a=self.context(3600);b=self.context(3600)
        self.assertIsNone(scalp_candidate([c(3600),c(3600,201,-1)],a,b,s,3600))
        self.assertIsNone(scalp_candidate([c(3600,q=70)],a,b,s,3600))
        self.assertIsNone(scalp_candidate([c(3600)],a,self.context(3600,-1),s,3600))
        self.assertIsNone(scalp_candidate([c(3600)],a,self.context(3600,0),s,3600))
        s=dict(s,context='nonopposed')
        self.assertIsNotNone(scalp_candidate([c(3600)],a,self.context(3600,0),s,3600))

    def test_future_context_cannot_change_past_signal(self):
        frames={'m5':({3600:[c(3600)]},{}),'m15':({}, {3600:self.context(3600)}),'h1':({}, {3600:self.context(3600)})}
        old=scalp_signals(frames,self.spec())
        frames['h1'][1][7200]=self.context(7200,-1)
        frames['m15'][1][4500]=self.context(4500,-1)
        self.assertEqual(old,scalp_signals(frames,self.spec()))

    def test_simultaneous_opposition_veto_and_intraday_priority(self):
        a={0:c(0)};b={0:dict(c(0),strategy='SCALP',timeframe='m5',supporting_models=['M5:101'])}
        both=merge_signals(a,b)
        self.assertEqual(both[0]['strategy'],'INTRADAY')
        self.assertIn('M5:101',both[0]['supporting_models'])
        b[0]['direction']=-1
        self.assertFalse(merge_signals(a,b))

    def test_shared_position_keeps_original_stop_and_hold(self):
        profiles={'INTRADAY':PROFILE,'SCALP':replace(PROFILE,timeframe='m5',maximum_hold_minutes=30,target_r=1.5)}
        signals={0:dict(c(0),strategy='SCALP',timeframe='m5'),300:dict(c(300),strategy='INTRADAY',timeframe='m30')}
        rows=[row(t,hi=101,lo=99) for t in range(0,1860,60)]
        trades,_=simulate(rows,signals,PROFILE,COST,profiles=profiles)
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]['strategy'],'SCALP')
        self.assertEqual(trades[0]['reason'],'TIME_EXIT')
        self.assertEqual(trades[0]['held_minutes'],30)

    def test_per_group_cooldown_does_not_starve_intraday(self):
        profiles={'INTRADAY':PROFILE,'SCALP':replace(PROFILE,timeframe='m5',maximum_hold_minutes=1)}
        signals={0:dict(c(0),strategy='SCALP',timeframe='m5'),600:dict(c(600),strategy='INTRADAY',timeframe='m30')}
        trades,_=simulate([row(t,hi=101,lo=99) for t in range(0,1260,60)],signals,PROFILE,COST,profiles=profiles)
        self.assertEqual([t['strategy'] for t in trades],['SCALP','INTRADAY'])

    def test_global_ten_minute_spacing_applies_across_groups(self):
        profiles={'INTRADAY':replace(PROFILE,maximum_hold_minutes=1),'SCALP':replace(PROFILE,timeframe='m5',maximum_hold_minutes=1)}
        signals={0:dict(c(0),strategy='INTRADAY'),300:dict(c(300),strategy='SCALP')}
        tr,_=simulate([row(t,hi=101,lo=99) for t in range(0,660,60)],signals,PROFILE,COST,profiles=profiles)
        self.assertEqual(len(tr),1)

    def test_intraday_only_hybrid_preserves_old_ledger(self):
        sig={0:c(0),3600:c(3600)};rows=[row(t,hi=101,lo=99) for t in range(0,15060,60)]
        old,_=simulate(rows,sig,PROFILE,COST)
        new,_=simulate(rows,merge_signals(sig,{}),PROFILE,COST,profiles={'INTRADAY':PROFILE})
        for a,b in zip(old,new):
            self.assertEqual(a,{k:v for k,v in b.items() if k not in ('strategy','timeframe','maximum_hold_minutes')})
        self.assertEqual(len(old),len(new))


if __name__=='__main__':unittest.main()
