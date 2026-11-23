"""Read-only, chat-scoped setup evidence and historical candle image."""
import io
from contextlib import closing
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from trades import MODEL_NAMES
from telegram_views import vn


def lookup(db, chat, identity):
    if not isinstance(identity, str) or not identity.isascii() or not identity.isdigit() or len(identity)>12:
        return None
    cursor=db.execute("SELECT t.*,u.number AS user_number FROM trades t JOIN user_signals u ON u.trade_id=t.id WHERE t.id=? AND u.chat_id=?", (int(identity),str(chat)))
    row=cursor.fetchone()
    if row is None: return None
    record=dict(zip([x[0] for x in cursor.description],tuple(row)))
    event=db.execute("SELECT payload_json FROM trade_events WHERE trade_id=? AND event_type='PRIMARY_OPEN' ORDER BY event_time LIMIT 1",(record['id'],)).fetchone()
    record['evidence']=json.loads(event[0]) if event else {}
    return record


def keyboard(identity):
    return {'inline_keyboard':[[{'text':'💡 Vì sao có lệnh này?', 'callback_data':f'why:{identity}'},
                                {'text':'📊 Xem setup', 'callback_data':f'chart:{identity}'}]]}


def explain(r):
    p=r['evidence'].get('position',{})
    model=MODEL_NAMES.get(r['subtype'],'Không có metadata mô hình')
    lines=[f"💡 SETUP #{r['user_number']} · {r['strategy']} · {r['timeframe']}",
           f"{r['signal']} · {model}", f"Trạng thái hiện tại: {r['status']}",
           f"Điểm engine tại tín hiệu: {r['quality']}/100 (không phải xác suất thắng)"]
    descriptions={
        101:'Hồi tiếp diễn: giá hồi về gần đường giữa, engine tìm nến tiếp tục theo hướng xu hướng.',
        102:'Lấy lại basis: giá xuyên đường giữa rồi đóng trở lại theo hướng setup.',
        201:'Phá cấu trúc: engine ghi nhận giá đóng vượt mốc cấu trúc đã xác nhận.',
        202:'Phá vùng: engine ghi nhận điều kiện breakout theo vùng tham chiếu.',
        301:'Đảo chiều cấu trúc: engine ghi nhận thay đổi cấu trúc theo hướng lệnh.',
        302:'Đảo chiều kiệt sức: engine ghi nhận điều kiện quét cực trị và phản ứng ngược hướng.'}
    if r['subtype'] in descriptions: lines+=['', 'Ý NGHĨA MÔ HÌNH', descriptions[r['subtype']]]
    if p:
        lines+=['', 'BẰNG CHỨNG ĐÃ LƯU', 'Đồng thuận: '+', '.join(p.get('supporting_models',[]) or ['Không có']),
                'Entry: '+('Chờ giá hồi chạm mức cho phép' if p.get('deferred') else 'Tick hợp lệ đầu nến thực thi')]
        for item in p.get('supporting_confirmations',[]):
            lines.append(f"{item.get('timeframe','?').upper()} đã xác nhận: {vn(item.get('confirmed'))} (VN)")
        if p.get('dual_agreement'): lines.append('CURRENT và Q100 cùng hướng.')
    else:
        lines+=['Không có snapshot quyết định chi tiết cho tín hiệu cũ này.']
    risk=abs(r['entry']-r['sl']); reward=abs(r['tp']-r['entry'])
    lines+=['',f"Entry {r['entry']:.3f} · SL {r['sl']:.3f} · TP {r['tp']:.3f}",
            f"Khoảng SL {risk:.3f} USD · TP {reward:.3f} USD",
            f"RR hình học {reward/risk:.2f} (chưa trừ chi phí)" if risk>0 else 'RR không hợp lệ',
            f"Giữ tối đa {r['maximum_hold_minutes']} phút; đóng trước rollover.",
            'Giải thích snapshot lúc báo, không phải tín hiệu vào lại.']
    return '\n'.join(lines)


def candles(path,r):
    seconds={'M5':300,'M30':1800}.get(r['timeframe'])
    if not seconds or not r.get('confirmation_time'): return []
    cutoff=datetime.fromisoformat(r['confirmation_time'].replace('Z','+00:00')).timestamp()
    end=datetime.fromtimestamp(cutoff-seconds,timezone.utc).isoformat()
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.3)) as db:
        rows=db.execute("SELECT timestamp,open,high,low,close FROM bars WHERE symbol='XAUUSD' AND timeframe=? AND timestamp<=? AND is_closed=1 AND price_scale=1 ORDER BY timestamp DESC LIMIT 1000",(r['timeframe'].lower(),end)).fetchall()
    rows=list(reversed(rows))
    if rows and datetime.fromisoformat(rows[-1][0]).timestamp()+seconds != cutoff:
        return []
    if any(not all(math.isfinite(v) and v>0 for v in row[1:]) or row[2]<max(row[1],row[4]) or row[3]>min(row[1],row[4]) for row in rows):
        raise ValueError('invalid OHLC')
    return rows


def image_reasons(r):
    """Describe only recorded decision evidence; never infer zones from candles."""
    p=r.get('evidence',{}).get('position',{})
    descriptions={
        101:'Giá hồi về gần basis; engine nhận diện nến tiếp diễn theo hướng setup.',
        102:'Giá xuyên basis rồi đóng trở lại theo hướng setup.',
        201:'Engine ghi nhận giá đóng vượt mốc cấu trúc đã xác nhận.',
        202:'Engine ghi nhận breakout theo vùng tham chiếu.',
        301:'Engine ghi nhận thay đổi cấu trúc theo hướng lệnh.',
        302:'Engine ghi nhận quét cực trị và phản ứng đảo hướng.'}
    name=MODEL_NAMES.get(r['subtype'],'Không có mô hình').split(' — ')[-1]
    reasons=[('01  '+name,descriptions.get(r['subtype'],'Chưa lưu diễn giải mô hình này.'))]
    votes=[]
    regimes={3:'áp lực tăng',7:'xu hướng tăng',4:'áp lực giảm',8:'xu hướng giảm',1:'vùng đang hình thành',2:'đi ngang',5:'chuyển tiếp',6:'ứng viên breakout'}
    for item in p.get('supporting_confirmations',[]):
        tf=item.get('timeframe','?').upper()
        if 'regime' in item:
            meaning=regimes.get(item['regime'],'chưa rõ trạng thái')
        elif 'subtype' in item:
            meaning=MODEL_NAMES.get(item['subtype'],'mô hình chưa rõ').split(' — ')[-1]
        else:meaning='chưa lưu hướng / mô hình'
        line=f"{tf}: {meaning}"
        if line not in votes:votes.append(line)
    reasons.append(('02  Đồng thuận đã ghi nhận','; '.join(votes) if votes else 'Chưa lưu bằng chứng đồng thuận cho tín hiệu này.'))
    reasons.append(('03  Cách kích hoạt',('Giá hồi chạm mức entry trong cửa sổ retest cho phép.' if p.get('deferred') else 'Tick hợp lệ đầu nến thực thi, sau khi setup được xác nhận.') if p else 'Chưa lưu cách kích hoạt của tín hiệu này.'))
    return reasons


def chart_indicators(rows):
    """Exact rolling BB20 population SD; pivots become visible only after five right bars."""
    bands=[];pivots=[]
    for i,row in enumerate(rows):
        if i>=19:
            closes=[x[4] for x in rows[i-19:i+1]];basis=sum(closes)/20
            sd=math.sqrt(sum((x-basis)**2 for x in closes)/20)
            bands.append((i,basis,basis+2*sd,basis-2*sd))
        if i>=10:
            j=i-5;h,l=rows[j][2:4]
            # Match the tick-rounded rightmost-equal-extreme pivot contract.
            tick=lambda x:int(math.floor(x*1000+.5))
            if all(tick(h)>=tick(x[2]) for x in rows[j-5:j]) and all(tick(h)>tick(x[2]) for x in rows[j+1:i+1]):pivots.append(('H',j,i,h))
            if all(tick(l)<=tick(x[3]) for x in rows[j-5:j]) and all(tick(l)<tick(x[3]) for x in rows[j+1:i+1]):pivots.append(('L',j,i,l))
    return bands,pivots


def render(r,rows):
    from smc_chart import render_chart
    return render_chart(r,rows)
