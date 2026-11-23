from contextlib import contextmanager
from threading import RLock, Lock
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path
try:
    import fcntl
except ImportError:
    fcntl = None
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from zoneinfo import ZoneInfo

from signal_engine import Bar, load_bars, replay_events, to_price
from release1_engine import replay_release1
from signal_engine import RELEASE_CONFIG_HASH, RELEASE_ENGINE_VERSION
from market_quality import inspect_bars, utc_timestamp, market_is_open
from market_migration import migrate_market_db
from trades import TradeStore, entry_message, info_message, session_key, update_message
from latency import slippage_observation

STATUS = {"ok": False, "started_at": datetime.now(timezone.utc).isoformat(), "error": None}
START_MONOTONIC = time.monotonic()
SYMBOL = os.environ.get("SYMBOL", "XAUUSD")
NEWS_DB = os.environ.get("NEWS_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "news.db")
VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")
BAR_CACHE = {}
METRICS = {"last_signal_cycle_ms": 0, "last_tick_age": None, "bars": {},
           "telegram_pending": 0, "telegram_failed": 0, "telegram_unknown": 0,
           "telegram_dead": 0, "signals_detected": 0, "signals_created": 0, "last_signal_at": None,
           "signals_duplicate": 0, "signals_skipped_stale": 0, "heartbeats": {},
           "tick_received_at": None, "tick_provider_time": None,
           "tick_reaction_ms": 0, "signal_latency": {},
           "slippage": {"warnings": 0, "last_delta": None},
           "early_warning": {"sent": 0, "deduplicated": 0}}
STALE_LOG_AT = {}
SIGNAL_SCAN_AT = {}
BAR_HEALTH_AT = {}
BAR_HEALTH_DETAIL = {}
ERROR_LOG_AT = {}
# cTrader trendbars can arrive 1–2 closed bars late; keep a stale guard while
# allowing the observed feed delay instead of suppressing healthy M1 signals.
EXECUTION_TIMEFRAMES = ("m1", "m3", "m5", "m15", "m30", "h1")
BAR_MAX_AGE = {"m1": 180, "m3": 360, "m5": 600, "m15": 1200, "m30": 2400, "h1": 4500}
BAR_SECONDS = {"m1": 60, "m3": 180, "m5": 300, "m15": 900, "m30": 1800, "h1": 3600}
REPLAY_WINDOW = {"m1": 10, "m3": 6, "m5": 6, "m15": 4, "m30": 4, "h1": 4}


def signal_engine_mode():
    configured = os.environ.get("SIGNAL_ENGINE")
    return configured.lower() if configured else ("release1-shadow" if SYMBOL == "XAUUSD" else "legacy")


def display_vietnam(value):
    if not value:
        return "chưa có"
    try:
        return utc_timestamp(value).astimezone(VIETNAM).strftime("%d/%m %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return "không xác định"


def market_open_now(at=None):
    return market_is_open(at or datetime.now(timezone.utc))


def worker_health(metrics, telegram_required=False, now=None):
    now = now or datetime.now(timezone.utc)
    limits = {"signal": 30, "tick": 5}
    if telegram_required:
        limits.update(telegram=90, notification=30)
    workers = {}
    for name, limit in limits.items():
        stamp = metrics.get("heartbeats", {}).get(name)
        age = max(0., (now-utc_timestamp(stamp)).total_seconds()) if stamp else None
        workers[name] = {"age_sec": age, "alive": age is not None and age <= limit}
    return {"alive": all(v["alive"] for v in workers.values()), "workers": workers}


def current_health():
    from telegram_views import health
    return health(STATUS,METRICS,worker_health(METRICS,not STATUS.get('dry_run',True)))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        snapshot = current_health()
        health = {'alive':snapshot['alive']}
        body = json.dumps(snapshot, separators=(",", ":")).encode()
        self.send_response(200 if health["alive"] else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def start_health_server():
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("BOT_HEALTH_PORT", "8766"))), HealthHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


class TelegramApiError(RuntimeError):
    def __init__(self, code, retry_after=None):
        super().__init__(f"Telegram API error {code}")
        self.code, self.retry_after = code, retry_after


class Telegram:
    def __init__(self, token, chat_id, timeout=10, *, chat_ids=None):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_ids = list(chat_ids) if chat_ids is not None else [item.strip() for item in os.environ.get("TELEGRAM_CHAT_IDS", chat_id).split(",") if item.strip()]
        self.chat_id = self.chat_ids[0]
        self.timeout = timeout
        self._route_guard=Lock()
        self._route_locks={}
        self._route_next={}
        self._global_next=0.
        self._retry_until=0.

    @contextmanager
    def sending_slot(self, chat_id):
        chat_id=str(chat_id)
        with self._route_guard:
            lock=self._route_locks.setdefault(chat_id,RLock())
        with lock:
            while True:
                with self._route_guard:
                    now=time.monotonic()
                    due=max(self._route_next.get(chat_id,0),self._global_next,self._retry_until)
                    if now>=due:
                        self._global_next=now+.05
                        self._route_next[chat_id]=now+1.
                        break
                time.sleep(min(.2,max(0,due-now)))
            yield

    def unused_slot(self, chat_id):
        # Preflight can wait for the engine without making any Telegram request.
        with self._route_guard:
            self._route_next[str(chat_id)]=time.monotonic()

    def rate_limited(self, seconds):
        with self._route_guard:
            self._retry_until=max(self._retry_until,time.monotonic()+max(1.,float(seconds or 1)))

    def call(self, method, payload, timeout=None):
        if method=='editMessageText':
            with self.sending_slot(payload['chat_id']):
                from telegram_views import rich_text
                payload=dict(payload,text=rich_text(payload['text']),parse_mode='HTML')
                return self._call(method,payload,timeout)
        return self._call(method,payload,timeout)

    def _call(self, method, payload, timeout=None):
        request = urllib.request.Request(self.url.replace("sendMessage", method), urllib.parse.urlencode(payload).encode())
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code==429:
                raw=exc.read()
                try: delay=json.loads(raw).get('parameters',{}).get('retry_after',1)
                except ValueError: delay=1
                self.rate_limited(delay)
                raise TelegramApiError(429,delay) from None
            raise
        if not result.get("ok"):
            if result.get('error_code')==429: self.rate_limited(result.get('parameters',{}).get('retry_after',1))
            raise TelegramApiError(result.get("error_code", 500),
                                   result.get("parameters", {}).get("retry_after"))
        return result

    def send(self, text, markup=None):
        responses = []
        for chat_id in self.chat_ids:
            responses.append(self.send_to(chat_id, text, markup))
        return responses

    def send_to(self, chat_id, text, markup=None, *, reply_message_id=None, slot_owned=False):
        if not slot_owned:
            with self.sending_slot(chat_id):
                return self.send_to(chat_id,text,markup,reply_message_id=reply_message_id,slot_owned=True)
        from telegram_views import rich_text
        payload = {"chat_id": chat_id, "text": rich_text(text), "parse_mode":"HTML"}
        if reply_message_id is not None:
            payload['reply_parameters']=json.dumps({'message_id':reply_message_id,'allow_sending_without_reply':True})
        if markup:
            payload["reply_markup"] = json.dumps(markup)
        try:
            return self.call("sendMessage", payload).get("result", {}).get("message_id")
        except urllib.error.HTTPError:
            raise
        except (TimeoutError, OSError) as exc:
            raise ConnectionError("Telegram delivery status is unknown") from exc

    def setup_details(self, db, chat_id, data, message_id):
        from setup_view import lookup, explain, candles, render
        action, identity = data.split(':', 1)
        record = lookup(db, chat_id, identity)
        if not record:
            return self.send_to(chat_id, 'Không tìm thấy setup thuộc lịch sử chat này.')
        if action == 'why':
            return self.send_to(chat_id, explain(record), reply_message_id=message_id)
        try:
            rows = candles(os.environ['MARKET_DB'], record)
            if len(rows)<2:
                return self.send_to(chat_id, 'Không đủ nến đã đóng để vẽ setup này.', reply_message_id=message_id)
            picture=render(record,rows)
        except (ImportError, OSError, ValueError, sqlite3.Error, KeyError):
            return self.send_to(chat_id, 'Chưa dựng được ảnh từ dữ liệu nến. Bạn vẫn có thể xem lý do setup.', reply_message_id=message_id)
        return self.send_photo(chat_id, picture, f"Setup #{record['user_number']} · ảnh lịch sử Exness · trạng thái {record['status']}", message_id)

    def send_photo(self, chat_id, picture, caption, reply_message_id):
        import uuid
        boundary='setup-'+uuid.uuid4().hex
        fields={'chat_id':str(chat_id),'caption':caption,
                'reply_parameters':json.dumps({'message_id':reply_message_id,'allow_sending_without_reply':True})}
        body=bytearray()
        for name,value in fields.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="setup.png"\r\nContent-Type: image/png\r\n\r\n'.encode())
        body.extend(picture);body.extend(f'\r\n--{boundary}--\r\n'.encode())
        request=urllib.request.Request(self.url.replace('sendMessage','sendPhoto'),data=bytes(body),headers={'Content-Type':'multipart/form-data; boundary='+boundary})
        # On-demand image only: never retry an ambiguous upload or delay the entry outbox.
        with self.sending_slot(chat_id):
            try:
                with urllib.request.urlopen(request,timeout=self.timeout) as response: result=json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code==429:
                    try: delay=json.loads(exc.read()).get('parameters',{}).get('retry_after',1)
                    except ValueError: delay=1
                    self.rate_limited(delay)
                raise
        if not result.get('ok'): raise TelegramApiError(result.get('error_code',500),result.get('parameters',{}).get('retry_after'))
        return result['result']['message_id']

    def set_commands(self):
        commands = [
            {"command": "start", "description": "Mở bảng điều khiển"},
            {"command": "status", "description": "Xem trạng thái bot"},
            {"command": "open", "description": "Xem lệnh đang mở"},
            {"command": "history", "description": "Xem lịch sử lệnh"},
            {"command": "stats", "description": "Xem thống kê phiên"},
            {"command": "news", "description": "Xem tin tức hôm nay"},
            {"command": "settings", "description": "Cài đặt timeframe"},
            {"command": "von", "description": "Nhập vốn USD, ví dụ /von 1000"},
            {"command": "ruiro", "description": "Nhập rủi ro, ví dụ /ruiro 1% hoặc 10usd"},
            {"command": "lot", "description": "Xem cài đặt vốn và rủi ro cá nhân"},
            {"command": "help", "description": "Xem hướng dẫn"},
        ]
        self.call("setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)}, timeout=5)

    @staticmethod
    def menu_markup(enabled):
        rows = []
        if signal_engine_mode().startswith('intraday-'):
            return {'inline_keyboard':[[{'text':'Nhận Intraday + SCALP: '+('BẬT' if 'm30' in enabled else 'TẮT'),'callback_data':'tf:m30'}]]}
        for tf in EXECUTION_TIMEFRAMES:
            rows.append([{"text": f"{tf.upper()} {'🟢 BẬT' if tf in enabled else '🔴 TẮT'}", "callback_data": f"tf:{tf}"}])
        return {"inline_keyboard": rows}

    def menu(self, chat_id, enabled):
        text = f"Chọn khung thời gian {SYMBOL}:"
        if signal_engine_mode().startswith('intraday-'):
            text += '\nIntraday CURRENT/Q100 + SCALP M5. Tắt chỉ ngừng entry mới; vẫn nhận kết quả của entry đã gửi. Không đóng vị thế mô phỏng hoặc lệnh broker.'
        self.send_to(chat_id, text, self.menu_markup(enabled))

    def home(self, chat_id):
        mode='Exness · Intraday + SCALP' if signal_engine_mode().startswith('intraday-') else 'Theo dõi tín hiệu thị trường'
        self.send_to(chat_id,f"🤖 {SYMBOL} · TRUNG TÂM TÍN HIỆU\n{mode}\n\n"
            "📍 Theo dõi lệnh và giá tham chiếu\n📊 Xem lịch sử, kết quả mô phỏng\n🔔 Quản lý thông báo và lịch tin\n\n"
            "Chọn một chức năng bên dưới.\nBot không tự đặt lệnh tại broker.",{'keyboard':[
            [{'text':'📡 Trạng thái'},{'text':'📈 Lệnh đang mở'}],
            [{'text':'📋 Lịch sử'},{'text':'📊 Thống kê'}],
            [{'text':'⚙️ Cài đặt'},{'text':'📰 Tin tức'}],
            [{'text':'💰 Vốn & rủi ro'}],
            [{'text':'❓ Hướng dẫn'},{'text':'🏠 Mở bảng điều khiển'}]],
            'resize_keyboard':True,'is_persistent':True})

    @staticmethod
    def status_text():
        if signal_engine_mode().startswith('intraday-'):
            from telegram_views import vn
            state=current_health();metrics=METRICS
            title='⏸ Thị trường đóng; bot chờ mở cửa' if state.get('market_closed') else '🟢 Sẵn sàng phát tín hiệu' if state['ready'] else '🟠 Chưa sẵn sàng phát tín hiệu'
            lines=['📡 XAUUSD · Intraday + SCALP thử nghiệm · M5 + M15 / M30 + H1',title]
            lines.extend(state.get('degraded_reasons',[]))
            for tf,item in state.get('timeframes',{}).items():
                lines.append(tf.upper()+': '+('sẵn sàng' if item.get('ready') else str(item.get('error')))+' · nến '+vn(item.get('checkpoint')))
            lines.extend(['CURRENT: Q≥75 · TP 2,5R · SL≥1 ATR · tối đa 4 giờ',
                'Q100: Q100 · TP 3R · SL≥1,5 ATR · tối đa 6 giờ',
                'SCALP M5: Q≥90 · TP 1,5R · SL≥1 ATR · tối đa 30 phút',
                'Scalp: M15/H1 đã đóng đồng hướng; setup hết hạn sau 5 phút.',
                'Một vị thế chung. Cùng thời điểm: Q100 > CURRENT > SCALP; đối hướng bỏ cả hai.',
                'Tick: '+str(state.get('tick_age_sec'))+' giây · Hàng đợi: '+str(metrics.get('telegram_pending',0)),
                'Tín hiệu gần nhất: '+vn(metrics.get('last_signal_at')),
                'Telegram ACK gần nhất: '+vn(metrics.get('telegram_delivery',{}).get('sent_at')),
                'UNKNOWN lịch sử: '+str(metrics.get('telegram_unknown',0))+'; không tự gửi lại.',
                'News đồng bộ: '+vn(state.get('news',{}).get('last_sync'))])
            for source,item in state.get('news',{}).get('source_status',{}).items():
                if item.get('status')!='OK': lines.append('News '+source+': '+str(item.get('status')))
            lines.append('Chưa đạt gate backtest/Pine parity/shadow 48h. Lịch nghỉ đặc biệt chưa đầy đủ. API ACK không xác nhận bạn đã đọc.')
            return '\n'.join(lines)
        metrics = STATUS.get("metrics", {})
        bars = metrics.get("bars", {})
        ready = STATUS.get("ready", False)
        lines = [f"📡 TRẠNG THÁI {SYMBOL}", "", "🟢 Sẵn sàng phát tín hiệu" if ready else "🟠 Chưa sẵn sàng phát tín hiệu"]
        if STATUS.get('error'):
            lines.append(str(STATUS['error']))
        if signal_engine_mode().startswith('intraday-'):
            lines += ['Intraday kết hợp thử nghiệm · Exness · M30 + H1',
                      'CURRENT: Q ≥ 75 · TP 2,5R · SL ≥ 1 ATR · tối đa 4 giờ',
                      'Q100: Q100 · TP 3R · SL ≥ 1,5 ATR · tối đa 6 giờ',
                      'Cùng báo: ưu tiên Q100; ngược hướng: bỏ cả hai.',
                      'Không giới hạn số lệnh/ngày; một vị thế tín hiệu tại một thời điểm.',
                      'Không mở 15:45–17:00 New York; mục tiêu đóng 16:00 hoặc trước khoảng nghỉ đã biết.',
                      'Thiếu giá: chưa thể xác nhận đóng. Lịch nghỉ đặc biệt chưa đầy đủ.',
                      'Chưa đạt gate backtest; chưa nghiệm thu Pine parity hoặc shadow 48 giờ.']
        for tf, state in STATUS.get("timeframes", {}).items():
            if not state.get("ready"):
                lines.append(f"{tf.upper()}: {state.get('error') or 'đang chờ dữ liệu'}")
        if STATUS.get("market_closed"):
            lines.append("⏸ Thị trường đang ngoài giờ; bot vẫn chạy và sẽ tự tiếp tục khi mở cửa.")
        for tf in (("m30", "h1") if signal_engine_mode().startswith("intraday-") else EXECUTION_TIMEFRAMES):
            item = bars.get(tf)
            if item:
                lines.append(f"{tf.upper()}: {item.get('quality', 'N/A')} · {item.get('age_sec', 'N/A')} giây")
            else:
                lines.append(f"{tf.upper()}: đang cập nhật")
        lines += ["", f"Tick: {metrics.get('last_tick_age', 'N/A')} giây",
                  f"Signal cycle: {metrics.get('last_signal_cycle_ms', 'N/A')} ms",
                  f"Signal gần nhất: {display_vietnam(metrics.get('last_signal_at'))}",
                  f"Telegram pending: {metrics.get('telegram_pending', 0)}",
                  f"News đồng bộ: {display_vietnam(STATUS.get('news', {}).get('last_sync'))}",
                  "", "Đang theo dõi thị trường." if ready else "Tín hiệu có thể bị gián đoạn; xem lý do ở trên."]
        return "\n".join(lines)

    @staticmethod
    def page_markup(page, has_next, prefix="hist"):
        row = []
        if page > 1:
            row.append({"text": "◀ Trước", "callback_data": f"{prefix}:{page - 1}"})
        if has_next:
            row.append({"text": "Sau ▶", "callback_data": f"{prefix}:{page + 1}"})
        return {"inline_keyboard": [row, [{"text": "🏠 Trang chính", "callback_data": "home"}]]} if row else {"inline_keyboard": [[{"text": "🏠 Trang chính", "callback_data": "home"}]]}

    def history_page(self, db, chat_id, page, edit_message_id=None):
        if signal_engine_mode().startswith('intraday-'):
            from telegram_views import history
            page=max(1,int(page));text,has_next=history(db,chat_id,page,opened=False)
            markup=self.page_markup(page,has_next,"hist")
            if edit_message_id:
                self.call('editMessageText',{'chat_id':chat_id,'message_id':edit_message_id,'text':text,'reply_markup':json.dumps(markup)})
            else: self.send_to(chat_id,text,markup)
            return
        page = max(1, int(page))
        limit, offset = 20, (page - 1) * 20
        rows = db.execute("""
            SELECT u.number,u.timeframe,t.signal,t.status,t.entry,t.sl,t.tp,t.created_at,t.closed_at
            FROM user_signals u JOIN trades t ON t.id=u.trade_id
            WHERE u.chat_id=? ORDER BY u.trade_id DESC LIMIT ? OFFSET ?
        """, (str(chat_id), limit + 1, offset)).fetchall()
        has_next = len(rows) > limit
        rows = rows[:limit]
        text = [f"📋 LỊCH SỬ {SYMBOL} — Trang {page}", ""]
        text += [f"#{r[0]} {r[1]} {r[2]} | {r[3]}\nEntry {r[4]:.2f} · SL {r[5]:.2f} · TP {r[6]:.2f}\nMở {display_vietnam(r[7])} · Đóng {display_vietnam(r[8])}" for r in rows]
        if not rows:
            text = [f"📋 LỊCH SỬ {SYMBOL}", "", "Chưa có lệnh trong lịch sử này."]
        markup = self.page_markup(page, has_next, "hist")
        if edit_message_id:
            self.call("editMessageText", {"chat_id": chat_id, "message_id": edit_message_id, "text": "\n".join(text), "reply_markup": json.dumps(markup)})
        else:
            self.send_to(chat_id, "\n".join(text), markup)

    def open_page(self, db, chat_id, page, edit_message_id=None):
        if signal_engine_mode().startswith('intraday-'):
            from telegram_views import history
            page=max(1,int(page));text,has_next=history(db,chat_id,page,opened=True)
            markup=self.page_markup(page,has_next,"open")
            markup['inline_keyboard'].insert(0,[{'text':'Làm mới','callback_data':'open:'+str(page)}])
            try:
                quote=latest_quote(os.environ.get('MARKET_DB','market.db'))
                if quote: text+=f'\nBid/ask {quote[1]:.3f}/{quote[2]:.3f} · lúc '+display_vietnam(quote[4] or quote[0])+' (VN)'
            except (sqlite3.Error,OSError): text+='\nChưa đọc được giá mới.'
            if edit_message_id:
                self.call('editMessageText',{'chat_id':chat_id,'message_id':edit_message_id,'text':text,'reply_markup':json.dumps(markup)})
            else: self.send_to(chat_id,text,markup)
            return
        page = max(1, int(page))
        limit, offset = 20, (page - 1) * 20
        rows = db.execute("""
            SELECT u.number,u.timeframe,t.signal,t.status,t.entry,t.sl,t.tp,t.created_at
            FROM user_signals u JOIN trades t ON t.id=u.trade_id
            WHERE u.chat_id=? AND t.status='OPEN'
            ORDER BY u.trade_id DESC LIMIT ? OFFSET ?
        """, (str(chat_id), limit + 1, offset)).fetchall()
        has_next = len(rows) > limit
        rows = rows[:limit]
        lines = [f"📈 LỆNH ĐANG MỞ {SYMBOL} — Trang {page}", ""]
        lines += [f"#{r[0]} {r[1]} {r[2]} | {r[3]}\nEntry {r[4]:.2f} · SL {r[5]:.2f} · TP {r[6]:.2f}\nMở {display_vietnam(r[7])}" for r in rows]
        if not rows:
            lines = [f"📈 LỆNH ĐANG MỞ {SYMBOL}", "", "Hiện không có lệnh đang mở."]
        markup = self.page_markup(page, has_next, "open")
        text = "\n".join(lines)
        if edit_message_id:
            self.call("editMessageText", {"chat_id": chat_id, "message_id": edit_message_id, "text": text, "reply_markup": json.dumps(markup)})
        else:
            self.send_to(chat_id, text, markup)

    def poll(self, store, stop):
        from position_size import handle as handle_sizing
        retry_delay = 1
        production_menu_store = TradeStore(store.path)
        menu_store = production_menu_store
        test_chat = os.environ.get("TELEGRAM_TEST_CHAT_ID")
        test_path = os.environ.get("TELEGRAM_TEST_STATE_DB")
        test_menu_store = None
        if test_chat or test_path:
            if not test_chat or not test_path or Path(test_path).name != "tv-test.db" or test_chat in self.chat_ids or Path(test_path).resolve() == Path(store.path).resolve():
                production_menu_store.close()
                raise ValueError("isolated TEST chat and tv-test.db required")
            test_menu_store = TradeStore(test_path)
        offset = int(menu_store.checkpoint("telegram_update_id") or 0)
        update = None
        METRICS["heartbeats"]["telegram"] = datetime.now(timezone.utc).isoformat()
        try:
            while not stop.is_set():
              try:
                update=None
                result = self.call("getUpdates", {"timeout": 20, "offset": offset, "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=30)
                retry_delay = 1
                METRICS["telegram_polling"] = True
                METRICS["heartbeats"]["telegram"] = datetime.now(timezone.utc).isoformat()
                for update in result.get("result", []):
                    item = update.get("callback_query") or update.get("message", {})
                    chat = (item.get("message") or item).get("chat", {})
                    if str(chat.get("id")) not in self.chat_ids and str(chat.get("id")) != test_chat:
                        offset = update["update_id"] + 1
                        production_menu_store.set_checkpoint("telegram_update_id", str(offset))
                        continue
                    menu_store = test_menu_store if str(chat.get("id")) == test_chat else production_menu_store
                    with closing(sqlite3.connect(menu_store.path)) as db:
                        menu_store.ensure_users([str(chat["id"])], SYMBOL)
                        if "callback_query" in update:
                            tf = update["callback_query"].get("data", "").removeprefix("tf:")
                            data = update["callback_query"].get("data", "")
                            valid_page = data.startswith(("hist:", "open:")) and len(data.split(":", 1)[1]) <= 6 and data.split(":", 1)[1].isascii() and data.split(":", 1)[1].isdigit() and 1 <= int(data.split(":", 1)[1]) <= 100000
                            if data.startswith(("hist:", "open:")) and not valid_page:
                                self.call("answerCallbackQuery", {"callback_query_id": item["id"], "text": "Trang không hợp lệ"})
                            elif data.startswith(('why:', 'chart:')):
                                self.call('answerCallbackQuery', {'callback_query_id':item['id']})
                                self.setup_details(db, chat['id'], data, item['message']['message_id'])
                            elif data == "home":
                                self.call("answerCallbackQuery", {"callback_query_id": update["callback_query"]["id"]})
                                self.home(chat["id"])
                            elif data.startswith("hist:"):
                                self.call("answerCallbackQuery", {"callback_query_id": update["callback_query"]["id"]})
                                self.history_page(db, chat["id"], int(data.split(":", 1)[1]), update["callback_query"]["message"]["message_id"])
                            elif data.startswith("open:"):
                                self.call("answerCallbackQuery", {"callback_query_id": update["callback_query"]["id"]})
                                self.open_page(db, chat["id"], int(data.split(":", 1)[1]), update["callback_query"]["message"]["message_id"])
                            if data.startswith('tf:') and signal_engine_mode().startswith('intraday-') and tf!='m30':
                                self.call('answerCallbackQuery',{'callback_query_id':item['id'],'text':'Khung cũ không còn hỗ trợ. Dùng công tắc chung Intraday + SCALP.'})
                                self.menu(chat['id'],menu_store.user_timeframes(str(chat['id']),SYMBOL))
                            if tf in ({'m30'} if signal_engine_mode().startswith('intraday-') else set(EXECUTION_TIMEFRAMES)):
                                value = not menu_store.user_enabled(str(chat["id"]), SYMBOL, tf)
                                menu_store.set_user_timeframe(str(chat["id"]), SYMBOL, tf, value)
                                self.call("answerCallbackQuery", {"callback_query_id": update["callback_query"]["id"], "text": f"{tf.upper()}: {'đã bật' if value else 'đã tắt'}"})
                                enabled = menu_store.user_timeframes(str(chat["id"]), SYMBOL)
                                self.call("editMessageText", {"chat_id": chat["id"], "message_id": update["callback_query"]["message"]["message_id"], "text": f"Chọn khung thời gian {SYMBOL}:", "reply_markup": json.dumps(self.menu_markup(enabled))})
                        elif handle_sizing(self, menu_store, item, chat):
                            pass
                        elif item.get("text", "").strip() in {"/start", "/menu", "Menu", "🏠 Mở bảng điều khiển"}:
                            self.home(chat["id"])
                        elif item.get("text", "").strip() in {"/status", "📡 Trạng thái"}:
                            self.send_to(chat["id"], ("[TEST · trạng thái dịch vụ chung; dữ liệu giao dịch TEST tách riêng]\n" if menu_store is test_menu_store else "") + self.status_text())
                        elif item.get("text", "").strip() in {"/help", "❓ Hướng dẫn"}:
                            self.send_to(chat["id"], "❓ HƯỚNG DẪN\n\n"
                                         "/start — mở menu\n/status — trạng thái bot\n"
                                         "/open — lệnh đang mở\n/history — lịch sử\n"
                                         "/stats — thống kê phiên\n/news — tin tức hôm nay\n"
                                         "/settings — bật/tắt nhận tín hiệu\n\n"
                                         "/von 1000 — vốn USD để tính lot\n/ruiro 1% hoặc /ruiro 10usd — rủi ro mỗi lệnh\n/lot — xem cài đặt riêng\n\n"
                                         "R là rủi ro ban đầu; kết quả hiển thị là mô phỏng, không phải tài khoản broker.\n"
                                         "Q100 là điểm chất lượng, không phải 100% thắng. Spread là chênh bid/ask.\n"
                                         "Chase giới hạn giá chạy bất lợi. Tin ĐÃ BỎ LỠ không phải lời mời vào lệnh.\n"
                                         "Giá trong tin có thời điểm kiểm tra; hãy đối chiếu giá broker trước khi quyết định.\n"
                                         "Tắt nhận tín hiệu chỉ chặn entry mới; bot không tự đặt hoặc đóng lệnh broker.")
                        elif item.get("text", "").strip() in {"⚙️ Cài đặt", "/settings"}:
                            self.menu(chat["id"], menu_store.user_timeframes(str(chat["id"]), SYMBOL))
                        elif item.get("text", "").strip() in {"📰 Tin tức", "/news"}:
                            enabled = menu_store.news_enabled(str(chat["id"]))
                            self.send_to(chat["id"], f"📰 Trung tâm tin tức {SYMBOL}", {"keyboard": [
                                [{"text": "📅 Tin tức hôm nay"}],
                                [{"text": f"🔔 Thông báo tin tức: {'🟢 BẬT' if enabled else '🔴 TẮT'}"}],
                                [{"text": "⬅️ Quay lại"}],
                            ], "resize_keyboard": True})
                        elif item.get("text", "").strip() == "📅 Tin tức hôm nay":
                            text = news_today(NEWS_DB)
                            for chunk in split_message(text): self.send_to(chat["id"], chunk)
                        elif item.get("text", "").strip().startswith("🔔 Thông báo tin tức:"):
                            value = not menu_store.news_enabled(str(chat["id"]))
                            menu_store.set_news_enabled(str(chat["id"]), value)
                            self.send_to(chat["id"], f"Thông báo tin tức: {'đã bật' if value else 'đã tắt'}")
                        elif item.get("text", "").strip() == "⬅️ Quay lại":
                            self.home(chat["id"])
                        elif item.get("text", "").strip() in {"/history", "📋 Lịch sử"}:
                            self.history_page(db, chat["id"], 1)
                        elif item.get("text", "").strip() in {"/open", "📈 Lệnh đang mở"}:
                            self.open_page(db, chat["id"], 1)
                        elif item.get("text", "").strip() in {"/stats", "📊 Thống kê"}:
                            session = session_key(datetime.now(timezone.utc))
                            if signal_engine_mode().startswith('intraday-'):
                                from telegram_views import stats
                                from intraday_live import VERSION
                                self.send_to(chat['id'],stats(db,str(chat['id']),session,VERSION))
                                offset=update['update_id']+1
                                production_menu_store.set_checkpoint('telegram_update_id',str(offset))
                                continue
                            rows = db.execute("""
                                SELECT u.number,u.timeframe,t.signal,t.entry,t.sl,t.tp,t.status,t.exit_price,t.realized_r
                                FROM user_signals u JOIN trades t ON t.id=u.trade_id
                                WHERE u.chat_id=? AND t.session=? AND t.status <> 'OPEN'
                                ORDER BY u.number
                            """, (str(chat["id"]), session)).fetchall()
                            try:
                                quote = latest_quote(os.environ.get("MARKET_DB", "market.db"))
                            except sqlite3.Error:
                                quote = None
                            lines = [f"📊 Thống kê phiên {session}"]
                            total = 0.0
                            total_r = 0.0
                            r_count = 0
                            for r in rows:
                                buy = r[2].endswith("BUY")
                                exit_price = r[7] if r[7] is not None else r[5] if r[6] == "TP" else r[4] if r[6] == "SL" else r[3]
                                distance = abs(exit_price - r[3]) / 0.01
                                pips = ((exit_price-r[3]) if buy else (r[3]-exit_price))/.01
                                total += pips
                                if r[8] is not None:
                                    total_r += r[8]; r_count += 1
                                result = f'{r[8]:+.3f}R' if r[8] is not None else f'{pips:+.0f} pip'
                                lines.append(f"#{r[0]} {r[1]} {r[2]} | {r[6]} | {result}")
                            lines.append(f"Tổng: {total:+.0f} pip | Lệnh: {len(rows)}")
                            if r_count:
                                lines.append(f'Intraday có kết quả R: {r_count} lệnh | {total_r:+.3f}R (mô phỏng)')
                            self.send_to(chat["id"], "\n".join(lines))
                    offset = update["update_id"] + 1
                    production_menu_store.set_checkpoint("telegram_update_id", str(offset))
                    METRICS["heartbeats"]["telegram"] = datetime.now(timezone.utc).isoformat()
              except Exception as exc:
                if update and update.get("callback_query"):
                    try:
                        self.call("answerCallbackQuery", {"callback_query_id": update["callback_query"]["id"], "text": "Không thể xử lý yêu cầu"})
                    except Exception:
                        pass
                if update:
                    offset=update['update_id']+1
                    production_menu_store.set_checkpoint('telegram_update_id',str(offset))
                    METRICS['telegram_command_error']=type(exc).__name__
                    update=None
                METRICS["telegram_polling"] = False
                print(f"telegram poll: {type(exc).__name__}: {exc}", flush=True)
                stop.wait(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
        finally:
            production_menu_store.close()
            if test_menu_store:
                test_menu_store.close()


def deliver_pending(store, telegram, limit=5, chat_id=None):
    from delivery import prepare
    from contextlib import nullcontext
    for row in store.pending_notifications(chat_id)[:limit]:
        slot=telegram.sending_slot(row['chat_id']) if hasattr(telegram,'sending_slot') and row['chat_id'] else nullcontext()
        with slot:
            body=prepare(store,row)
            if body is None or not store.claim_notification(row['event_key']):
                if hasattr(telegram,'unused_slot'):telegram.unused_slot(row['chat_id'])
                continue
            row=store.db.execute('SELECT * FROM notifications WHERE event_key=?',(row['event_key'],)).fetchone()
            body=prepare(store,row)
            if body is None:
                with store.transaction():
                    store.db.execute("UPDATE notifications SET state='PENDING',requested_at=NULL WHERE event_key=? AND state='SENDING'",(row['event_key'],))
                if hasattr(telegram,'unused_slot'):telegram.unused_slot(row['chat_id'])
                continue
            # A lock wait after validation must not silently consume the freshness budget.
            instant=datetime.now(timezone.utc)
            check=store.db.execute('SELECT check_json FROM notifications WHERE event_key=?',(row['event_key'],)).fetchone()[0]
            quote_time=json.loads(check).get('provider_time') if check else None
            expired=bool(row['expires_at'] and instant>=datetime.fromisoformat(row['expires_at']))
            stale=bool(row['delivery_json'] and quote_time and (instant-datetime.fromisoformat(quote_time)).total_seconds()>1)
            if expired or stale:
                with store.transaction():
                    store.db.execute("UPDATE notifications SET state='PENDING',requested_at=NULL WHERE event_key=? AND state='SENDING'",(row['event_key'],))
                if hasattr(telegram,'unused_slot'):telegram.unused_slot(row['chat_id'])
                continue
            try:
                if row['chat_id']:
                    kw={}
                    if isinstance(telegram,Telegram):
                        kw=dict(reply_message_id=row['reply_message_id'],slot_owned=True)
                        if row['delivery_json']:
                            from setup_view import keyboard
                            kw['markup']=keyboard(json.loads(row['delivery_json'])['trade_id'])
                    response=telegram.send_to(row['chat_id'],body,**kw)
                else:
                    response=telegram.send(body)
                if not (isinstance(response,int) and not isinstance(response,bool) and response>0 or isinstance(response,list) and response and all(isinstance(x,int) and not isinstance(x,bool) and x>0 for x in response)):
                    raise ValueError('missing Telegram message_id')
            except urllib.error.HTTPError as exc:
                delay=None
                if exc.code==429:
                    try: delay=json.loads(exc.read()).get('parameters',{}).get('retry_after')
                    except (ValueError,OSError): pass
                    if hasattr(telegram,'rate_limited'): telegram.rate_limited(delay)
                store.notification_failed(row['event_key'],f'HTTP {exc.code}',
                    permanent=400<=exc.code<500 and exc.code not in (408,429),retry_after=delay)
            except TelegramApiError as exc:
                if exc.code==429 and hasattr(telegram,'rate_limited'): telegram.rate_limited(exc.retry_after)
                store.notification_failed(row['event_key'],str(exc),
                    permanent=400<=exc.code<500 and exc.code not in (408,429),retry_after=exc.retry_after)
            except (ConnectionError,TimeoutError,OSError,ValueError):
                store.notification_uncertain(row['event_key'],'delivery outcome unknown')
            else:
                store.notification_sent(row['event_key'],response)


def notification_worker(path, telegram, stop):
    from trades import OUTBOX_WAKE
    test_path=os.environ.get('TELEGRAM_TEST_STATE_DB');test_chat=os.environ.get('TELEGRAM_TEST_CHAT_ID')
    if test_path or test_chat:
        if not test_path or not test_chat or Path(test_path).name!='tv-test.db' or Path(test_path).resolve()==Path(path).resolve() or test_chat in telegram.chat_ids:
            raise ValueError('isolated TEST outbox required')
    store=TradeStore(path)
    test_store=TradeStore(test_path) if test_path else None
    stores=[store]+([test_store] if test_store else [])
    incident_since=store.checkpoint('delivery_incident_since')
    if not incident_since:
        incident_since=datetime.now(timezone.utc).isoformat()
        store.set_checkpoint('delivery_incident_since',incident_since)
    # Single coordinator owns recovery, before any HTTP worker is started.
    for db in stores: db.recover_notifications()
    def dispatch(db_path,chat):
        db=TradeStore(db_path)
        db.db.execute('PRAGMA busy_timeout=250')
        try: deliver_pending(db,telegram,limit=1,chat_id=chat)
        except Exception:
            # This task has stopped HTTP work: conservatively quarantine any unpersisted ACK.
            with db.transaction():
                db.db.execute("UPDATE notifications SET state='UNKNOWN',failed_at=?,last_error='dispatch interrupted; ACK uncertain' WHERE state='SENDING' AND chat_id=?",(datetime.now(timezone.utc).isoformat(),str(chat)))
            raise
        finally: db.close()
    lanes={};errors={}
    try:
        with ThreadPoolExecutor(max_workers=max(1,len(telegram.chat_ids)+(1 if test_store else 0))) as pool:
            while not stop.is_set():
                try:
                    routes=[(store,str(chat)) for chat in telegram.chat_ids]
                    if test_store:
                        with test_store.transaction():
                            test_store.db.execute("UPDATE notifications SET state='DEAD',last_error='TEST route mismatch' WHERE state='PENDING' AND (chat_id IS NULL OR chat_id!=?)",(test_chat,))
                        routes.append((test_store,test_chat))
                    # Fan out legacy broadcasts before claiming, so partial ACK cannot resend to every chat.
                    with store.transaction():
                        for row in store.db.execute("SELECT * FROM notifications WHERE state='PENDING' AND chat_id IS NULL").fetchall():
                            for chat in telegram.chat_ids:
                                store.queue_notification(row['event_key']+':route:'+str(chat),row['body'],chat,
                                    expires_at=row['expires_at'],priority=row['priority'])
                            store.db.execute("UPDATE notifications SET state='SUPPRESSED',last_error='expanded by destination' WHERE event_key=?",(row['event_key'],))
                    for db,chat in routes:
                        key=(db.path,chat);future=lanes.get(key)
                        if future and future.done():
                            try: future.result();errors.pop(key,None)
                            except Exception as exc: errors[key]=type(exc).__name__
                            del lanes[key]
                        if key not in lanes and db.pending_notifications(chat):
                            lanes[key]=pool.submit(dispatch,db.path,chat)
                    METRICS['heartbeats']['notification']=datetime.now(timezone.utc).isoformat()
                    METRICS['telegram_oldest_pending_sec']=store.oldest_pending_age()
                    latest=store.db.execute("SELECT state,last_error,requested_at,sent_at FROM notifications WHERE requested_at IS NOT NULL ORDER BY requested_at DESC LIMIT 1").fetchone()
                    METRICS['telegram_delivery']=dict(latest) if latest else {}
                    METRICS['notification_counts']=store.notification_counts()
                    since=incident_since
                    for state in ('UNKNOWN','DEAD'):
                        METRICS['delivery_new_'+state.lower()]=store.db.execute('SELECT COUNT(*) FROM notifications WHERE state=? AND COALESCE(failed_at,requested_at,created_at)>=?',(state,since)).fetchone()[0]
                    METRICS['delivery_stuck_dependencies']=store.db.execute("SELECT COUNT(*) FROM notifications n LEFT JOIN notifications p ON p.event_key=n.depends_on WHERE n.state='PENDING' AND n.depends_on IS NOT NULL AND (p.state IN ('UNKNOWN','EXPIRED','DEAD','SUPPRESSED') OR p.state IS NULL)").fetchone()[0]
                    METRICS['notification_active_sends']=sum(not f.done() for f in lanes.values())
                    sending=store.db.execute("SELECT MIN(requested_at) FROM notifications WHERE state='SENDING'").fetchone()[0]
                    METRICS['sending_age_seconds']=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(sending)).total_seconds()) if sending else 0
                    if errors: METRICS['notification_error']=str(list(errors.values()))
                    else: METRICS.pop('notification_error',None)
                except Exception as exc:
                    METRICS['notification_error']=type(exc).__name__
                OUTBOX_WAKE.wait(.2);OUTBOX_WAKE.clear()
    finally:
        for db in stores: db.close()


def latest_quote(market_db):
    with closing(sqlite3.connect(market_db)) as db:
        return db.execute(
            "SELECT timestamp, bid, ask, received_at, provider_timestamp FROM latest_ticks WHERE symbol=?", (SYMBOL,)
        ).fetchone()


def bar_quality(market_db, timeframe, latest=None):
    """Return the quality state of the newest bars for compatibility callers."""
    db = sqlite3.connect(market_db)
    try:
        rows = db.execute("""
            SELECT timestamp,open,high,low,close FROM bars
            WHERE symbol=? AND timeframe=? ORDER BY timestamp DESC LIMIT 2001
        """, (SYMBOL, timeframe)).fetchall()
    finally:
        db.close()
    return inspect_bars(rows, timeframe, datetime.now(timezone.utc),
                        min_closed_bars=1, max_age_sec=BAR_MAX_AGE[timeframe]).status


def bar_quality_detail(market_db, timeframe):
    db = sqlite3.connect(market_db)
    try:
        rows = db.execute("""
            SELECT timestamp,open,high,low,close,symbol_digits FROM bars
            WHERE symbol=? AND timeframe=? ORDER BY timestamp DESC LIMIT 2001
        """, (SYMBOL, timeframe)).fetchall()
    finally:
        db.close()
    if rows and (rows[0][5] is None or not 0 < int(rows[0][5]) <= 10):
        from market_quality import BarQuality
        return BarQuality("INVALID", None, ready=False)
    return inspect_bars(rows, timeframe, datetime.now(timezone.utc),
                        min_closed_bars=2000, max_age_sec=BAR_MAX_AGE[timeframe])


def refresh_bar_health(market_db, timeframe):
    now = time.monotonic()
    if now - BAR_HEALTH_AT.get(timeframe, 0) >= 5 or timeframe not in BAR_HEALTH_DETAIL:
        BAR_HEALTH_DETAIL[timeframe] = bar_quality_detail(market_db, timeframe)
        BAR_HEALTH_AT[timeframe] = now
    detail = BAR_HEALTH_DETAIL[timeframe]
    age = detail.age_sec
    METRICS.setdefault("bars", {})[timeframe] = {
        "age_sec": round(age, 1) if age is not None else None,
        "quality": detail.status, "ready": detail.ready,
        "gap_count": detail.gap_count, "duplicate_count": detail.duplicate_count,
    }
    return detail

def news_today(path):
    local = ZoneInfo("Asia/Ho_Chi_Minh")
    today = datetime.now(local).date()
    try:
        with closing(sqlite3.connect(path)) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(news)")}
            extra = ",source_url,confidence,status" if {"source_url", "confidence", "status"} <= columns else ""
            rows = db.execute("SELECT event_time,title,source,impact,actual,forecast,previous" + extra + " FROM news ORDER BY event_time").fetchall()
    except sqlite3.Error:
        return "📰 Dữ liệu tin tức chưa sẵn sàng."
    source_health=news_health(path)
    lines = [f"📰 TIN TỨC {SYMBOL} — {today:%d/%m/%Y} · giờ VN",
             'Đồng bộ gần nhất: '+display_vietnam(source_health.get('last_sync'))]
    unavailable=[name for name,item in source_health.get('source_status',{}).items() if item.get('status')!='OK']
    if unavailable: lines.append('Lịch chưa đầy đủ; nguồn thiếu: '+', '.join(unavailable))
    if source_health.get('error'): lines.append('Chưa xác minh đầy đủ trạng thái nguồn tin.')
    shown=0
    for row in rows:
        ts, title, source, impact, actual, forecast, previous = row[:7]
        source_url = row[7] if len(row) > 7 else None
        confidence = row[8] if len(row) > 8 else "Medium"
        status = row[9] if len(row) > 9 else "SCHEDULED"
        try: at = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(local)
        except ValueError: continue
        if at.date() != today: continue
        shown+=1
        try:
            actual_value = float(str(actual).replace(",", "").replace("%", "")) if actual else None
            forecast_value = float(str(forecast).replace(",", "").replace("%", "")) if forecast else None
        except ValueError:
            actual_value = forecast_value = None
        direction = ("áp lực giảm XAUUSD" if actual_value > forecast_value else
                     "hỗ trợ tăng XAUUSD" if actual_value < forecast_value else
                     "biến động cao/chưa đủ dữ liệu") if actual_value is not None and forecast_value is not None else "biến động cao/chưa đủ dữ liệu"
        if signal_engine_mode().startswith('intraday-'):
            direction='Chưa suy ra hướng giá chỉ từ chênh actual/forecast'
        lines += ["", f"🔴 {at:%H:%M} — {title}", f"Nguồn: {source} | Ảnh hưởng: {impact} | {status}",
                  f"Actual: {actual or 'Chưa công bố'} | Forecast: {forecast or 'Chưa công bố'} | Previous: {previous or 'Chưa công bố'}",
                  f"Đánh giá: {direction} | Độ tin cậy: {confidence}"]
        if source_url: lines.append(f"Nguồn chính thức: {source_url}")
    return "\n".join(lines if shown else lines + ["", "Chưa có sự kiện hôm nay trong dữ liệu đã tải; không có nghĩa thị trường không có tin."])


def split_message(text, limit=3500):
    """Split Telegram text on line boundaries whenever possible."""
    chunks, current = [], []
    size = 0
    for line in text.splitlines():
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.extend(line[index:index + limit] for index in range(0, len(line), limit))
        else:
            current.append(line)
            size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def owned_lifecycle_events(lifecycle, event):
    owner = next((index for index, item in enumerate(lifecycle)
                  if item.get("event_key") == event.event_key and
                  item.get("event_type") == event.event_kind), None)
    owned = [] if owner is None else lifecycle[owner + 1:]
    next_owner = next((index for index, item in enumerate(owned)
                       if item.get("event_type") in {"PRIMARY_OPEN", "DCA_OPEN"}), None)
    return owned if next_owner is None else owned[:next_owner]

def news_health(path):
    try:
        with closing(sqlite3.connect(path, timeout=2)) as db:
            columns = {r[1] for r in db.execute("PRAGMA table_info(news_source_status)")}
            sources = {r[0]: {"status": r[1], "last_sync": r[2], "last_error": r[3], "event_count": r[4]}
                       for r in db.execute("SELECT source,status,last_sync,last_error,event_count FROM news_source_status")} if columns else {}
            event_sync = db.execute("SELECT MAX(updated_at) FROM news").fetchone()[0]
            stamps=[item['last_sync'] for item in sources.values() if item.get('last_sync')]
            last_sync=max(stamps) if stamps else event_sync
            local = ZoneInfo("Asia/Ho_Chi_Minh"); today = datetime.now(local).date(); events = 0
            for (value,) in db.execute("SELECT event_time FROM news"):
                try: events += datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(local).date() == today
                except ValueError: pass
        age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(last_sync.replace("Z", "+00:00"))).total_seconds()) if last_sync else None
        return {"last_sync": last_sync, "events_today": events, "source_status": sources, "cache_age_sec": round(age, 1) if age is not None else None}
    except sqlite3.Error as exc:
        return {"last_sync": None, "events_today": 0, "source_status": {}, "error": str(exc)}

def queue_news_alerts(store, news_db):
    now = datetime.now(timezone.utc)
    try:
        with closing(sqlite3.connect(news_db, timeout=2)) as db:
            rows = db.execute("SELECT COALESCE(event_id,id),event_time,title,source,impact,actual,forecast,previous,status FROM news WHERE event_time BETWEEN ? AND ?", ((now-timedelta(minutes=5)).isoformat(), (now+timedelta(minutes=61)).isoformat())).fetchall()
        chats = store.db.execute("SELECT chat_id FROM user_news_settings WHERE enabled=1").fetchall()
    except sqlite3.Error as exc:
        METRICS['news_error']=type(exc).__name__
        return
    METRICS.pop('news_error',None)
    for event_id,ts,title,source,impact,actual,forecast,previous,status in rows:
        if status in {"CANCELLED", "TIME_UNCONFIRMED", "NEEDS_VERIFICATION"}: continue
        at=datetime.fromisoformat(ts.replace("Z", "+00:00")); minutes=(at-now).total_seconds()/60
        alert = "before60" if 55 <= minutes <= 61 else "before15" if 10 <= minutes <= 16 else "released" if -5 <= minutes <= 0 and actual else None
        if not alert: continue
        label={"before60":"còn khoảng 60 phút","before15":"còn khoảng 15 phút","released":"đã công bố"}[alert]
        body=f"📰 {title}\n{label} · {source} · {impact}\nActual: {actual or 'Chưa công bố'} | Forecast: {forecast or 'Chưa công bố'} | Previous: {previous or 'Chưa công bố'}"
        body+='\nGiờ sự kiện (VN): '+display_vietnam(ts)+' · kiểm tra '+display_vietnam(now.isoformat())
        deadline=at+timedelta(minutes=5) if alert=='released' else at
        for (chat_id,) in chats: store.queue_notification(f"news:{chat_id}:{event_id}:{alert}",body,chat_id,expires_at=deadline.isoformat(),priority=20)

def close_session_report(store, telegram, current_session):
    previous = store.checkpoint("last_session")
    if previous is None:
        store.set_checkpoint("last_session", current_session)
        return
    if previous == current_session:
        return
    if signal_engine_mode().startswith('intraday-'):
        from telegram_views import stats
        from intraday_live import VERSION
        from trades import NEW_YORK
        boundary=datetime.combine(datetime.fromisoformat(current_session).date(),__import__('datetime').time(17),tzinfo=NEW_YORK).astimezone(timezone.utc)
        now=datetime.now(timezone.utc)
        with store.transaction():
            if boundary<=now<boundary+timedelta(minutes=30):
                for chat_id in telegram.chat_ids:
                    store.queue_notification(f'session-summary:{chat_id}:{previous}:{VERSION}',
                        'KẾT THÚC PHIÊN\n'+stats(store.db,chat_id,previous,VERSION),chat_id,
                        expires_at=(boundary+timedelta(minutes=30)).isoformat(),priority=20)
            store.set_checkpoint('last_session',current_session)
        return
    for chat_id in telegram.chat_ids:
        key = f"session-summary:{chat_id}:{previous}"
        if store.checkpoint(key) is not None:
            continue
        count, pips, tp, sl, open_count = store.session_summary(chat_id, previous)
        if pips > 0:
            outlook, explanation = "Nghiêng BUY", "Giá vàng đang duy trì động lượng tăng, nhưng có thể xuất hiện nhịp điều chỉnh trước khi tiếp tục xu hướng."
        elif pips < 0:
            outlook, explanation = "Nghiêng SELL", "Giá vàng đang chịu áp lực giảm, nhưng có thể xuất hiện nhịp hồi trước khi tiếp tục xu hướng."
        else:
            outlook, explanation = "Trung lập", "Kết quả phiên chưa tạo ra xu hướng đủ rõ để nghiêng BUY hoặc SELL."
        day = datetime.fromisoformat(previous).strftime("%d/%m/%Y")
        body = (f"🌙 KẾT THÚC PHIÊN {SYMBOL} — {day}\n\n"
                f"📊 Kết quả giao dịch\n\n"
                f"Tổng số lệnh đã đóng: {count}\n"
                f"✅ TP: {tp}\n❌ SL: {sl}\n📈 Tổng kết: {pips:+.0f} pip\n\n"
                f"⏳ Lệnh còn đang mở: {open_count}\n"
                f"Các lệnh này sẽ không được tính vào kết quả hôm nay.\n\n"
                f"🔮 Nhận định phiên tiếp theo\n\n"
                f"Xu hướng tham khảo: {outlook}\nMức độ tin cậy: Trung bình\n\n"
                f"{explanation} Nên chờ tín hiệu xác nhận từ các khung thời gian đang bật.\n\n"
                f"⚠️ Đây là nhận định tham khảo, không phải cam kết tín hiệu hay khuyến nghị đầu tư.\n\n"
                f"────────────────────\nPhiên mới đã bắt đầu.\n"
                f"Số thứ tự lệnh của phiên mới bắt đầu lại từ #1.")
        store.queue_notification(key, body, chat_id)
        store.set_checkpoint(key, current_session)
    store.set_checkpoint("last_session", current_session)


def run_once(store, market_db, telegram):
    started = time.perf_counter()
    quote = latest_quote(market_db)
    if not quote:
        return
    timestamp, bid, ask, received_at, provider_timestamp = quote
    identity = received_at or timestamp
    received = utc_timestamp(received_at or timestamp)
    at = utc_timestamp(provider_timestamp or timestamp)
    age = max(0.0, (datetime.now(timezone.utc) - received).total_seconds())
    METRICS["last_tick_age"] = round(age, 1)
    METRICS["tick_received_at"] = received.isoformat()
    METRICS["tick_provider_time"] = at.isoformat()
    if identity == store.checkpoint("tick"):
        return
    if age > 30:
        METRICS["market_closed"] = not market_open_now()
        if not market_open_now():
            return
        raise RuntimeError(f"{SYMBOL} tick is stale")
    for trade in store.update_quote(bid, ask, at):
        if os.environ.get("DRY_RUN", "true").lower() != "true":
            for chat_id in telegram.chat_ids:
                number = store.user_number(chat_id, trade.id)
                if number is not None:
                    store.queue_notification(f"close:{chat_id}:{trade.id}:{trade.status}", update_message(trade, number), chat_id)
            store.update_user_status(trade.id, trade.status)
    store.set_checkpoint("tick", identity)
    METRICS["tick_reaction_ms"] = round((time.perf_counter() - started) * 1000, 1)


def tick_worker(path, market_db, telegram, stop):
    """Own the fast path and its SQLite connection; Telegram never blocks it."""
    worker_store = TradeStore(path)
    test_path = os.environ.get("TELEGRAM_TEST_STATE_DB")
    test_chat = os.environ.get("TELEGRAM_TEST_CHAT_ID")
    test_store = None
    test_telegram = None
    if test_path or test_chat:
        if not test_path or not test_chat or Path(test_path).name != "tv-test.db" or Path(test_path).resolve() == Path(path).resolve() or test_chat in telegram.chat_ids:
            worker_store.close()
            raise ValueError("isolated TEST outbox required")
        test_store = TradeStore(test_path)
        test_telegram = telegram
    try:
        while not stop.is_set():
            try:
                run_once(worker_store, market_db, telegram)
                METRICS.pop("tick_error", None)
            except (RuntimeError, ValueError, sqlite3.Error) as exc:
                METRICS["tick_error"] = str(exc)
                if time.monotonic()-ERROR_LOG_AT.get("tick", 0) >= 60:
                    print(f"bot tick: {exc}", flush=True)
                    ERROR_LOG_AT["tick"] = time.monotonic()
            finally:
                METRICS["heartbeats"]["tick"] = datetime.now(timezone.utc).isoformat()
            stop.wait(0.25)
    finally:
        worker_store.close()


def average_true_range(bars, period=14):
    if len(bars) < period + 1:
        return 0.0
    ranges = [max(bar.high - bar.low, abs(bar.high - previous.close),
                   abs(bar.low - previous.close))
              for previous, bar in zip(bars[-period - 1:-1], bars[-period:])]
    return sum(ranges) / len(ranges)


def bar_received_time(market_db, timeframe, timestamp):
    try:
        with closing(sqlite3.connect(market_db)) as db:
            row = db.execute(
                "SELECT received_at_utc FROM bars WHERE symbol=? AND timeframe=? AND timestamp=?",
                (SYMBOL, timeframe, timestamp),
            ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def process_early_warnings(store, market_db, telegram):
    if os.environ.get("EARLY_WARNING_ENABLED", "false").lower() != "true":
        return
    quote = latest_quote(market_db)
    if not quote:
        return
    _, bid, ask, _, _ = quote
    now = datetime.now(timezone.utc)
    for timeframe, minutes in zip(EXECUTION_TIMEFRAMES, (1, 3, 5, 15, 30, 60)):
        try:
            with closing(sqlite3.connect(market_db)) as db:
                latest = db.execute("SELECT MAX(timestamp) FROM bars WHERE symbol=? AND timeframe=?",
                                    (SYMBOL, timeframe)).fetchone()[0]
            bars = BAR_CACHE.get(timeframe)
            if bars is None or bars[-1].timestamp != latest:
                bars = load_bars(market_db, SYMBOL, timeframe, 2000)
                BAR_CACHE[timeframe] = bars
        except (ValueError, sqlite3.Error):
            continue
        last = bars[-1]
        minute = (now.minute // minutes) * minutes
        start = now.replace(minute=minute, second=0, microsecond=0)
        forming = Bar(start.isoformat(), last.close, max(last.close, bid, ask),
                      min(last.close, bid, ask), (bid + ask) / 2, 0.0)
        events = replay_events(bars + [forming], timeframe, SYMBOL)
        for event in events:
            if event.confirmation_time != forming.timestamp:
                continue
            direction = "BUY" if event.signal.endswith("BUY") else "SELL"
            key = f"early_warning:{SYMBOL}:{timeframe}:{forming.timestamp}:{direction}"
            if not store.mark_early_warning(key, SYMBOL, timeframe, direction, forming.timestamp):
                METRICS["early_warning"]["deduplicated"] += 1
                continue
            METRICS["early_warning"]["sent"] += 1
            if telegram and os.environ.get("EARLY_WARNING_TELEGRAM", "false").lower() == "true":
                body = (f"EARLY WARNING {SYMBOL} {timeframe.upper()} {event.signal}\n"
                        "Chưa phải tín hiệu giao dịch chính thức.\n"
                        "Nến hiện tại chưa đóng.\n"
                        f"Giá tham chiếu: {(bid + ask) / 2:.2f}")
                for chat_id in telegram.chat_ids:
                    store.queue_notification(key + ":" + str(chat_id), body, chat_id)


def process_signals(store, market_db, telegram):
    if os.environ.get("PARITY_APPROVED", "false").lower() != "true":
        METRICS["timeframes"] = {tf: {"ready": False, "error": "PARITY_NOT_APPROVED"} for tf in EXECUTION_TIMEFRAMES}
        return
    chat_ids = telegram.chat_ids if telegram else []
    if chat_ids:
        store.ensure_users(chat_ids, SYMBOL)
    METRICS["signals_detected"] = 0
    METRICS["signals_created"] = 0
    METRICS["signals_duplicate"] = 0
    METRICS["signals_skipped_stale"] = 0
    statuses = METRICS.setdefault("timeframes", {})
    for timeframe in EXECUTION_TIMEFRAMES:
        try:
            process_timeframe(store, market_db, telegram, timeframe)
            quality = METRICS["bars"].get(timeframe, {})
            statuses[timeframe] = {"ready": quality.get("ready", True),
                                   "error": None if quality.get("ready", True) else quality.get("quality", "NOT_READY"),
                                   "updated_at": datetime.now(timezone.utc).isoformat()}
        except Exception as exc:
            statuses[timeframe] = {"ready": False, "error": f"{type(exc).__name__}: {exc}",
                                   "updated_at": datetime.now(timezone.utc).isoformat()}
            key = f"{timeframe}:{type(exc).__name__}:{exc}"
            now = time.monotonic()
            if now - ERROR_LOG_AT.get(key, 0) >= 60:
                print(f"bot {timeframe}: {key}", flush=True)
                ERROR_LOG_AT[key] = now


def process_timeframe(store, market_db, telegram, timeframe):
    chat_ids = telegram.chat_ids if telegram else []
    with store.transaction():
        with closing(sqlite3.connect(market_db)) as db:
            latest = db.execute(
                "SELECT MAX(timestamp) FROM bars WHERE symbol=? AND timeframe=?",
                (SYMBOL, timeframe),
            ).fetchone()[0]
        if latest is None:
            raise ValueError("no bars")
        quality_detail = refresh_bar_health(market_db, timeframe)
        now = time.monotonic()
        if latest == (store.checkpoint(f"bar_checkpoint:{timeframe}") or store.checkpoint(f"bar:{timeframe}")):
            return
        SIGNAL_SCAN_AT[timeframe] = now
        quality = quality_detail.status
        ready = quality_detail.ready
        age = quality_detail.age_sec
        METRICS.setdefault("bars", {})[timeframe] = {"age_sec": round(age, 1) if age is not None else None, "quality": quality, "ready": ready,
                                                       "gap_count": quality_detail.gap_count,
                                                       "duplicate_count": quality_detail.duplicate_count}
        if not ready:
            METRICS["signals_skipped_stale"] += 1
            now = time.monotonic()
            if now - STALE_LOG_AT.get(timeframe, 0) >= 60:
                label = quality.lower() if quality != "OK" else "stale"
                age_text = f"{age:.1f}s" if age is not None else "unknown age"
                print(f"bot: {label} {timeframe} bar ({age_text})", flush=True)
                STALE_LOG_AT[timeframe] = now
            return
        bars = BAR_CACHE.get(timeframe)
        if bars is None or bars[-1].timestamp != latest:
            bars = load_bars(market_db, SYMBOL, timeframe, 2000)
            BAR_CACHE[timeframe] = bars
        checkpoint_name = f"bar_checkpoint:{timeframe}"
        checkpoint = store.checkpoint(checkpoint_name) or store.checkpoint(f"bar:{timeframe}")
        if checkpoint is None:
            store.set_checkpoint(checkpoint_name, bars[-1].timestamp)
            return
        replay_from = bars[max(0, len(bars) - REPLAY_WINDOW[timeframe])].timestamp
        watermark_name = f"signal_watermark:{timeframe}"
        watermark = store.checkpoint(watermark_name)
        latest_event_time = watermark
        engine_mode = signal_engine_mode()
        if engine_mode == "release1" and os.environ.get("RELEASE1_PARITY_APPROVED", "false").lower() != "true":
            raise ValueError("Release 1 active mode refused: parity not approved")
        release_signals = None
        lifecycle = []
        if engine_mode in {"release1-shadow", "release1"}:
            persisted = store.load_engine_state(SYMBOL, timeframe, RELEASE_ENGINE_VERSION)
            release_signals, lifecycle, state = replay_release1(
                bars, timeframe, SYMBOL,
                two_stage_exit=os.environ.get("RELEASE1_TWO_STAGE_EXIT", "false").lower() == "true",
                trend_dca=os.environ.get("RELEASE1_TREND_DCA", "false").lower() == "true",
                initial_state=persisted["state"] if persisted else None,
                start_after=persisted["last_bar_timestamp"] if persisted else None)
            for item in lifecycle:
                store.record_trade_event(
                    event_key=f"shadow:{item['event_key']}" if engine_mode == "release1-shadow" else item["event_key"],
                    event_type=item["event_type"], event_time=item["event_time"],
                    campaign_key=item.get("campaign_key"),
                    price=to_price(item.get("price_ticks")) if item.get("price_ticks") is not None else None,
                    payload=item,
                )
            store.save_engine_state(SYMBOL, timeframe, RELEASE_ENGINE_VERSION,
                                    bars[-1].timestamp, state)
        selected_events = release_signals if engine_mode == "release1" else replay_events(bars, timeframe, SYMBOL)
        for event in selected_events:
            if event.execution_time < replay_from:
                continue
            METRICS["signals_detected"] += 1
            at = datetime.fromisoformat(event.execution_time.replace("Z", "+00:00"))
            eligible_at = at + (timedelta(seconds=BAR_SECONDS[timeframe])
                                if event.event_kind == "CLOUD_INFO" else timedelta(0))
            deadline = eligible_at + timedelta(seconds=5)
            if SYMBOL == "XAUUSD" and datetime.now(timezone.utc) > deadline:
                store.record_trade_event(
                    event_key="archived:" + event.event_key, event_type="HISTORICAL_SIGNAL",
                    event_time=event.execution_time,
                    payload={"signal": event.signal, "timeframe": event.timeframe,
                             "reason": "DELIVERY_WINDOW_EXPIRED", "event": vars(event)})
                METRICS["signals_skipped_stale"] += 1
                continue
            if event.event_kind == "CLOUD_INFO":
                info, created = store.create_info(
                    event_key=event.event_key, timeframe=event.timeframe,
                    signal=event.signal, reference=to_price(event.reference_ticks), at=at)
                if not created:
                    METRICS["signals_duplicate"] += 1
                    continue
                METRICS["signals_created"] += 1
                METRICS["last_signal_at"] = event.execution_time
                if os.environ.get("DRY_RUN", "true").lower() != "true":
                    for chat_id in telegram.chat_ids:
                        if store.user_enabled(chat_id, SYMBOL, timeframe):
                            store.queue_notification(
                                f"info:{chat_id}:{event.event_key}", info_message(info), chat_id,
                                expires_at=deadline.isoformat() if SYMBOL == "XAUUSD" else None)
                latest_event_time = max(latest_event_time or event.execution_time, event.execution_time)
                continue
            quote = latest_quote(market_db)
            signal_started = time.perf_counter()
            metadata = {"confirmation_time": event.confirmation_time,
                        "signal_created_time": datetime.now(timezone.utc).isoformat(),
                        "planned_entry": to_price(event.entry_ticks),
                        "telegram_queued_time": datetime.now(timezone.utc).isoformat(),
                        "bar_received_time": bar_received_time(market_db, timeframe, event.confirmation_time),
                        "engine_version": event.engine_version,
                        "config_hash": event.config_hash,
                        "event_kind": event.event_kind,
                        "campaign_key": event.campaign_key,
                        "family": event.family, "subtype": event.subtype,
                        "grade": event.grade, "quality": event.quality,
                        "initial_sl": to_price(event.sl_ticks),
                        "tp1": to_price(event.tp1_ticks) if event.tp1_ticks is not None else None,
                        "tp2": to_price(event.tp_ticks)}
            if quote:
                _, bid, ask, received_at, provider_timestamp = quote
                spread = max(0.0, ask - bid)
                observation = slippage_observation(event.signal, metadata["planned_entry"], bid, ask,
                                                   spread, average_true_range(bars), provider_timestamp)
                metadata.update(current_bid=bid, current_ask=ask, spread=spread,
                                tick_age=observation["tick_age"], price_delta=observation["delta"],
                                slippage_warning=observation["warning"],
                                bar_received_time=metadata.get("bar_received_time"))
                if observation["warning"]:
                    METRICS["slippage"]["warnings"] += 1
                    METRICS["slippage"]["last_delta"] = observation["delta"]
                try:
                    confirmation = utc_timestamp(event.confirmation_time)
                    received_dt = utc_timestamp(metadata.get("bar_received_time") or received_at)
                    signal_dt = utc_timestamp(metadata["signal_created_time"])
                    METRICS["signal_latency"] = {
                        "bar_to_receive_ms": round(max(0.0, (received_dt - confirmation).total_seconds()) * 1000, 1),
                        "receive_to_signal_ms": round(max(0.0, (signal_dt - received_dt).total_seconds()) * 1000, 1),
                    }
                except (TypeError, ValueError):
                    pass
            trade, created = store.create(
                event_key=event.event_key, timeframe=event.timeframe, signal=event.signal,
                entry=metadata["planned_entry"], sl=to_price(event.sl_ticks), tp=to_price(event.tp_ticks),
                at=at, metadata=metadata)
            if not created:
                METRICS["signals_duplicate"] += 1
                continue
            if engine_mode == "release1" and event.campaign_key:
                for item in owned_lifecycle_events(lifecycle, event):
                    if item.get("campaign_key") == event.campaign_key:
                        store.apply_trade_event(
                            trade.id, item,
                            to_price(item["price_ticks"]) if item.get("price_ticks") is not None else None)
                trade = store._trade(store.db.execute("SELECT * FROM trades WHERE id=?", (trade.id,)).fetchone())
            METRICS["signals_created"] += 1
            METRICS["last_signal_at"] = event.execution_time
            if created and os.environ.get("DRY_RUN", "true").lower() != "true":
                for chat_id in telegram.chat_ids:
                    if store.user_enabled(chat_id, SYMBOL, timeframe):
                        number, _ = store.create_user_signal(chat_id, trade)
                        store.queue_notification(f"open:{chat_id}:{event.event_key}", entry_message(trade, number), chat_id,
                                                 expires_at=deadline.isoformat() if SYMBOL == "XAUUSD" else None)
                        if trade.status != "OPEN":
                            store.queue_notification(
                                f"close:{chat_id}:{trade.id}:{trade.status}",
                                update_message(trade, number), chat_id)
                if trade.status != "OPEN":
                    store.update_user_status(trade.id, trade.status)
                METRICS["signal_latency"]["signal_to_queue_ms"] = round(
                    (time.perf_counter() - signal_started) * 1000, 1)
            latest_event_time = max(latest_event_time or event.execution_time, event.execution_time)
        store.set_checkpoint(checkpoint_name, bars[-1].timestamp)
        if latest_event_time is not None:
            store.set_checkpoint(watermark_name, latest_event_time)

def main():
    if signal_engine_mode() in {"intraday-live", "intraday-shadow"}:
        from intraday_live import run
        return run()
    if signal_engine_mode() in {"pine-shadow", "pine-live", "pine-test"}:
        from pine_runtime import run
        return run()
    dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not dry_run and (not token or not chat_id):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    store = TradeStore(os.environ.get("TRADES_DB", "trades.db"))
    market_path = str(Path(os.environ.get("MARKET_DB", "market.db")).expanduser().resolve())
    migrate_market_db(market_path)
    lock_file = None
    if fcntl:
        lock_file = open(f"/tmp/{SYMBOL.lower()}-bot.lock", "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"Another {SYMBOL} bot instance is already running")
    telegram = None if dry_run else Telegram(token, chat_id)
    if telegram:
        try:
            telegram.set_commands()
        except Exception as exc:
            print(f"telegram command menu: {type(exc).__name__}: {exc}", flush=True)
    stop = __import__("threading").Event()
    Thread(target=tick_worker, args=(store.path, market_path, telegram, stop), daemon=True).start()
    if telegram and SYMBOL == "XAUUSD":
        Thread(target=telegram.poll, args=(store, stop), daemon=True).start()
        Thread(target=notification_worker, args=(store.path, telegram, stop), daemon=True).start()
    health_server = start_health_server()
    last_news_check = 0.0
    try:
        while True:
            try:
                if telegram:
                    close_session_report(store, telegram, session_key(datetime.now(timezone.utc)))
                cycle_started = time.perf_counter()
                process_signals(store, market_path, telegram)
                process_early_warnings(store, market_path, telegram)
                METRICS["last_signal_cycle_ms"] = round((time.perf_counter() - cycle_started) * 1000, 1)
                METRICS["heartbeats"]["signal"] = datetime.now(timezone.utc).isoformat()
                if telegram:
                    q = store.pending_notifications()
                    counts = store.notification_counts()
                    METRICS["telegram_pending"] = len(q)
                    METRICS["telegram_failed"] = counts.get("FAILED", 0) + counts.get("DEAD", 0)
                    METRICS["telegram_unknown"] = counts.get("UNKNOWN", 0)
                    METRICS["telegram_dead"] = counts.get("DEAD", 0)
                    if time.monotonic() - last_news_check >= 30:
                        queue_news_alerts(store, NEWS_DB)
                        last_news_check = time.monotonic()
                news = news_health(NEWS_DB) if SYMBOL == "XAUUSD" else {}
                telegram_heartbeat = METRICS.get("heartbeats", {}).get("telegram")
                telegram_ok = True
                if telegram and telegram_heartbeat:
                    telegram_ok = (datetime.now(timezone.utc) - utc_timestamp(telegram_heartbeat)).total_seconds() < 90
                tick_age = METRICS.get("last_tick_age")
                market_closed = not market_open_now()
                health = worker_health(METRICS, telegram is not None)
                frames_ready = all(METRICS.get("timeframes", {}).get(tf, {}).get("ready", False) for tf in EXECUTION_TIMEFRAMES)
                ready = health["alive"] and frames_ready and tick_age is not None and tick_age <= 30
                STATUS.update(ok=ready, alive=health["alive"], ready=ready, degraded=not ready,
                              workers=health["workers"], timeframes=METRICS.get("timeframes", {}),
                              error=None if ready else "worker, timeframe or feed not ready",
                              service="xauusd-bot", openapi_connected=tick_age is not None and tick_age <= 30,
                              market_closed=market_closed,
                              dry_run=dry_run,
                              parity_approved=os.environ.get("PARITY_APPROVED", "false").lower() == "true",
                              updated_at=datetime.now(timezone.utc).isoformat(),
                              uptime_sec=round(time.monotonic() - START_MONOTONIC, 1),
                              data_source=os.environ.get("MARKET_SOURCE", "ctrader-openapi"),
                              source_acceptance=os.environ.get("SOURCE_ACCEPTANCE", "existing"),
                              signal_engine={"mode": signal_engine_mode(),
                                             "version": RELEASE_ENGINE_VERSION,
                                             "config_hash": RELEASE_CONFIG_HASH,
                                             "parity_approved": os.environ.get("RELEASE1_PARITY_APPROVED", "false").lower() == "true"},
                              metrics=METRICS.copy(), news=news,
                              tick={"age_sec": METRICS.get("last_tick_age"),
                                    "reaction_ms": METRICS.get("tick_reaction_ms"),
                                    "received_at": METRICS.get("tick_received_at"),
                                    "provider_time": METRICS.get("tick_provider_time")},
                              signal_latency=METRICS.get("signal_latency", {}),
                              slippage=METRICS.get("slippage", {}),
                              early_warning=METRICS.get("early_warning", {}),
                              heartbeats=METRICS.get("heartbeats", {}),
                              checkpoints={tf: store.checkpoint(f"bar_checkpoint:{tf}") or store.checkpoint(f"bar:{tf}") for tf in EXECUTION_TIMEFRAMES})
            except (RuntimeError, ValueError, sqlite3.Error) as exc:
                STATUS.update(ok=False, error=str(exc), updated_at=datetime.now(timezone.utc).isoformat())
                now = time.monotonic()
                key = str(exc)
                if now - ERROR_LOG_AT.get(key, 0) >= 60:
                    print(f"bot: {exc}", flush=True)
                    ERROR_LOG_AT[key] = now
            time.sleep(1)
    finally:
        health_server.shutdown()
        stop.set()
        store.close()
        if lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()


if __name__ == "__main__":
    main()
