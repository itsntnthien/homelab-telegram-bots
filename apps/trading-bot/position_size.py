"""Private button-driven USD/USC accounts and indicative gold sizing; no orders."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
import re

BUTTON = '💰 Vốn & rủi ro'
USD = '💵 Tài khoản USD'
CENT = '🪙 Tài khoản Cent'
CAPITAL = '💰 Nhập vốn'
PERCENT = '📉 Rủi ro theo %'
MONEY = '💸 Rủi ro cố định'
VIEW = '👁 Xem cài đặt'
CANCEL = '↩️ Hủy / Quay lại'
DELETE = '🗑 Xóa cài đặt lot'
CONFIRM = '✅ Xác nhận xóa'
HOME = '🏠 Mở bảng điều khiển'
BUTTONS = {BUTTON,USD,CENT,CAPITAL,PERCENT,MONEY,VIEW,CANCEL,DELETE,CONFIRM}
NAVIGATION = {HOME,'📡 Trạng thái','📈 Lệnh đang mở','📋 Lịch sử','📊 Thống kê','⚙️ Cài đặt','📰 Tin tức','❓ Hướng dẫn','Menu'}
LIMIT = Decimal('1000000000')
# Reference model, not a live XAUUSDc quote/specification feed. See docs/personal-lot.md.
CENT_SPEC = dict(instrument='XAUUSDc',contract_size='1',volume_min='0.01',volume_max='200',volume_step='0.01',
                 source='exness-standard-cent-reference-20261123')


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
    if not row: return None
    result=dict(zip(('chat_id','capital_usd','risk_mode','risk_value','updated_at'), row),account_type=ui(store,chat_id)[0])
    if result['account_type']=='cent': result['cent_spec']=dict(CENT_SPEC)
    return result


def ui(store, chat_id):
    row=store.db.execute('SELECT account_type,pending FROM user_risk_ui WHERE chat_id=?',(str(chat_id),)).fetchone()
    return tuple(row) if row else ('usd',None)


def set_ui(store, chat_id, account, pending=None):
    with store.transaction():
        store.db.execute('INSERT INTO user_risk_ui VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET account_type=excluded.account_type,pending=excluded.pending',
                         (str(chat_id),account,pending))


def keyboard(pending=None):
    rows=([[CONFIRM],[CANCEL,HOME]] if pending=='delete' else [[CANCEL,HOME]] if pending else
          [[USD,CENT],[CAPITAL],[PERCENT,MONEY],[VIEW,DELETE],[HOME]])
    return dict(keyboard=[[dict(text=x) for x in row] for row in rows],resize_keyboard=True,is_persistent=True)


def money_text(value, account):
    return display(Decimal(str(value))*100)+' USC (Cent)' if account=='cent' else display(value)+' USD'


def save(store, chat_id, field, value, *, account='usd'):
    with store.transaction():
        current = profile(store, chat_id) or dict(chat_id=str(chat_id), capital_usd=None, risk_mode=None, risk_value=None)
        if field == 'capital':
            current['capital_usd'] = str(entered_number(value)/(100 if account=='cent' else 1))
        elif field == 'risk':
            match = re.fullmatch(r'(.+?)\s*(%|usd|usc)', value.strip(), re.IGNORECASE)
            if not match:
                raise ValueError('Bấm nút chọn kiểu rủi ro rồi nhập số')
            risk = entered_number(match[1])
            mode = 'percent' if match[2] == '%' else 'usd'
            if match[2].lower()=='usc':
                risk/=100
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
    account=ui(store,chat_id)[0]
    capital = money_text(p['capital_usd'],account) if p.get('capital_usd') else 'chưa nhập'
    risk = (display(p['risk_value'])+'% vốn' if p.get('risk_mode')=='percent' else money_text(p['risk_value'],account)+'/lệnh') if p.get('risk_value') else 'chưa nhập'
    kind='Exness Standard Cent · USC · XAUUSDc · cent lot' if account=='cent' else 'USD · XAUUSD · lot chuẩn'
    note=('Mô hình Cent: 1 cent lot = 0,01 lot chuẩn; bước 0,01 cent lot. Giá XAUUSD tham chiếu; đối chiếu quy cách và giá XAUUSDc trên tài khoản.\n' if account=='cent' else '')
    return (f'{BUTTON}\nTài khoản: {kind}\n'
            f'Vốn: {capital}\nRủi ro: {risk}\n\n'
            'Chọn loại tài khoản → Nhập vốn → chọn kiểu rủi ro. Chỉ gõ con số khi bot hỏi.\n'
            '100 USC = 1 USD. Đổi loại tài khoản giữ nguyên giá trị tiền và đổi loại lot; hãy nhập lại vốn nếu dùng tài khoản khác.\n\n'
            +note+
            'Áp dụng cho tín hiệu mới sau khi lưu đủ vốn và rủi ro. '
            'Vốn không tự tăng/giảm theo kết quả mô phỏng. Bot không đặt lệnh và không kiểm tra ký quỹ.')


def handle(telegram, store, item, chat):
    text = item.get('text', '').strip()
    parts = text.split(maxsplit=1)
    command = parts[0].split('@',1)[0].lower() if parts else ''
    recognized=text in BUTTONS or command in {'/von','/ruiro','/lot'}
    if chat.get('type') != 'private' or str(item.get('from',{}).get('id')) != str(chat.get('id')):
        if recognized:
            telegram.send_to(chat['id'], 'Vốn và rủi ro chỉ được cài đặt trong chat riêng với bot.')
        return recognized
    account,pending=ui(store,chat['id'])
    if not recognized and (text in NAVIGATION or command.startswith('/')):
        if pending: set_ui(store,chat['id'],account)
        return False
    if not recognized and not pending:
        return False
    try:
        if text in {USD,CENT}:
            account='cent' if text==CENT else 'usd'
            set_ui(store,chat['id'],account)
        elif text in {CAPITAL,PERCENT,MONEY}:
            pending={CAPITAL:'capital',PERCENT:'percent',MONEY:'money'}[text]
            set_ui(store,chat['id'],account,pending)
            unit='USC (Cent)' if account=='cent' else 'USD'
            prompt=(f'Nhập vốn bằng {unit}.' if pending=='capital' else 'Nhập phần trăm vốn rủi ro cho mỗi lệnh.' if pending=='percent' else f'Nhập số tiền rủi ro mỗi lệnh bằng {unit}.')
            telegram.send_to(chat['id'],prompt+'\nChỉ nhập số dương; dùng dấu chấm hoặc phẩy cho phần thập phân, không phân cách hàng nghìn.',keyboard(pending))
            return True
        elif text==DELETE:
            set_ui(store,chat['id'],account,'delete')
            telegram.send_to(chat['id'],'Xóa vốn và rủi ro đã lưu của bạn? Các tín hiệu mới sẽ không có lot cho tới khi cài đặt lại.',keyboard('delete'))
            return True
        elif text==CONFIRM:
            if pending!='delete': raise ValueError('Bấm Xóa cài đặt lot trước khi xác nhận')
            with store.transaction():
                store.db.execute('DELETE FROM user_risk_settings WHERE chat_id=?',(str(chat['id']),))
                set_ui(store,chat['id'],account)
        elif text in {BUTTON,VIEW,CANCEL}:
            set_ui(store,chat['id'],account)
        elif command in {'/von','/ruiro'}:
            if len(parts) != 2:
                telegram.send_to(chat['id'],summary(store,chat['id']),keyboard())
                return True
            # Legacy commands keep their original USD meaning; buttons use the selected account.
            save(store, chat['id'], 'capital' if command=='/von' else 'risk', parts[1])
            set_ui(store,chat['id'],account)
        elif command == '/lot' and len(parts)==2:
            if parts[1].lower() != 'off':
                raise ValueError('Bấm nút Xem cài đặt hoặc Xóa cài đặt lot')
            with store.transaction():
                store.db.execute('DELETE FROM user_risk_settings WHERE chat_id=?', (str(chat['id']),))
                set_ui(store,chat['id'],account)
        elif command=='/lot':
            set_ui(store,chat['id'],account)
        elif pending in {'capital','percent','money'}:
            with store.transaction():
                suffix='%' if pending=='percent' else 'usc' if account=='cent' else 'usd'
                save(store,chat['id'],'capital' if pending=='capital' else 'risk',text if pending=='capital' else text+suffix,account=account)
                set_ui(store,chat['id'],account)
        else:
            raise ValueError('Chọn Xác nhận xóa hoặc Hủy bằng nút')
        telegram.send_to(chat['id'], summary(store,chat['id']),keyboard())
    except ValueError as exc:
        telegram.send_to(chat['id'], 'Chưa lưu: '+str(exc),keyboard(ui(store,chat['id'])[1]))
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
    account=p.get('account_type','usd')
    if account not in {'usd','cent'}: raise ValueError('Loại tài khoản không hợp lệ')
    if account=='cent':
        cent=p.get('cent_spec') or {}
        if contract!=100 or cent.get('instrument')!='XAUUSDc' or cent.get('source')!=CENT_SPEC['source']:
            raise ValueError('Thiếu quy cách Cent đã xác định')
        contract=number(cent['contract_size']);minimum=number(cent['volume_min'])
        maximum=number(cent['volume_max']);step=number(cent['volume_step'])
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
    return dict(state='OK',lot=str(lots),account_type=account,standard_lot=str(lots/100 if account=='cent' else lots),
                budget_usd=str(budget),estimated_risk_usd=str(lots*per_lot),
                price=str(price),sl=str(sl),capital_usd=str(capital),risk_mode=p['risk_mode'],risk_value=str(risk_value))


def annotation(config, check, chat_id):
    p=config.get('sizing_profile')
    if not p or not p.get('capital_usd') or not p.get('risk_value'):
        return '\n\nLot: chưa cài đặt — bấm 💰 Vốn & rủi ro trong chat riêng.', {'state':'UNCONFIGURED'}
    if str(p.get('chat_id'))!=str(chat_id):
        return '\n\nLot: không tính được do cấu hình người nhận không khớp.', {'state':'INVALID_PROFILE'}
    try:
        result=calculate(p,config.get('sizing_spec') or {},check['price'],config['sl'],config['direction'],
                         config['slippage'],config['commission'])
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return '\n\nLot: chưa tính được; cần kiểm tra vốn/rủi ro và quy cách sản phẩm.', {'state':'UNAVAILABLE'}
    account=p.get('account_type','usd')
    label='cent lot (XAUUSDc)' if account=='cent' else 'lot chuẩn (XAUUSD)'
    if result['state']=='BELOW_MINIMUM':
        return (f"\n\nLot: không đủ ngân sách rủi ro cho mức tối thiểu {display(result['minimum_lot'])} {label}."
                f"\nNgân sách {money_text(result['budget_usd'],account)}; lot tối thiểu cần khoảng {money_text(result['minimum_risk_usd'],account)} tại SL."
                '\nKhông tự nâng lot.',result)
    risk=display(p['risk_value'])+'%' if p['risk_mode']=='percent' else money_text(p['risk_value'],account)
    text=(f"\n\nLot tham khảo của bạn: {result['lot']} {label}"
          f"\nVốn {money_text(p['capital_usd'],account)} · rủi ro chọn {risk}"
          f"\nLỗ ước tính tại SL: {money_text(result['estimated_risk_usd'],account)} / {money_text(result['budget_usd'],account)}"
          '\nTheo giá kiểm tra và SL; làm tròn xuống. Có dự phòng trượt giá theo engine; chưa gồm phí riêng của tài khoản/ký quỹ. Giá gap có thể làm lỗ vượt ước tính.')
    if account=='cent':
        text+=(f"\nTương đương {display(result['standard_lot'])} lot chuẩn. Mô hình Exness Cent: 1 cent lot = 0,01 lot chuẩn; bước 0,01 cent lot."
               '\nGiá tham chiếu XAUUSD; chưa có giá/spread XAUUSDc riêng. Đối chiếu quy cách và giá trên tài khoản Cent trước khi vào lệnh.')
    return text,result
