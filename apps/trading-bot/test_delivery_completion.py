import json, tempfile, unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from trades import TradeStore, Trade, update_message
from unittest.mock import patch
from contextlib import nullcontext
from threading import Event, Thread
import sqlite3,time
import bot,delivery,telegram_views

class CompletionTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=TradeStore(Path(self.tmp.name)/'state.db')
 def tearDown(self):
  self.store.close();self.tmp.cleanup()
 def test_unknown_parent_does_not_leave_exit_pending_forever(self):
  s=self.store;s.queue_notification('entry','ENTRY','42');s.notification_uncertain('entry','timeout')
  s.queue_notification('exit','EXIT','42',depends_on='entry')
  s.pending_notifications()
  row=s.db.execute("SELECT * FROM notifications WHERE event_key='exit'").fetchone()
  self.assertIsNone(row['depends_on'])
  self.assertIn('ĐỐI SOÁT',row['body'])
 def test_expired_parent_suppresses_exit(self):
  s=self.store;s.queue_notification('entry','ENTRY','42',expires_at='2000-01-01T00:00:00+00:00')
  s.queue_notification('exit','EXIT','42',depends_on='entry');s.pending_notifications()
  self.assertEqual(s.db.execute("SELECT state FROM notifications WHERE event_key='exit'").fetchone()[0],'SUPPRESSED')
 def test_exit_label_does_not_invent_four_hours(self):
  t=Trade(1,'e','s',1,'M30','MAIN BUY',100,99,103,'TIME_EXIT')
  self.assertNotIn('4 giờ',update_message(t))
 def test_tp2_success_icon(self):
  t=Trade(1,'e','s',1,'M30','MAIN BUY',100,99,103,'TP2')
  self.assertIn('✅',update_message(t))

 def fixture(self,d=1):
  now=datetime.now(timezone.utc)
  t,_=self.store.create(event_key='trade',timeframe='M30',signal='MAIN BUY' if d==1 else 'MAIN SELL',entry=100,sl=100-d*5,tp=100+d*15,
    metadata=dict(strategy='Q100',maximum_hold_minutes=360,entry_atr=4,engine_version='xau-intraday-combined-q100-v1'))
  config=dict(trade_id=t.id,label='#1 TEST',direction=d,entry=100,sl=t.sl,tp=t.tp,initial_risk=5.02,atr=4,
    maximum_chase_atr=.1,minimum_net_rr=1.2,slippage=.02,commission=0,spread_floor=.3,market_path='not-used')
  self.store.queue_notification('entry','TEST ENTRY','42',expires_at=(now+timedelta(seconds=5)).isoformat(),delivery=config)
  return now,config

 def row(self,key='entry'):
  return self.store.db.execute('SELECT * FROM notifications WHERE event_key=?',(key,)).fetchone()

 def test_buy_and_sell_chase_rr_geometry_and_quote_age(self):
  now,c=self.fixture()
  for d in (1,-1):
   conf=dict(c,direction=d,sl=100-d*5,tp=100+d*15)
   q=(now.isoformat(),now.isoformat(),99.9,100.1)
   self.assertEqual(delivery.assess(conf,q,now)[0],'SEND')
   moved=(100.6,100.8) if d==1 else (99.2,99.4)
   self.assertEqual(delivery.assess(conf,(*q[:2],*moved),now)[0],'SKIP')
   self.assertEqual(delivery.assess(conf,( (now-timedelta(seconds=2)).isoformat(),*q[1:]),now)[0],'WAIT')
   self.assertEqual(delivery.assess(conf,( (now+timedelta(seconds=1)).isoformat(),*q[1:]),now)[0],'WAIT')
   self.assertEqual(delivery.assess(conf,(*q[:2],101,100),now)[0],'WAIT')
   short=dict(conf,tp=100+d*2)
   self.assertEqual(delivery.assess(short,q,now)[0],'SKIP')
   target=conf['tp'];self.assertEqual(delivery.assess(conf,(*q[:2],target,target+.1),now)[0],'SKIP')

 def test_skip_one_notice_no_trade_mutation_or_duplicate(self):
  now,c=self.fixture();before=tuple(self.store.db.execute('SELECT * FROM trades').fetchone())
  with patch.object(delivery,'latest_quote',return_value=(now.isoformat(),now.isoformat(),101,101.2)):
   self.assertIsNone(delivery.prepare(self.store,self.row(),now))
  self.store.pending_notifications();self.store.pending_notifications()
  self.assertEqual(self.store.notification_counts(),{'EXPIRED':1,'PENDING':1})
  self.assertIn('ĐÃ BỎ LỠ',self.row('missed:entry')['body'])
  self.assertEqual(before,tuple(self.store.db.execute('SELECT * FROM trades').fetchone()))

 def test_stale_wait_then_expire_and_no_recovery_spam(self):
  now,c=self.fixture()
  with patch.object(delivery,'latest_quote',return_value=None):
   self.assertIsNone(delivery.prepare(self.store,self.row(),now))
  self.assertEqual(self.row()['state'],'PENDING')
  with patch.object(delivery,'utcnow',return_value=now+timedelta(seconds=70)):
   self.store.pending_notifications()
  self.assertEqual(self.store.notification_counts(),{'EXPIRED':1})

 def test_closed_before_send_is_missed(self):
  now,c=self.fixture()
  self.store.db.execute("UPDATE trades SET status='SL'");self.store.db.commit()
  self.assertIsNone(delivery.prepare(self.store,self.row(),now))
  self.assertEqual(self.row()['last_error'],'Vị thế mô phỏng đã đóng')

 def test_reply_and_ack_then_restart_no_duplicate(self):
  self.store.queue_notification('entry','ENTRY','42');self.store.notification_sent('entry',123)
  self.store.queue_notification('exit','EXIT','42',depends_on='entry')
  tg=bot.Telegram('fake','42',chat_ids=['42']);calls=[]
  with patch.object(tg,'sending_slot',return_value=nullcontext()),patch.object(tg,'call',side_effect=lambda m,p,**k:(calls.append(p) or {'result':{'message_id':124}})):
   bot.deliver_pending(self.store,tg)
   path=self.store.path;self.store.close();self.store=TradeStore(path)
   self.store.recover_notifications();bot.deliver_pending(self.store,tg)
  self.assertEqual(len(calls),1)
  self.assertEqual(json.loads(calls[0]['reply_parameters']),{'message_id':123,'allow_sending_without_reply':True})

 def test_retry_after_expiry_does_not_send_entry(self):
  now,c=self.fixture()
  self.store.notification_failed('entry','429',retry_after=30)
  with patch.object(delivery,'utcnow',return_value=now+timedelta(seconds=6)):
   pending=self.store.pending_notifications()
  self.assertEqual([r['event_key'] for r in pending],['missed:entry'])

 def test_metadata_survives_restart_and_q100_label(self):
  self.fixture();path=self.store.path;self.store.close();self.store=TradeStore(path)
  t=self.store._trade(self.store.db.execute('SELECT * FROM trades').fetchone())
  from dataclasses import replace
  self.assertIn('360 phút',update_message(replace(t,status='TIME_EXIT')))
  self.assertEqual(t.strategy,'Q100')

 def test_health_recent_error_not_historical_unknown(self):
  state=dict(ready=True,market_closed=False)
  workers=dict(alive=True,workers={})
  self.assertTrue(telegram_views.health(state,dict(telegram_unknown=34),workers)['ready'])
  self.assertFalse(telegram_views.health(state,dict(delivery_new_unknown=1),workers)['ready'])
  self.assertFalse(telegram_views.health(state,dict(telegram_oldest_pending_sec=10),workers)['ready'])

 def test_stats_excludes_old_engine_and_missing_r(self):
  self.fixture();t=self.store._trade(self.store.db.execute('SELECT * FROM trades').fetchone())
  self.store.create_user_signal('42',t)
  self.store.db.execute("UPDATE trades SET status='TP',realized_r=2");self.store.db.commit()
  text=telegram_views.stats(self.store.db,'42',t.session,t.engine_version)
  self.assertIn('Win 100.0%',text);self.assertIn('+2.000R',text)
  other=telegram_views.stats(self.store.db,'42',t.session,'OLD')
  self.assertNotIn('+2.000R',other)
  hist,_=telegram_views.history(self.store.db,'42',1)
  self.assertIn('100.000',hist);self.assertIn('Q100',hist)

 def test_100_isolated_deliveries_no_duplicates(self):
  from types import SimpleNamespace
  from unittest.mock import Mock
  for i in range(100): self.store.queue_notification('sample:'+str(i),'TEST','42')
  tg=SimpleNamespace(send_to=Mock(side_effect=range(1,101)))
  begin=time.perf_counter()
  for _ in range(2): bot.deliver_pending(self.store,tg,limit=100)
  self.assertEqual(tg.send_to.call_count,100)
  self.assertEqual(self.store.notification_counts(),{'SENT':100})
  self.assertLess(time.perf_counter()-begin,10)

 def test_slow_chat_does_not_block_other_chat(self):
  from types import SimpleNamespace
  entered=Event();release=Event();fast=Event();stop=Event()
  self.store.queue_notification('slow','slow','1');self.store.queue_notification('fast','fast','2')
  def send(chat,text):
   if chat=='1': entered.set();release.wait(4)
   else: fast.set()
   return 1
  tg=SimpleNamespace(chat_ids=['1','2'],send_to=send)
  with patch.dict(__import__('os').environ,{'TELEGRAM_TEST_STATE_DB':'','TELEGRAM_TEST_CHAT_ID':''}):
   thread=Thread(target=bot.notification_worker,args=(self.store.path,tg,stop));thread.start()
   try:
    self.assertTrue(entered.wait(2));self.assertTrue(fast.wait(1))
   finally:
    release.set();stop.set();thread.join(5)
  self.assertFalse(thread.is_alive())

 def test_existing_intraday_tick_behavior_preserved(self):
  import importlib.util
  from copy import deepcopy
  from test_intraday_live import pending,T
  import intraday_live
  source=Path(__file__).parent/'fixtures/combined_engine.py'
  spec=importlib.util.spec_from_file_location('frozen_live_compare',source);old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
  def stripped(x):
   if isinstance(x,dict): return {k:stripped(v) for k,v in x.items() if k not in ('entry_atr','timeframe','config_hash','m5','m15')}
   if isinstance(x,list): return [stripped(v) for v in x]
   return x
  for name in ('CURRENT','Q100'):
   self.assertEqual(old.COMPONENTS[name],intraday_live.COMPONENTS[name])
  for direction in (1,-1):
   for strategy in ('CURRENT','Q100'):
    a=old.IntradayEngine();b=intraday_live.IntradayEngine()
    item=pending(T,direction);item['strategy']=strategy
    a.s['pending']=deepcopy(item);b.s['pending']=deepcopy(item)
    for offset,bid,ask in [(0.2,100,100.24),(1,100.1,100.34),(2,100.2,100.44),(360,102,102.24),(400,98,98.24),(22000,100,100.24)]:
     self.assertEqual(stripped(a.tick(T+offset,bid,ask)),stripped(b.tick(T+offset,bid,ask)))
     self.assertEqual(stripped(a.snapshot()),stripped(b.snapshot()))
     b=intraday_live.IntradayEngine(b.snapshot())

 def test_claim_delay_cannot_send_expired_entry(self):
  now,c=self.fixture()
  tg=bot.Telegram('fake','42',chat_ids=['42'])
  original=self.store.claim_notification
  def claim(key):
   result=original(key)
   self.store.db.execute("UPDATE notifications SET expires_at=? WHERE event_key=?",((now-timedelta(seconds=1)).isoformat(),key));self.store.db.commit()
   return result
  with patch.object(tg,'sending_slot',return_value=nullcontext()),patch.object(tg,'call') as call,patch.object(self.store,'claim_notification',side_effect=claim),patch.object(delivery,'latest_quote',return_value=(now.isoformat(),now.isoformat(),99.9,100.1)):
   bot.deliver_pending(self.store,tg)
   call.assert_not_called()

 def test_intraday_commands_are_readable_and_reject_old_timeframe(self):
  import os
  stop=Event();calls=[]
  commands=['/start','/status','/open','/history','/stats','/news','/settings','/help']
  updates=[{'update_id':i+1,'message':{'chat':{'id':42},'text':c}} for i,c in enumerate(commands)]
  updates.append({'update_id':9,'callback_query':{'id':'old','data':'tf:m1','message':{'chat':{'id':42},'message_id':1}}})
  self.store.ensure_users(['42'],'XAUUSD');before=self.store.user_enabled('42','XAUUSD','m1')
  def call(method,payload,timeout=None):
   if method=='getUpdates':stop.set();return {'result':updates}
   calls.append((method,payload));return {'result':{'message_id':12}}
  with patch.dict(os.environ,{'SIGNAL_ENGINE':'intraday-live','TELEGRAM_CHAT_IDS':'42','TELEGRAM_TEST_CHAT_ID':'','TELEGRAM_TEST_STATE_DB':''}),patch.object(bot,'latest_quote',return_value=None):
   tg=bot.Telegram('fake','42',chat_ids=['42'])
   with patch.object(tg,'call',side_effect=call),patch.object(tg,'sending_slot',return_value=nullcontext()):tg.poll(self.store,stop)
  self.assertEqual(self.store.checkpoint('telegram_update_id'),'10')
  self.assertEqual(before,self.store.user_enabled('42','XAUUSD','m1'))
  messages=[p['text'] for _,p in calls if 'text' in p]
  self.assertTrue(any('không còn hỗ trợ' in x for x in messages))
  self.assertTrue(any('Q100 không có nghĩa thắng 100%' in x or 'Q100 là điểm chất lượng' in x for x in messages))
  self.assertTrue(all(len(x.encode('utf-16-le'))//2<4096 for x in messages))

 def test_atomic_claim_and_rollback_survive_reopen(self):
  s=self.store;s.queue_notification('once','TEST','42')
  other=TradeStore(s.path)
  try:
   self.assertTrue(s.claim_notification('once'));self.assertFalse(other.claim_notification('once'))
   other.recover_notifications();self.assertEqual(self.row('once')['state'],'UNKNOWN')
   with self.assertRaises(ValueError):
    with s.transaction():
     s.queue_notification('rollback','TEST','42');s.set_checkpoint('cursor','99');raise ValueError('injected')
   self.assertIsNone(self.row('rollback'));self.assertIsNone(s.checkpoint('cursor'))
  finally:other.close()

 def test_news_deadline_and_missing_source_are_explicit(self):
  import os
  path=Path(self.tmp.name)/'news.db';db=sqlite3.connect(path)
  now=datetime.now(timezone.utc);event=(now+timedelta(minutes=15)).isoformat()
  db.executescript('CREATE TABLE news(id,event_id,event_time,title,source,impact,actual,forecast,previous,status,updated_at); CREATE TABLE news_source_status(source,status,last_sync,last_error,event_count);')
  db.execute('INSERT INTO news VALUES(?,?,?,?,?,?,?,?,?,?,?)',(1,'news-id',event,'Test event','BLS','HIGH',None,'1',None,'SCHEDULED',now.isoformat()))
  db.execute('INSERT INTO news_source_status VALUES(?,?,?,?,?)',('CENSUS','UNAVAILABLE',now.isoformat(),'timeout',0));db.commit();db.close()
  self.store.ensure_users(['42'],'XAUUSD')
  with patch.dict(os.environ,{'SIGNAL_ENGINE':'intraday-live'}):
   bot.queue_news_alerts(self.store,path);text=bot.news_today(path)
  row=self.store.db.execute("SELECT * FROM notifications WHERE event_key LIKE 'news:%'").fetchone()
  self.assertEqual(row['expires_at'],event)
  self.assertIn('CENSUS',text);self.assertIn('Lịch chưa đầy đủ',text)

 def test_default_news_on_is_persisted_without_resetting_off(self):
  self.store.ensure_users(['42'],'XAUUSD')
  self.assertEqual(self.store.db.execute('SELECT enabled FROM user_news_settings WHERE chat_id=?',('42',)).fetchone()[0],1)
  self.store.set_news_enabled('42',False);self.store.ensure_users(['42'],'XAUUSD')
  self.assertFalse(self.store.news_enabled('42'))

 def test_mobile_menu_has_nine_functions_including_personal_risk_and_safe_html(self):
  tg=bot.Telegram('fake','42',chat_ids=['42']);sent=[]
  with patch.object(tg,'call',side_effect=lambda m,p,**k:(sent.append(p) or {'result':{'message_id':1}})),patch.object(tg,'sending_slot',return_value=nullcontext()):tg.home('42')
  markup=json.loads(sent[0]['reply_markup']);self.assertEqual(sum(len(row) for row in markup['keyboard']),9)
  self.assertIn('💰 Vốn & rủi ro',[button['text'] for row in markup['keyboard'] for button in row])
  self.assertEqual(sent[0]['parse_mode'],'HTML')
  html=telegram_views.rich_text('Title <unsafe>\nEntry: 123.456\n<&>')
  self.assertIn('&lt;unsafe&gt;',html);self.assertIn('<code>123.456</code>',html);self.assertNotIn('<unsafe>',html)

 def test_quote_ahead_of_committed_engine_waits_for_exit_processing(self):
  now,c=self.fixture();c['engine_version']='TEST-VERSION'
  self.store.db.execute('UPDATE notifications SET delivery_json=?',(json.dumps(c),));self.store.db.commit()
  q=(now.isoformat(),now.isoformat(),99.9,100.1)
  with patch.object(delivery,'latest_quote',return_value=q):
   self.assertIsNone(delivery.prepare(self.store,self.row(),now))
   self.store.save_engine_state('XAUUSD','M30-H1','TEST-VERSION',None,{'last_tick':now.timestamp()})
   self.assertIsNotNone(delivery.prepare(self.store,self.row(),now))

 def test_preflight_wait_does_not_consume_telegram_rate_budget(self):
  self.fixture();tg=bot.Telegram('fake','42',chat_ids=['42'])
  with patch.object(delivery,'latest_quote',return_value=None),patch.object(tg,'call') as call:
   bot.deliver_pending(self.store,tg)
   call.assert_not_called();self.assertLessEqual(tg._route_next['42'],time.monotonic())
