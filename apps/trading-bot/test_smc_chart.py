import json,unittest,importlib.util,os
from pathlib import Path
from unittest.mock import patch
from smc_chart import capture,reconstruct,label_positions
from pine_engine import PineEngine
from pine_config import PineConfig
from signal_engine import Bar
from intraday_live import IntradayEngine
from test_intraday_live import pending,T
from test_ensemble_research import c


class SmcChartTest(unittest.TestCase):
    def test_labels_stay_bounded_and_separated(self):
        for anchors in ([200]*21,[850]*21,list(range(200,850,25)),[]):
            positions=label_positions(anchors,185,880)
            self.assertEqual(len(positions),len(anchors))
            self.assertTrue(all(185<=v<=880 for v in positions))
            self.assertTrue(all(b-a>=min(28,695/max(1,len(anchors)-1))-1e-9 for a,b in zip(positions,positions[1:])))

    def rows(self):
        return [(f'2026-11-19T00:{i*5:02d}:00+00:00',*ohlc) for i,ohlc in enumerate([(101,102,99,100),(100,105,100,104),(105,108,104,107),(105,106,103,105),(103,104,101,102)])]

    def test_fvg_lifecycle_reuses_engine_rules(self):
        rows=self.rows()
        gap=reconstruct(rows[:3],'M5')['gaps'][0]
        self.assertEqual((gap['bottom'],gap['top']),(102000,104000))
        self.assertFalse(gap['mitigated'])
        self.assertTrue(reconstruct(rows[:4],'M5')['gaps'][0]['mitigated'])
        self.assertEqual(reconstruct(rows,'M5')['gaps'],[])

    def test_ob_and_sweep_geometry_is_not_invented(self):
        engine=PineEngine('m5',PineConfig(tick_size=.001))
        for row in self.rows()[:2]:
            bar=Bar(*row,0);engine._market(bar);engine._structure(bar)
        engine.s.update(high_pivot=102000,trend=-1)
        bar=Bar(*self.rows()[2],0);engine._market(bar)
        event,broken=engine._structure(bar)
        self.assertEqual((event,broken),(2,102000))
        self.assertEqual((engine.s['blocks'][0]['top'],engine.s['blocks'][0]['bottom']),(102000,99000))
        snapshot=capture(engine.s);engine.s['blocks'][0]['top']=1
        self.assertEqual(snapshot['blocks'][0]['top'],102000)
        engine.s['liquidity']=[dict(id=99,side=-1,bar=1,level=106000,swept=False,terminal=False)]
        bar=Bar('2026-11-19T00:15:00+00:00',105,107,104,105,0)
        engine._market(bar);engine._structure(bar)
        self.assertTrue(next(p for p in engine.s['liquidity'] if p['id']==99)['swept'])

    def test_snapshot_survives_restart_entry_and_is_not_live_reference(self):
        e=IntradayEngine()
        e.engines['m30'].s['gaps']=[dict(side=1,id=3,bar=0,top=100000,bottom=99000,mitigated=False,terminal=False)]
        with patch('intraday_live.advance_subtypes',return_value=({},[],{})),patch('intraday_live.combine',side_effect=[{T:c(T)},{}]):e.bar('m30',None)
        saved=capture(e.engines['m30'].s)
        e.engines['m30'].s['gaps'].clear()
        e=IntradayEngine(json.loads(json.dumps(e.snapshot())))
        opened=e.tick(T,100,100.24)[0]
        self.assertEqual(opened['position']['chart_context'],saved)
        opened['position']['chart_context']['gaps'].clear()
        self.assertTrue(e.s['position']['chart_context']['gaps'])

    def test_decision_and_state_equal_previous_release_except_chart_metadata(self):
        source=Path(__file__).parent/'fixtures/chart_engine.py'
        spec=importlib.util.spec_from_file_location('chart_baseline',source);old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
        def clean(v):
            if isinstance(v,dict):return {k:clean(x) for k,x in v.items() if k not in ('chart_context','config_hash')}
            if isinstance(v,list):return [clean(x) for x in v]
            return v
        for direction in (-1,1):
            a=old.IntradayEngine();b=IntradayEngine()
            a.s['pending']=pending(direction=direction);b.s['pending']=dict(a.s['pending'],chart_context=capture(b.engines['m30'].s))
            for stamp,bid,ask in [(T,100,100.24),(T+1,101,101.24),(T+2,98,98.24)]:
                self.assertEqual(clean(a.tick(stamp,bid,ask)),clean(b.tick(stamp,bid,ask)))
                self.assertEqual(clean(a.snapshot()),clean(b.snapshot()))


if __name__=='__main__':unittest.main()
