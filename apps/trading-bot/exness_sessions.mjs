// Validate broker UTC weekly boundaries before metadata or stale-socket checks.
export function sessionSecond(value) {
 const m=typeof value==='string' && /^(Sun|Mon|Tue|Wed|Thu|Fri|Sat)(?:\s*,\s*|\s+)(\d{2}):(\d{2}):(\d{2})$/.exec(value.trim());
 if(!m || +m[2]>23 || +m[3]>59 || +m[4]>59)throw Error('INVALID_SCHEDULE');
 return ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].indexOf(m[1])*86400+(+m[2])*3600+(+m[3])*60+(+m[4]);
}
export function enabledSessions(conditions) {
 if(!Array.isArray(conditions.trade_sessions))throw Error('INVALID_SCHEDULE');
 const intervals=conditions.trade_sessions.filter(s=>s.trade_mode==='enabled').map(s=>{
  const start=sessionSecond(s.week_start_time),end=sessionSecond(s.week_end_time)+1;
  if(start>=end)throw Error('INVALID_SCHEDULE');
  return [start,end];
 });
 if(!intervals.length)throw Error('INVALID_SCHEDULE');
 return intervals;
}
