// Read-only broker transport. Only signed GET and tick subscriptions are supported.
import fs from 'node:fs';
import WebSocket from 'ws';
import {headers,host,loadKey} from './exness_signer.mjs';
const emit=e=>process.stdout.write(JSON.stringify({...e,receivedAt:Date.now()})+'\n');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const frames={m1:60,m3:180,m5:300,m15:900,m30:1800,h1:3600};
const folder=process.env.EXNESS_CREDENTIAL_DIR;
if(!folder)throw Error('CREDENTIAL_DIRECTORY_REQUIRED');
const read=n=>fs.readFileSync(folder+'/'+n,'utf8').trim();
const cfg=JSON.parse(read('connection.json').replace(/^\uFEFF/,''));
const account=String(cfg.accountId),apiKey=read('api-key.txt'),key=loadKey(read('private-key.txt'));
if(!/^[1-9][0-9]*$/.test(account))throw Error('INVALID_ACCOUNT');
let stopped=false,socket,lastRequest=0;
process.on('SIGTERM',()=>{stopped=true;socket?.terminate();});
process.on('SIGINT',()=>{stopped=true;socket?.terminate();});
async function get(base,path){
 await sleep(Math.max(0,1100-(Date.now()-lastRequest)));lastRequest=Date.now();
 const r=await fetch(host(base)+path,{headers:headers(apiKey,key,path),redirect:'error',signal:AbortSignal.timeout(15000)});
 if(r.status===429){emit({type:'status',state:'rate_limited'});await sleep(Math.max(60000,Number(r.headers.get('retry-after')||0)*1000));}
 if(!r.ok)throw Error('HTTP_'+r.status);
 return r.json();
}
let backoff=1000;
while(!stopped){
 try{
  emit({type:'status',state:'connecting'});
  const discovery=await get(cfg.gatewayUrl,'/v1/trading/access-point?account_id='+account),base=host(discovery.access_point);
  const conditions=await get(base,'/v1/configuration/accounts/'+account+'/instruments/XAUUSD/conditions');
  if(conditions.instrument!=='XAUUSD'||conditions.point_digits!==3)throw Error('INSTRUMENT_MISMATCH');
  emit({type:'metadata',conditions});
  const limits=await get(base,'/v1/configuration/accounts/'+account+'/limits');
  const cap=limits.limits?.rest?.methods?.find(x=>x.api_operation_id==='getCandleHistory')?.limits?.max_candles_per_request;
  if(!Number.isInteger(cap)||cap<1)throw Error('INVALID_LIMITS');
  async function history(tf,count,from=Date.now()){
   const q=new URLSearchParams({instrument:'XAUUSD',price_type:'bid',timeframe:tf.toUpperCase(),from:new Date(from).toISOString(),count:String(-Math.min(count,cap))});
   const r=await get(base,'/v1/market-data/accounts/'+account+'/candles?'+q);
   if(!Array.isArray(r.candles)||!r.candles.length)throw Error('EMPTY_HISTORY');
   emit({type:'history',timeframe:tf,bars:r.candles});return r.candles;
  }
  for(const tf of Object.keys(frames)){
   let from=Date.now(),total=0;
   while(total<2002&&!stopped){
    const bars=await history(tf,2002-total,from),earliest=Date.parse(bars[0].time);
    if(!Number.isFinite(earliest)||earliest>=from)throw Error('PAGINATION_STALLED');
    total+=bars.length;from=earliest-1;
   }
  }
  if(stopped)break;
  await new Promise((resolve,reject)=>{
   const path='/v1/server-events/accounts/'+account+'/ws/ticks';
   let finished=false,last=Date.now(),refreshing=false;
   const latest={};
   const finish=e=>{if(finished)return;finished=true;clearInterval(timer);socket.terminate();e?reject(e):resolve();};
   socket=new WebSocket(base.replace('https:','wss:')+path,'exness-ws-protocol',{headers:headers(apiKey,key,path),handshakeTimeout:15000,followRedirects:false});
   const timer=setInterval(async()=>{
    if(refreshing||finished)return;refreshing=true;
    try{
     for(const [tf,seconds] of Object.entries(frames)){
      if(finished)break;
      const boundary=Math.floor(Date.now()/1000/seconds)*seconds;
      if(latest[tf]===boundary)continue;
      const rows=await history(tf,4);
      if(rows.some(b=>Date.parse(b.time)/1000===boundary-seconds))latest[tf]=boundary;
     }
     // Closed sessions may be silent; an open stale socket is reauthenticated.
     const now=new Date(),week=now.getUTCDay()*86400+now.getUTCHours()*3600+now.getUTCMinutes()*60+now.getUTCSeconds();
     const second=s=>{const [day,t]=s.split(', ');return ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].indexOf(day)*86400+t.split(':').reduce((a,n)=>a*60+Number(n),0);};
     const open=conditions.trade_sessions.some(s=>s.trade_mode==='enabled'&&second(s.week_start_time)<=week&&week<=second(s.week_end_time));
     if(open&&Date.now()-last>60000)finish(Error('STALE_SOCKET'));
    }catch(e){
     if(e.message==='HTTP_429')emit({type:'status',state:'history_rate_limited'});
     else finish(e);
    }finally{refreshing=false;}
   },2000);
   socket.on('open',()=>{socket.send(JSON.stringify({id:'xau-source',subscribe:{event:'ticks',instruments:['XAUUSD']}}));emit({type:'status',state:'subscribed'});});
   socket.on('message',bytes=>{
    try{
     const e=JSON.parse(bytes.toString());
     if(e.code!=null&&![0,200].includes(Number(e.code)))return finish(Error('SUBSCRIPTION_REJECTED'));
     if(e.instrument!=='XAUUSD')return;
     emit({type:'quote',bid:e.bid,ask:e.ask,timestamp:e.timestamp});last=Date.now();backoff=1000;
    }catch{finish(Error('INVALID_FRAME'));}
   });
   socket.on('error',()=>finish(Error('NETWORK')));
   socket.on('unexpected-response',(_q,r)=>{r.resume();finish(Error('HTTP_'+r.statusCode));});
   socket.on('close',()=>finish(stopped?null:Error('DISCONNECTED')));
  });
 }catch(e){
  const code=/^(HTTP_\d+|INSTRUMENT_MISMATCH|INVALID_LIMITS|EMPTY_HISTORY|PAGINATION_STALLED|STALE_SOCKET|SUBSCRIPTION_REJECTED|INVALID_FRAME|NETWORK|DISCONNECTED)$/.test(e.message)?e.message:'TRANSPORT_ERROR';
  const blocked=['HTTP_401','HTTP_403','INSTRUMENT_MISMATCH','SUBSCRIPTION_REJECTED'].includes(code);
  emit({type:'status',state:blocked?'blocked':'reconnecting',code});
  if(blocked){process.exitCode=78;break;}
  if(!stopped){await sleep(backoff);backoff=Math.min(60000,backoff*2);}
 }
}
