"""Private USD risk preferences and indicative standard-XAUUSD sizing; no orders."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
import re

BUTTON = '💰 Vốn & rủi ro'
LIMIT = Decimal('1000000000')


def number(value, *, allow_zero=False):
    if isinstance(value, bool):
        raise ValueError('Giá trị không hợp lệ')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Giá trị không hợp lệ') from None
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0) or result > LIMIT:
        raise ValueError('Giá trị phải hữu hạn, dương và không quá 1 tỷ')
    return result


def entered_number(text):
    if not re.fullmatch(r'\d{1,10}(?:[.,]\d{1,4})?', text.strip()):
        raise ValueError('Nhập số dương, không dùng dấu phân cách hàng nghìn')
    return number(text.strip().replace(',', '.'))


def display(value):
    text = format(Decimal(str(value)), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def profile(store, chat_id):
    row = store.db.execute('SELECT chat_id,capital_usd,risk_mode,risk_value,updated_at FROM user_risk_settings WHERE chat_id=?', (str(chat_id),)).fetchone()
    return dict(zip(('chat_id','capital_usd','risk_mode','risk_value','updated_at'), row)) if row else None


def save(store, chat_id, field, value):
    with store.transaction():
        current = profile(store, chat_id) or dict(chat_id=str(chat_id), capital_usd=None, risk_mode=None, risk_value=None)
        if field == 'capital':
            current['capital_usd'] = str(entered_number(value))
        elif field == 'risk':
            match = re.fullmatch(r'(.+?)\s*(%|usd)', value.strip(), re.IGNORECASE)
            if not match:
                raise ValueError('Ghi rõ đơn vị: /ruiro 1% hoặc /ruiro 10usd')
            risk = entered_number(match[1])
            mode = 'percent' if match[2] == '%' else 'usd'
            if mode == 'percent' and risk > 100:
                raise ValueError('Rủi ro phần trăm không được vượt 100%')
            current.update(risk_mode=mode, risk_value=str(risk))
        else:
            raise ValueError('Cài đặt không hợp lệ')
        if current['capital_usd'] and current['risk_mode'] == 'usd' and Decimal(current['risk_value']) > Decimal(current['capital_usd']):
            raise ValueError('Số tiền rủi ro không được vượt vốn đã nhập')
        store.db.execute('''INSERT INTO user_risk_settings(chat_id,capital_usd,risk_mode,risk_value,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET capital_usd=excluded.capital_usd,
            risk_mode=excluded.risk_mode,risk_value=excluded.risk_value,updated_at=excluded.updated_at''',
            (str(chat_id),current['capital_usd'],current['risk_mode'],current['risk_value'],datetime.now(timezone.utc).isoformat()))


def summary(store, chat_id):
    p = profile(store, chat_id) or {}
    capital = display(p['capital_usd'])+' USD' if p.get('capital_usd') else 'chưa nhập'
    risk = display(p['risk_value'])+('% vốn' if p.get('risk_mode')=='percent' else ' USD/lệnh') if p.get('risk_value') else 'chưa nhập'
    return (f'{BUTTON}\nXAUUSD chuẩn · vốn USD (không dùng số dư Cent)\n'
            f'Vốn: {capital}\nRủi ro: {risk}\n\n'
            '/von 1000 — nhập/cập nhật vốn USD\n'
            '/ruiro 1% — tự chọn phần trăm vốn mỗi lệnh\n'
            '/ruiro 10usd — hoặc số tiền cố định mỗi lệnh\n'
            '/lot — xem cài đặt\n/lot off — xóa cài đặt lot\n\n'
            'Các số trên chỉ là ví dụ. Áp dụng cho tín hiệu mới sau khi lưu đủ vốn và rủi ro. '
            'Vốn không tự tăng/giảm theo kết quả mô phỏng. Bot không đặt lệnh và không kiểm tra ký quỹ.')


def handle(telegram, store, item, chat):
    text = item.get('text', '').strip()
    parts = text.split(maxsplit=1)
    command = parts[0].split('@',1)[0].lower() if parts else ''
    if text != BUTTON and command not in {'/von','/ruiro','/lot'}:
        return False
    if chat.get('type') != 'private' or str(item.get('from',{}).get('id')) != str(chat.get('id')):
        telegram.send_to(chat['id'], 'Vốn và rủi ro chỉ được cài đặt trong chat riêng với bot.')
        return True
    try:
        if command in {'/von','/ruiro'}:
            if len(parts) != 2:
                raise ValueError('Dùng /von 1000 (USD) hoặc /ruiro 1% hay /ruiro 10usd')
            save(store, chat['id'], 'capital' if command=='/von' else 'risk', parts[1])
        elif command == '/lot' and len(parts)==2:
            if parts[1].lower() != 'off':
                raise ValueError('Dùng /lot để xem hoặc /lot off để xóa')
            with store.transaction():
                store.db.execute('DELETE FROM user_risk_settings WHERE chat_id=?', (str(chat['id']),))
        telegram.send_to(chat['id'], summary(store,chat['id']))
    except ValueError as exc:
        telegram.send_to(chat['id'], 'Chưa lưu: '+str(exc))
    return True


def market_spec(db):
    try:
        row = db.execute("SELECT value FROM market_meta WHERE key='conditions'").fetchone()
        c = json.loads(row[0]) if row else {}
        return {k:c[k] for k in ('instrument','quote_currency','contract_size','volume_min','volume_max','volume_step')}
    except (KeyError, TypeError, ValueError):
        return None


def calculate(p, spec, price, sl, direction, slippage=0, commission=0):
    if spec.get('instrument')!='XAUUSD' or spec.get('quote_currency')!='USD':
        raise ValueError('Thiếu quy cách XAUUSD/USD hợp lệ')
    capital = number(p['capital_usd']);risk_value=number(p['risk_value'])
    if p['risk_mode']=='percent' and risk_value<=100:
        budget=capital*risk_value/100
    elif p['risk_mode']=='usd':
        budget=risk_value
    else:
        raise ValueError('Rủi ro không hợp lệ')
    if budget>capital:
        raise ValueError('Rủi ro vượt vốn')
    contract=number(spec['contract_size']);minimum=number(spec['volume_min'])
    maximum=number(spec['volume_max']);step=number(spec['volume_step'])
    if minimum>maximum or minimum%step or direction not in (-1,1) or isinstance(direction,bool):
        raise ValueError('Quy cách lot không hợp lệ')
    distance=(number(price)-number(sl))*direction
    if distance<=0:
        raise ValueError('SL không nằm về phía rủi ro')
    # Current executable quote already includes the bid/ask spread; do not add it twice.
    per_lot=(distance+2*number(slippage,allow_zero=True)+number(commission,allow_zero=True))*contract
    raw=budget/per_lot
    lots=(min(raw,maximum)/step).to_integral_value(rounding=ROUND_FLOOR)*step
    if lots<minimum:
        return dict(state='BELOW_MINIMUM',budget_usd=str(budget),minimum_lot=str(minimum),minimum_risk_usd=str(minimum*per_lot))
    return dict(state='OK',lot=str(lots),budget_usd=str(budget),estimated_risk_usd=str(lots*per_lot),
                price=str(price),sl=str(sl),capital_usd=str(capital),risk_mode=p['risk_mode'],risk_value=str(risk_value))


def annotation(config, check, chat_id):
    p=config.get('sizing_profile')
    if not p or not p.get('capital_usd') or not p.get('risk_value'):
        return '\n\nLot: chưa cài đặt — dùng /von và /ruiro trong chat riêng.', {'state':'UNCONFIGURED'}
    if str(p.get('chat_id'))!=str(chat_id):
        return '\n\nLot: không tính được do cấu hình người nhận không khớp.', {'state':'INVALID_PROFILE'}
    try:
        result=calculate(p,config.get('sizing_spec') or {},check['price'],config['sl'],config['direction'],
                         config['slippage'],config['commission'])
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return '\n\nLot: chưa tính được; cần kiểm tra vốn/rủi ro và quy cách sản phẩm.', {'state':'UNAVAILABLE'}
    if result['state']=='BELOW_MINIMUM':
        return (f"\n\nLot: không đủ ngân sách rủi ro cho mức tối thiểu {display(result['minimum_lot'])}."
                f"\nNgân sách {float(result['budget_usd']):.2f} USD; lot tối thiểu cần khoảng {float(result['minimum_risk_usd']):.2f} USD tại SL."
                '\nKhông tự nâng lot.',result)
    text=(f"\n\nLot tham khảo của bạn: {result['lot']} lot"
          f"\nVốn {display(p['capital_usd'])} USD · rủi ro chọn {display(p['risk_value'])}{'%' if p['risk_mode']=='percent' else ' USD'}"
          f"\nLỗ ước tính tại SL: {float(result['estimated_risk_usd']):.2f} / {float(result['budget_usd']):.2f} USD"
          '\nTheo giá kiểm tra và SL; làm tròn xuống. Có dự phòng trượt giá theo engine; chưa gồm phí riêng của tài khoản/ký quỹ. Giá gap có thể làm lỗ vượt ước tính.')
    return text,result
