import unittest
from ensemble_research import configurations,combine
from trades import MODEL_NAMES
from test_pine_contract import candidate
from test_intraday_research import row,COST
from intraday_execution import Profile,simulate


def c(t,sub=101,d=1,q=90):
    return candidate(subtype=sub,family=sub//100,direction=d,slot=(sub//100-1)*2+(d==-1),
                     quality=q,time=t,entry=100.,sl=98. if d==1 else 102.,atr=2.,model_id=sub)


def streams(items,tf='m15'):
    return {(sub,frame):{x['time']:x for x in items if x['subtype']==sub} if frame==tf else {}
            for sub in MODEL_NAMES for frame in ('m15','m30','h1')}


def spec(rule='best_unopposed',members=None):
    return dict(members=members or list(MODEL_NAMES),rule=rule,profile=dict(timeframe='m15',minimum_quality=75))


class EnsembleTests(unittest.TestCase):
    def test_registered_grid_has_728_combinations_and_72_controls(self):
        grid=configurations()
        self.assertEqual(len(grid),800);self.assertEqual(sum(x['control'] for x in grid),72)
        self.assertEqual(len({x['id'] for x in grid}),800)
        self.assertTrue(all(x['profile']['maximum_entries_daily'] is None for x in grid))

    def test_conflicting_current_candidates_are_vetoed(self):
        self.assertFalse(combine(streams([c(900),c(900,201,-1)]),spec()))

    def test_quality_gate_precedes_voting_and_subset_excludes_other_models(self):
        ss=streams([c(900,101,q=70),c(900,201)])
        self.assertFalse(combine(ss,spec('agree2_samebar')))
        self.assertEqual(set(combine(ss,spec(members=[201]))),{900})

    def test_choch_double_label_does_not_count_as_diverse_evidence(self):
        ss=streams([c(900,201),c(900,301)])
        self.assertTrue(combine(ss,spec('agree2_samebar')))
        self.assertFalse(combine(ss,spec('diverse2_recent')))

    def test_recent_vote_expires_or_invalidates_and_never_triggers_alone(self):
        ss=streams([c(900,101),c(1800,201),c(3600,301)])
        self.assertEqual(set(combine(ss,spec('diverse2_recent'))),{1800})
        invalid=c(900,101);invalid['sl']=101
        self.assertFalse(combine(streams([invalid,c(1800,201)]),spec('diverse2_recent')))
        self.assertFalse(combine(streams([c(900,101)]),spec('agree2_recent')))

    def test_h1_confirmation_no_future_stale_or_opposed_votes(self):
        ss=streams([c(1800),c(3600),c(7200)])
        ss[201,'h1']={3600:c(3600,201)}
        got=combine(ss,spec('h1_support'))
        self.assertEqual(set(got),{3600})
        self.assertIn('H1:201',got[3600]['supporting_models'])
        ss[302,'h1']={3600:c(3600,302,-1)}
        self.assertFalse(combine(ss,spec('h1_support')))

    def test_prefix_invariance(self):
        all_=streams([c(900),c(1800,201),c(2700,302,-1)])
        before={k:{t:v for t,v in x.items() if t<=1800} for k,x in all_.items()}
        for rule in ('best_unopposed','agree2_samebar','agree2_recent','diverse2_recent','h1_support'):
            self.assertEqual(combine(before,spec(rule)),{t:c for t,c in combine(all_,spec(rule)).items() if t<=1800})

    def test_combination_uses_one_position_and_persists_evidence(self):
        sig=combine(streams([c(0),c(0,201),c(60,301)]),spec())
        tr,_=simulate([row(0,hi=100.5,lo=99.5),row(60,hi=100.5,lo=99.5),row(120,hi=100.5,lo=99.5)],
                      sig,Profile(),COST)
        self.assertEqual(len(tr),1)
        self.assertEqual(tr[0]['supporting_models'],['M15:101','M15:201'])


if __name__=='__main__':unittest.main()
