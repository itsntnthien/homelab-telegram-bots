"""Read-only Telegram projections, separate from engine and delivery state."""
from datetime import date, datetime, timedelta, timezone
import calendar
import re
from zoneinfo import ZoneInfo
from trades import MODEL_NAMES


def vn(value):
    if value is None: return 'Chưa có'
    at=datetime.fromtimestamp(value,timezone.utc) if isinstance(value,(int,float)) else datetime.fromisoformat(value.replace('Z','+00:00'))
    return at.astimezone(ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%H:%M:%S %d/%m/%Y')


def health(snapshot,metrics,workers):
    result=dict(snapshot)
    reasons=[]
    if not workers['alive']: reasons.append('WORKER_NOT_READY')
    if snapshot.get('error'): reasons.append(snapshot['error'])
    if metrics.get('notification_error'): reasons.append('OUTBOX_ERROR')
    if metrics.get('telegram_oldest_pending_sec',0)>5: reasons.append('OUTBOX_DELAYED')
    if metrics.get('delivery_new_unknown',0): reasons.append('DELIVERY_UNKNOWN')
    if metrics.get('delivery_new_dead',0): reasons.append('DELIVERY_DEAD')
    if metrics.get('sending_age_seconds',0)>12: reasons.append('SENDING_STUCK')
    if metrics.get('delivery_stuck_dependencies',0): reasons.append('DEPENDENCY_STUCK')
    if snapshot.get('market_closed'): reasons.append('MARKET_CLOSED')
    result.update(alive=workers['alive'],workers=workers['workers'],
        ready=bool(snapshot.get('ready') and workers['alive'] and not reasons),degraded_reasons=reasons)
    result['ok']=result['ready'];result['degraded']=not result['ready']
    result['telegram_delivery']=metrics.get('telegram_delivery',{})
    result['notification_counts']=metrics.get('notification_counts',{})
    result['outbox_oldest_pending_sec']=metrics.get('telegram_oldest_pending_sec',0)
    result['subsystems']={
        'feed':{'age_seconds':snapshot.get('tick_age_sec'),'pending_ticks':snapshot.get('pending_ticks')},
        'engine':{'timeframes':snapshot.get('timeframes',{}),'error':snapshot.get('error')},
        'outbox':{'age_seconds':metrics.get('telegram_oldest_pending_sec',0),'error':metrics.get('notification_error')},
        'telegram':{'last_delivery':metrics.get('telegram_delivery',{}),'new_unknown':metrics.get('delivery_new_unknown',0),'new_dead':metrics.get('delivery_new_dead',0)},
        'news':snapshot.get('news',{})}
    return result


def history_day(day):
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',day):
        raise ValueError('Ngày không hợp lệ')
    chosen=date.fromisoformat(day)
    # Keep the next-day UTC boundary representable, including malformed callbacks.
    if not 1970<=chosen.year<=9998: raise ValueError('Ngày ngoài phạm vi hỗ trợ')
    start=datetime.combine(chosen,datetime.min.time(),ZoneInfo('Asia/Ho_Chi_Minh'))
    return chosen,start.astimezone(timezone.utc).isoformat(),(start+timedelta(days=1)).astimezone(timezone.utc).isoformat()


def history_filters(day=None,now=None):
    today=(now or datetime.now(timezone.utc)).astimezone(ZoneInfo('Asia/Ho_Chi_Minh')).date()
    month=(history_day(day)[0] if day else today).strftime('%Y-%m')
    return [[{'text':'Hôm nay','callback_data':f'histdate:{today.isoformat()}:1'},
             {'text':'Hôm qua','callback_data':f'histdate:{(today-timedelta(days=1)).isoformat()}:1'}],
            [{'text':'📅 Chọn ngày','callback_data':'histcal:'+month},{'text':'Tất cả','callback_data':'hist:1'}]]


def history_calendar(month):
    chosen,_,_=history_day(month+'-01')
    rows=[[{'text':name,'callback_data':'histnoop'} for name in ('T2','T3','T4','T5','T6','T7','CN')]]
    for week in calendar.monthcalendar(chosen.year,chosen.month):
        rows.append([{'text':str(day) if day else '·',
                      'callback_data':f'histdate:{month}-{day:02d}:1' if day else 'histnoop'} for day in week])
    navigation=[]
    if chosen>date(1970,1,1):
        navigation.append({'text':'◀ Tháng trước','callback_data':'histcal:'+(chosen-timedelta(days=1)).strftime('%Y-%m')})
    if chosen<date(9998,12,1):
        after=(chosen+timedelta(days=32)).replace(day=1)
        navigation.append({'text':'Tháng sau ▶','callback_data':'histcal:'+after.strftime('%Y-%m')})
    rows.append(navigation)
    rows.extend(history_filters())
    return (f'📅 CHỌN NGÀY — {chosen.month:02d}/{chosen.year}\nBấm một ngày để xem các lệnh phát trong ngày đó, theo giờ Việt Nam.',
            {'inline_keyboard':rows})


def history_action(data):
    if data=='histnoop': return 'noop',None,None
    if data.startswith('histcal:'):
        month=data.removeprefix('histcal:')
        history_day(month+'-01')
        return 'calendar',month,None
    match=re.fullmatch(r'histdate:([0-9]{4}-[0-9]{2}-[0-9]{2}):([0-9]{1,6})',data)
    if not match or not 1<=int(match[2])<=100000: raise ValueError('Ngày/trang không hợp lệ')
    history_day(match[1])
    return 'date',match[1],int(match[2])


def history(db,chat,page,opened=False,day=None):
    offset=(page-1)*10
    where=" AND t.status='OPEN'" if opened else ''
    args=[str(chat)]
    if day:
        chosen,start,end=history_day(day)
        where+=' AND t.created_at>=? AND t.created_at<?'
        args.extend((start,end))
    args.append(offset)
    rows=db.execute("""SELECT t.*,u.number AS user_number,
        (SELECT n.state FROM notifications n WHERE n.event_key='open:'||u.chat_id||':'||t.event_key) AS delivery_state
        FROM user_signals u JOIN trades t ON t.id=u.trade_id WHERE u.chat_id=?"""+
        where+' ORDER BY t.id DESC LIMIT 11 OFFSET ?', args).fetchall()
    columns=[r[0] for r in db.execute('SELECT * FROM trades LIMIT 0').description]
    # Accept both ordinary sqlite connections and row-factory connections.
    values=[dict(zip(columns+['user_number','delivery_state'],tuple(r))) for r in rows]
    title='LỆNH MÔ PHỎNG ĐANG MỞ' if opened else 'LỊCH SỬ TÍN HIỆU'
    lines=[f'{title} — Trang {page} · giờ VN']
    if day: lines.append('Ngày phát lệnh: '+chosen.strftime('%d/%m/%Y')+' · 00:00–23:59 giờ Việt Nam')
    now=datetime.now(timezone.utc).timestamp()
    for r in values[:10]:
        label={'EXPIRED':'ĐÃ BỎ LỠ','SUPPRESSED':'KHÔNG GỬI','UNKNOWN':'CHƯA XÁC MINH','SENT':'API ĐÃ XÁC NHẬN'}.get(r['delivery_state'],r['delivery_state'] or 'KHÔNG XÁC ĐỊNH')
        lines.append(f"\n#{r['user_number']} {r['signal']} · {r['strategy'] or 'Không xác định'} · {r['status']}\n{MODEL_NAMES.get(r['subtype'],'Mô hình không xác định')}\nEntry {r['entry']:.3f} · SL {r['sl']:.3f} · TP {r['tp']:.3f}\nMở {vn(r['created_at'])} · Gửi: {label}")
        if opened:
            entered=datetime.fromisoformat(r['created_at']).timestamp()
            due=min(r['flat_at'] or float('inf'),entered+r['maximum_hold_minutes']*60 if r['maximum_hold_minutes'] else float('inf'))
            lines.append(f"Đã giữ {max(0,(now-entered)/60):.0f} phút · Hạn đóng {vn(due) if due!=float('inf') else 'Không xác định'}")
        else:
            lines.append('Đóng '+vn(r['closed_at'])+ (f" · {r['realized_r']:+.3f}R mô phỏng" if r['realized_r'] is not None else ' · R chưa có'))
        lines.append('Engine: '+(r['engine_version'] or 'LEGACY'))
    if not rows: lines.append('Chưa có lệnh phù hợp.')
    return '\n'.join(lines),len(rows)>10


def stats(db,chat,session,version):
    rows=db.execute("SELECT t.strategy,t.realized_r,t.status FROM user_signals u JOIN trades t ON t.id=u.trade_id WHERE u.chat_id=? AND t.session=? AND t.engine_version=?",(str(chat),session,version)).fetchall()
    lines=[f'THỐNG KÊ MÔ PHỎNG · phiên {session}',version]
    for group in ('TỔNG','CURRENT','Q100','SCALP'):
        subset=[r for r in rows if group=='TỔNG' or r[0]==group]
        values=[r[1] for r in subset if r[2]!='OPEN' and r[1] is not None]
        wins=sum(x>0 for x in values);ties=sum(x==0 for x in values)
        lines.append(f"{group}: {len(values)} lệnh có R · {sum(values):+.3f}R · Win {wins/len(values)*100:.1f}% · Hòa {ties}" if values else group+': chưa có lệnh đóng có R')
        lines.append(f"Đang mở {sum(r[2]=='OPEN' for r in subset)} · Đóng thiếu R {sum(r[2]!='OPEN' and r[1] is None for r in subset)}")
    lines.append('Bao gồm tín hiệu đã bỏ lỡ; không phải hiệu quả tài khoản broker. Phiên bản cũ xem /history.')
    return '\n'.join(lines)


def rich_text(text):
    """Escape all content first; apply a small, mobile-friendly HTML vocabulary."""
    import html,re
    lines=text.splitlines();out=[]
    for i,line in enumerate(lines):
        safe=html.escape(line)
        if i==0 or line.startswith(('GIÁ THAM CHIẾU','THÔNG TIN TÍN HIỆU','KẾT QUẢ MÔ PHỎNG','ĐÃ BỎ LỠ','ĐỐI SOÁT','💰 KHỐI LƯỢNG CỦA BẠN','Lot tham khảo:')):
            safe='<b>'+safe+'</b>'
        elif re.fullmatch(r'(Entry|SL|TP): [0-9]+[.][0-9]+',line):
            label,value=line.split(': ',1);safe='<b>'+label+'</b>  <code>'+value+'</code>'
        out.append(safe)
    return '\n'.join(out)
