import json
import io
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch
from pathlib import Path
from datetime import datetime,timezone,timedelta
from exness_collector import connect,accept,schedule
import exness_collector


class ExnessCollectorTest(unittest.TestCase):
    def setUp(self):
        self.db=connect(':memory:')
        self.addCleanup(self.db.close)
        self.now=datetime.now(timezone.utc)
        self.received=int(self.now.timestamp()*1000)
    def quote(self,at,bid=4000,ask=4000.24):
        return dict(type='quote',receivedAt=self.received,timestamp=at.isoformat(),bid=bid,ask=ask)
    def test_order_dedupe_spike_and_atomic_latest(self):
        accept(self.db,self.quote(self.now))
        accept(self.db,self.quote(self.now))
        accept(self.db,self.quote(self.now-timedelta(seconds=1),3990))
        accept(self.db,self.quote(self.now+timedelta(milliseconds=1),3999))
        accept(self.db,self.quote(self.now+timedelta(milliseconds=2)))
        self.assertEqual([r[0] for r in self.db.execute('SELECT bid FROM tick_events ORDER BY id')],[4000,3999,4000])
        with self.assertRaises(ValueError):accept(self.db,self.quote(self.now,bid=4001,ask=4000))
        self.assertEqual(self.db.execute('SELECT bid FROM latest_ticks').fetchone()[0],4000)
    def test_six_frames_closed_only_and_rollback(self):
        for tf,seconds in dict(m1=60,m3=180,m5=300,m15=900,m30=1800,h1=3600).items():
            start=int(self.now.timestamp())//seconds*seconds
            bars=[dict(time=datetime.fromtimestamp(start-seconds,timezone.utc).isoformat(),open=4000,high=4002,low=3999,close=4001,volume=10),
                  dict(time=datetime.fromtimestamp(start,timezone.utc).isoformat(),open=4000,high=4002,low=3999,close=4001,volume=10)]
            e=dict(type='history',receivedAt=self.received,timeframe=tf,bars=bars)
            accept(self.db,e);accept(self.db,e)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM bars WHERE timeframe=?',(tf,)).fetchone()[0],1)
            bars[0]['close']=4000;bars[1]['low']=5000
            with self.assertRaises(ValueError):accept(self.db,e)
            self.assertEqual(self.db.execute('SELECT close FROM bars WHERE timeframe=?',(tf,)).fetchone()[0],4001)
    def test_metadata_calendar_and_foreign_provider_rejected(self):
        c=dict(instrument='XAUUSD',point_digits=3,trade_sessions=[dict(trade_mode='enabled',week_start_time='Mon, 23:00:30',week_end_time='Mon, 23:59:59')])
        s=schedule(c,datetime(2026,11,18,tzinfo=timezone.utc))
        self.assertEqual(s['scheduleTimeZone'],'America/New_York')
        self.assertEqual(s['schedule'][0]['endSecond'],154800)
        self.db.execute("UPDATE market_meta SET value='ctrader' WHERE key='provider'")
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'foreign.db';dest=sqlite3.connect(p);self.db.commit();self.db.backup(dest);dest.close()
            with self.assertRaises(ValueError):connect(p)
    def test_future_and_stale_transport_rejected(self):
        with self.assertRaises(ValueError):accept(self.db,self.quote(self.now+timedelta(seconds=10)))
        e=self.quote(self.now);e['receivedAt']-=31000
        with self.assertRaises(ValueError):accept(self.db,e)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM tick_events').fetchone()[0],0)

    def conditions(self,start='Mon, 23:00:30',end='Mon, 23:59:59'):
        return dict(instrument='XAUUSD',point_digits=3,trade_sessions=[
            dict(trade_mode='enabled',week_start_time=start,week_end_time=end)])

    def test_session_formats_are_equivalent(self):
        observed=datetime(2026,11,18,tzinfo=timezone.utc)
        expected=schedule(self.conditions(),observed)
        for start,end in [('Mon 23:00:30','Mon 23:59:59'),
                          (' Mon,23:00:30 ',' Mon , 23:59:59 '),
                          ('Mon\t23:00:30','Mon  23:59:59')]:
            with self.subTest(start=start):
                self.assertEqual(schedule(self.conditions(start,end),observed),expected)

    def test_session_rejects_bad_boundaries(self):
        for value in (None,123,'','Monday 23:00:30','Mon 24:00:00','Mon 23:60:00',
                      'Mon 23:00:60','Mon 3:00:30','Mon 23:00:30 trailing'):
            for field in ('week_start_time','week_end_time'):
                with self.subTest(value=value,field=field):
                    c=self.conditions();c['trade_sessions'][0][field]=value
                    with self.assertRaises(ValueError):schedule(c)
        c=self.conditions();del c['trade_sessions'][0]['week_start_time']
        with self.assertRaises(ValueError):schedule(c)

    def test_invalid_metadata_preserves_last_valid_calendar(self):
        accept(self.db,dict(type='metadata',receivedAt=self.received,conditions=self.conditions()))
        before=list(self.db.execute('SELECT key,value FROM market_meta ORDER BY key'))
        with self.assertRaises(ValueError):
            accept(self.db,dict(type='metadata',receivedAt=self.received,
                                conditions=self.conditions('Mon 24:00:00')))
        self.assertEqual(list(self.db.execute('SELECT key,value FROM market_meta ORDER BY key')),before)

    def test_invalid_schedule_stops_collector_and_terminates_transport(self):
        event=dict(type='metadata',receivedAt=self.received,conditions=self.conditions('invalid'))
        process=Mock(stdout=io.StringIO(json.dumps(event)+'\n'))
        process.poll.return_value=None
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'market-exness.db'
            env={'EXNESS_MARKET_DB':str(target),'EXNESS_TRANSPORT_COMMAND':'["synthetic-transport"]'}
            with patch.dict(os.environ,env),patch.object(exness_collector.subprocess,'Popen',return_value=process), \
                 patch.object(exness_collector.signal,'signal'),patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(exness_collector.main(),78)
            self.assertEqual(json.loads(output.getvalue()),dict(type='status',state='blocked',code='INVALID_SCHEDULE'))
            process.terminate.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=10)
            self.assertFalse((Path(folder)/'exness-schedule.json').exists())
