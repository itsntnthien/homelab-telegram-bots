from contextlib import closing
import unittest,tempfile,sqlite3,json,io,os
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone
from setup_view import lookup,explain,keyboard,candles,render,image_reasons,chart_indicators
from trades import TradeStore,entry_message
import bot

class SetupViewTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=TradeStore(self.root/'state.db')
  self.trade,_=self.store.create(event_key='setup',timeframe='M5',signal='MAIN SELL',entry=100,sl=102,tp=97,
   metadata=dict(engine_version='xau-intraday-scalp-combined-v2',strategy='SCALP',quality=96,family=3,subtype=301,
                 confirmation_time='2026-11-19T04:20:00+00:00',signal_created_time='2026-11-19T04:20:01+00:00',maximum_hold_minutes=30))
  self.store.create_user_signal('42',self.trade)
  self.store.record_trade_event(event_key='e',event_type='PRIMARY_OPEN',event_time='2026-11-19T04:20:01+00:00',trade_id=self.trade.id,
   payload=dict(position=dict(deferred=True,supporting_models=['M5:301','H1:CONTEXT'],supporting_confirmations=[dict(timeframe='h1',confirmed=1795060800)])))
  self.r=lookup(self.store.db,'42',str(self.trade.id));self.market=self.root/'market.db'
  with closing(sqlite3.connect(self.market,isolation_level=None)) as d:
   d.execute('CREATE TABLE bars(symbol,timeframe,timestamp,open,high,low,close,is_closed,price_scale)')
   for minute,closed in [(10,1),(15,1),(20,1),(25,0)]:
    d.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?)',('XAUUSD','m5',f'2026-11-19T04:{minute}:00+00:00',100,101,99,100.5,closed,1))
 def tearDown(self):self.store.close();self.tmp.cleanup()
 def test_chat_scope_and_malformed_id(self):
  for bad in ('1 OR 1=1','-1','９','9'*13,'',None):self.assertIsNone(lookup(self.store.db,'42',bad))
  self.assertIsNone(lookup(self.store.db,'43',str(self.trade.id)))
 def test_short_card_and_evidence(self):
  text=entry_message(self.trade,7);self.assertIn('#7',text);self.assertLess(len(text),500)
  details=explain(self.r);self.assertIn('H1:CONTEXT',details);self.assertIn('hồi',details);self.assertIn('không phải tín hiệu vào lại',details)
  self.r['evidence']={};self.assertIn('Không có snapshot',explain(self.r))
 def test_no_future_candles_readonly_and_png(self):
  rows=candles(self.market,self.r);self.assertEqual(len(rows),2);self.assertTrue(rows[-1][0].endswith('04:15:00+00:00'))
  blob=render(self.r,rows)
  from PIL import Image
  with Image.open(io.BytesIO(blob)) as img:self.assertEqual(img.size,(1800,1100))
  self.assertTrue(blob.startswith(b'\x89PNG'))
 def test_gap_at_confirmation_not_misrepresented(self):
  with closing(sqlite3.connect(self.market,isolation_level=None)) as d:d.execute("DELETE FROM bars WHERE timestamp LIKE '%04:15:%'")
  self.assertEqual(candles(self.market,self.r),[])
 def test_callback_unauthorized_never_uploads(self):
  tg=bot.Telegram('fake','43',chat_ids=['43']);sent=[]
  with patch.object(tg,'send_to',side_effect=lambda *a,**k:sent.append(a)),patch.object(tg,'send_photo') as upload:
   tg.setup_details(self.store.db,'43','chart:'+str(self.trade.id),123)
   upload.assert_not_called();self.assertIn('Không tìm thấy',sent[0][1])
 def test_chart_callback_and_missing_data_fallback(self):
  tg=bot.Telegram('fake','42',chat_ids=['42'])
  with patch.dict(os.environ,MARKET_DB=str(self.market)),patch.object(tg,'send_photo',return_value=91) as upload:
   self.assertEqual(tg.setup_details(self.store.db,'42','chart:'+str(self.trade.id),123),91)
   self.assertEqual(upload.call_args.args[3],123)
  with patch.dict(os.environ,MARKET_DB=str(self.root/'missing.db')),patch.object(tg,'send_to') as sent:
   tg.setup_details(self.store.db,'42','chart:'+str(self.trade.id),123);self.assertIn('Chưa dựng',sent.call_args.args[1])
 def test_photo_multipart_and_timeout_no_retry(self):
  tg=bot.Telegram('fake','42',chat_ids=['42']);response=unittest.mock.MagicMock()
  response.__enter__.return_value=io.BytesIO(b'{"ok":true,"result":{"message_id":91}}')
  with patch.object(bot.urllib.request,'urlopen',return_value=response) as http:
   self.assertEqual(tg.send_photo('42',b'PNG','TEST',123),91)
   req=http.call_args.args[0];self.assertIn(b'PNG',req.data);self.assertIn(b'reply_parameters',req.data);self.assertTrue(req.full_url.endswith('sendPhoto'))
  with patch.object(bot.urllib.request,'urlopen',side_effect=TimeoutError) as http:
   with self.assertRaises(TimeoutError):tg.send_photo('42',b'PNG','TEST',123)
   self.assertEqual(http.call_count,1)
 def test_image_reasons_are_recorded_not_inferred(self):
  self.r['evidence']['position']['supporting_confirmations']=[dict(timeframe='m15',regime=8),dict(timeframe='h1',regime=8)]
  body=str(image_reasons(self.r));self.assertIn('M15: xu hướng giảm',body);self.assertIn('H1: xu hướng giảm',body);self.assertIn('retest',body)
  self.r['evidence']={};self.assertIn('Chưa lưu',str(image_reasons(self.r)))
  with self.assertRaises(ValueError):render(self.r,[])
 def test_long_evidence_expands_image(self):
  self.r['evidence']['position']['supporting_confirmations']=[dict(timeframe=tf,subtype=sub) for tf in ('m30','h1') for sub in (101,102,201,202,301,302)]
  from PIL import Image
  with Image.open(io.BytesIO(render(self.r,candles(self.market,self.r)))) as img:self.assertEqual(img.size,(1800,1100))
 def test_buy_image_and_narrow_levels(self):
  self.r.update(signal='MAIN BUY',entry=100,sl=99.999,tp=100.001)
  self.assertTrue(render(self.r,candles(self.market,self.r)).startswith(b'\x89PNG'))
 def test_indicator_math_and_no_future_pivot(self):
  rows=[('2026-11-19T00:00:00+00:00',100,101,99,100) for _ in range(25)]
  bands,pivots=chart_indicators(rows)
  self.assertEqual(bands[0],(19,100,100,100));self.assertEqual(pivots,[])
  rows[10]=(rows[10][0],100,105,99,100)
  self.assertFalse(chart_indicators(rows[:15])[1])
  self.assertIn(('H',10,15,105),chart_indicators(rows[:16])[1])
 def test_keyboard_small_stable(self):
  for b in keyboard(self.trade.id)['inline_keyboard'][0]:self.assertLess(len(b['callback_data'].encode()),64)

if __name__=='__main__':unittest.main()
