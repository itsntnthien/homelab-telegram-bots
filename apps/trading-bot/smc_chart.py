"""Read-only SMC chart; reuse the engine's structure lifecycle, never guess zones."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import io
from pine_engine import PineEngine
from pine_config import PineConfig
from signal_engine import Bar


def capture(state):
    return deepcopy({k:state[k] for k in ('count','liquidity','gaps','blocks','trend','high_pivot','low_pivot')})


def reconstruct(rows, timeframe):
    engine=PineEngine(timeframe.lower(),PineConfig(tick_size=.001));events=[]
    for row in rows:
        bar=Bar(*row,0.)
        before={p['id']:dict(p) for p in engine.s['liquidity']}
        engine._market(bar)
        event,broken=engine._structure(bar)
        index=engine.s['count']
        if event:events.append(dict(bar=index,level=broken,kind='CHoCH' if abs(event)==2 else 'BOS',side=1 if event>0 else -1))
        for p in engine.s['liquidity']:
            if p['swept'] and p['id'] in before and not before[p['id']]['swept']:
                events.append(dict(bar=index,level=p['level'],kind='SWP',side=p['side']))
    result=capture(engine.s)
    result['events']=[dict(e,relative=e['bar']-engine.s['count']) for e in events[-100:]]
    return result


def label_positions(anchors,top,bottom,gap=28):
    if not anchors:return []
    gap=min(gap,(bottom-top)/max(1,len(anchors)-1))
    result=[]
    for anchor in anchors:result.append(max(top,anchor,result[-1]+gap if result else top))
    if result[-1]>bottom:
        result[-1]=bottom
        for i in range(len(result)-2,-1,-1):result[i]=min(result[i],result[i+1]-gap)
    return result


def render_chart(record,rows):
    from PIL import Image,ImageDraw,ImageFont
    from setup_view import chart_indicators
    from telegram_views import vn
    from trades import MODEL_NAMES
    if len(rows)<2:raise ValueError('at least two confirmed candles required')
    replay=reconstruct(rows,record['timeframe'])
    saved=record.get('evidence',{}).get('position',{}).get('chart_context')
    state=saved or replay
    bands,_=chart_indicators(rows)
    offset=max(0,len(rows)-80);visible=rows[offset:];n=len(visible)
    bands=[(i-offset,b,u,l) for i,b,u,l in bands if i>=offset]
    img=Image.new('RGB',(1800,1100),'#ffffff');d=ImageDraw.Draw(img,'RGBA')
    fontpath=next((str(p) for p in (Path('C:/Windows/Fonts/arial.ttf'),Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')) if p.exists()),None)
    def font(size):return ImageFont.truetype(fontpath,size) if fontpath else ImageFont.load_default(size=size)
    def text(x,y,value,size=20,color='#334155'):d.text((x,y),value,font=font(size),fill=color)
    title=MODEL_NAMES.get(record['subtype'],'Unknown').split(' — ')[-1]
    side='BUY' if 'BUY' in record['signal'] else 'SELL';sidecolor='#089981' if side=='BUY' else '#f23645'
    text(38,24,f"XAUUSD  /  {record['timeframe']}",34)
    text(365,34,'EXNESS  ·  '+record['strategy'],20,'#7c899b')
    text(38,74,f"{side} #{record['user_number']}  ·  {title}",23,sidecolor)
    for xx,name,key,color in ((38,'Entry','entry','#2962ff'),(385,'SL','sl','#d95160'),(690,'TP','tp','#168879')):
        text(xx,118,f'{name}  {record[key]:.3f}',24,color)
    votes=record.get('evidence',{}).get('position',{}).get('supporting_confirmations',[])
    ctx={v.get('timeframe','?').upper():v for v in votes}
    parts=[tf+(' ↑' if v.get('regime') in (3,7) else ' ↓' if v.get('regime') in (4,8) else ' ✓') for tf,v in ctx.items()]
    text(1170,30,'  /  '.join(parts) or 'Chưa lưu đồng thuận',23)
    text(1170,73,f"FVG {len(state['gaps'])}   ·   OB {len(state['blocks'])}   ·   Liquidity {len(state['liquidity'])}",21,'#7c899b')
    text(1170,118,f"Q {record['quality']}/100 · Điểm chất lượng",21,'#7c899b')
    left,right,top,bottom=38,1320,185,880
    candle_right=1180;width=(candle_right-left)/n
    x=lambda i:int(left+(i+.5)*width)
    prices=[v for row in visible for v in row[1:]]+[record[k] for k in ('entry','sl','tp')]
    prices.extend(item[key]*.001 for name in ('gaps','blocks') for item in state[name] if not item.get('terminal') for key in ('top','bottom'))
    prices.extend(item['level']*.001 for item in state['liquidity'] if not item.get('terminal'))
    low,high=min(prices),max(prices);pad=max((high-low)*.10,.1);low-=pad;high+=pad
    y=lambda price:int(bottom-(price-low)/(high-low)*(bottom-top))
    for i in range(9):
        price=low+(high-low)*i/8;yy=y(price)
        d.line((left,yy,right,yy),fill='#eef1f5');text(right+8,yy-10,f'{price:.3f}',17)
    for i in range(0,n,max(1,n//8)):
        xx=x(i);d.line((xx,top,xx,bottom),fill='#f2f4f7')
        text(xx-24,bottom+12,vn(visible[i][0])[:5],17)
    clipped=0;labels=[]
    def origin(item):return max(left,min(candle_right,x(n-1+item['bar']-state['count'])))
    for key,name in (('gaps','FVG'),('blocks','OB')):
        for item in state[key]:
            if item.get('terminal'):continue
            hi,lo=item['top']*.001,item['bottom']*.001
            if hi<low or lo>high:clipped+=1;continue
            bullish=item['side']==1;rgb=(8,153,129) if bullish else (242,54,69)
            alpha=9 if item.get('mitigated') else 23
            xx=origin(item);yt=max(top,y(hi));yb=min(bottom,y(lo))
            d.rectangle((xx,yt,right,yb),fill=(*rgb,alpha),outline=(*rgb,75),width=1 if name=='FVG' else 2)
            labels.append((int((yt+yb)/2),name+(' · đã chạm' if item.get('mitigated') else ' · mới'),rgb))
    for pool in state['liquidity']:
        level=pool['level']*.001
        if not low<=level<=high:clipped+=1;continue
        yy=y(level);color='#f23645' if pool['side']==-1 else '#089981'
        for xx in range(origin(pool),right,10):d.line((xx,yy,min(xx+4,right),yy),fill=color)
        labels.append((yy,('BSL' if pool['side']==-1 else 'SSL')+(' · đã quét' if pool['swept'] else ''),(242,54,69) if pool['side']==-1 else (8,153,129)))
    for column,color in ((1,'#b7bec8'),(2,'#bac9dc'),(3,'#bac9dc')):
        points=[(x(a[0]),y(a[column])) for a in bands if low<=a[column]<=high]
        if len(points)>1:d.line(points,fill=color,width=2)
    for i,(stamp,o,h,l,c) in enumerate(visible):
        xx=x(i);color='#089981' if c>=o else '#f23645'
        d.line((xx,y(h),xx,y(l)),fill=color,width=1)
        d.rectangle((xx-max(2,int(width*.28)),min(y(o),y(c)),xx+max(2,int(width*.28)),max(y(o),y(c))+1),fill=color)
    for e in replay['events']:
        i=n-1+e['relative'];price=e['level']*.001
        if i<0 or not low<=price<=high:continue
        xx=x(i);yy=y(price);color='#089981' if e['side']==1 else '#f23645'
        d.line((max(left,xx-width*4),yy,xx,yy),fill=color,width=1)
        tx=max(left,xx-32);ty=max(top,min(bottom-24,yy-25 if e['side']==-1 else yy+5))
        d.rectangle((tx-2,ty-1,tx+62,ty+21),fill=(255,255,255,220))
        text(tx,ty,e['kind'],17,color)
    # One bounded lane for every zone/price label, separate from candles and price axis.
    for name,key,color in (('ENTRY','entry',(41,98,255)),('SL','sl',(217,81,96)),('TP','tp',(22,136,121))):
        yy=y(record[key]);d.line((x(n-1),yy,right,yy),fill=(*color,200),width=2)
        labels.append((yy,f'{name}  {record[key]:.3f}',color))
    ordered=sorted(labels)
    for (anchor,label,rgb),target in zip(ordered,label_positions([v[0] for v in ordered],top+12,bottom-18)):
        d.line((right+104,anchor,1460,target,1475,target),fill=(*rgb,95),width=1)
        d.rounded_rectangle((1478,target-11,1760,target+14),radius=5,fill=(*rgb,10))
        d.line((1480,target-8,1480,target+13),fill=(*rgb,200),width=3)
        text(1492,target-10,label,19,(*rgb,255))
    xx=x(n-1);yy=y(visible[-1][2] if side=='SELL' else visible[-1][3]);labely=max(top,min(bottom-32,yy-45 if side=='SELL' else yy+18))
    d.line((xx,yy,xx,labely+15),fill=sidecolor,width=2)
    d.rounded_rectangle((xx-35,labely,xx+35,labely+30),radius=4,fill=sidecolor);text(xx-28,labely+2,side,20,'#ffffff')
    d.line((38,935,1760,935),fill='#e6eaf0')
    text(38,951,f"{vn(record['confirmation_time'])} VN   ·   {record.get('status','?')}   ·   Tối đa {record['maximum_hold_minutes']} phút",21)
    text(38,990,'Đỏ: vùng bán / BSL    ·    Xanh: vùng mua / SSL    ·    Nhạt: đã chạm    ·    SWP: quét thanh khoản',19,'#728094')
    source='Vùng từ snapshot setup' if saved else f'Vùng tái dựng từ {len(rows)} nến'
    text(38,1030,source+' · Cấu trúc lịch sử từ replay · Giờ Việt Nam · Ảnh lịch sử, không phải lệnh mới',18,'#8591a1')
    out=io.BytesIO();img.save(out,format='PNG');return out.getvalue()
