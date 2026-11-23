"""Free, cache-first USD/XAUUSD calendar from official sources."""
import hashlib, html, json, os, re, sqlite3, time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

DB = os.path.abspath(os.environ.get("NEWS_DB", "news.db"))
FRED_KEY = os.environ.get("FRED_API_KEY", "")
HORIZON = 45
WORDS = ("consumer price", "cpi", "employment", "nonfarm", "jobless", "unemployment", "producer price", "ppi", "gross domestic", "gdp", "personal income", "personal consumption", "retail sales", "federal funds", "fomc", "speech", "testimony", "manufacturing pmi", "services pmi", "prices paid", "construction", "housing starts", "building permits")
SOURCE_URLS = {"FED":"https://www.federalreserve.gov/newsevents/calendar.htm", "BLS":"https://www.bls.gov/schedule/news_release/bls.ics", "ISM":"https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/", "CENSUS":"https://www.census.gov/economic-indicators/calendar-listview.html", "FRED":"https://fred.stlouisfed.org/"}
db = sqlite3.connect(DB, timeout=20)
db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE IF NOT EXISTS news(id TEXT PRIMARY KEY,event_time TEXT NOT NULL,title TEXT NOT NULL,source TEXT NOT NULL,impact TEXT NOT NULL DEFAULT 'HIGH',actual TEXT,forecast TEXT,previous TEXT,status TEXT NOT NULL DEFAULT 'SCHEDULED',updated_at TEXT NOT NULL)")
columns = {r[1] for r in db.execute("PRAGMA table_info(news)")}
for name, definition in {"event_id":"TEXT", "source_url":"TEXT", "currency":"TEXT NOT NULL DEFAULT 'USD'", "needs_verification":"INTEGER NOT NULL DEFAULT 0", "confidence":"TEXT NOT NULL DEFAULT 'Medium'"}.items():
    if name not in columns: db.execute(f"ALTER TABLE news ADD COLUMN {name} {definition}")
db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_news_event_id ON news(event_id)")
db.execute("CREATE TABLE IF NOT EXISTS news_source_status(source TEXT PRIMARY KEY,status TEXT NOT NULL,last_sync TEXT NOT NULL,last_error TEXT,event_count INTEGER NOT NULL DEFAULT 0)")
# Collapse legacy rows written by the first worker before event_id existed.
db.execute("DELETE FROM news WHERE rowid NOT IN (SELECT MAX(rowid) FROM news GROUP BY event_time,title,source)")
db.commit()

def release_at(day, hour, minute):
    """Convert US Eastern release time to UTC, including DST."""
    local = datetime.combine(day, datetime.min.time(), ZoneInfo("America/New_York")).replace(hour=hour, minute=minute)
    return local.astimezone(timezone.utc).isoformat()

def relevant(title): return any(word in title.lower() for word in WORDS)
def event_id(source, official, title, day):
    raw = f"{source}|{official}|{day}|{' '.join(title.lower().split())}"
    return f"{source.lower()}:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"
def put(source, official, event_time, title, impact="HIGH", actual=None, forecast=None, previous=None, status="SCHEDULED", source_url=None, confidence=None):
    title = html.unescape(" ".join(title.split())).strip()
    if not title or not relevant(title): return 0
    day = (event_time or "unknown")[:10]; eid = event_id(source, official, title, day); now = datetime.now(timezone.utc).isoformat()
    confidence = confidence or ("High" if source in {"FED","BLS","ISM","CENSUS"} else "Low")
    with db:
        db.execute("""INSERT INTO news(id,event_time,title,source,impact,actual,forecast,previous,status,updated_at,event_id,source_url,currency,needs_verification,confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET event_time=excluded.event_time,title=excluded.title,impact=excluded.impact,actual=COALESCE(excluded.actual,news.actual),forecast=COALESCE(excluded.forecast,news.forecast),previous=COALESCE(excluded.previous,news.previous),status=excluded.status,updated_at=excluded.updated_at,source_url=excluded.source_url,needs_verification=excluded.needs_verification,confidence=excluded.confidence""", (eid,event_time or f"{day}T00:00:00+00:00",title,source,impact,actual,forecast,previous,status,now,eid,source_url or SOURCE_URLS.get(source),"USD",int(status == "NEEDS_VERIFICATION"),confidence))
    return 1
def fetch(url):
    with urlopen(Request(url, headers={"User-Agent":"xauusd-news-bot/1.0", "Accept-Language":"en-US,en;q=0.8"}), timeout=20) as response: return response.read().decode("utf-8", "ignore")
def sync_bls():
    text, total, today = fetch(SOURCE_URLS["BLS"]), 0, datetime.now(timezone.utc).date()
    for block in text.split("BEGIN:VEVENT")[1:]:
        date, title = re.search(r"DTSTART[^:]*:(\d{8})", block), re.search(r"SUMMARY:(.+)", block)
        if not date or not title: continue
        day = datetime.strptime(date.group(1), "%Y%m%d").date()
        if today <= day <= today + timedelta(days=HORIZON): total += put("BLS",date.group(1),release_at(day,8,30),title.group(1),source_url=SOURCE_URLS["BLS"])
    return total
def sync_fred():
    if not FRED_KEY: return 0
    today = datetime.now(timezone.utc).date(); q = urlencode({"api_key":FRED_KEY,"file_type":"json","realtime_start":today.isoformat(),"realtime_end":(today+timedelta(days=HORIZON)).isoformat(),"limit":1000})
    data = json.loads(fetch("https://api.stlouisfed.org/fred/releases/dates?" + q))
    return sum(put("FRED",i["release_id"],i["date"]+"T00:00:00+00:00",i["release_name"],source_url=SOURCE_URLS["FRED"],confidence="Low") for i in data.get("release_dates",[]))
def sync_fed():
    text, total = fetch(SOURCE_URLS["FED"]), 0
    for m in re.finditer(r"(?P<title>(?:Speech|Testimony|FOMC|Federal Funds|Press release)[^<]{3,160}).{0,500}?(?P<date>\d{1,2}/\d{1,2}/\d{4})",text,re.I|re.S):
        title = re.sub(r"\s+"," ",html.unescape(m.group("title"))).strip(); month,day,year=map(int,m.group("date").split("/")); at=datetime(year,month,day,tzinfo=timezone.utc); now=datetime.now(timezone.utc)
        if now-timedelta(days=1)<=at<=now+timedelta(days=HORIZON): total += put("FED",m.group("date"),at.isoformat(),title,source_url=SOURCE_URLS["FED"])
    return total

def sync_fed_speeches():
    """Official speech archive; dates are reliable, times are intentionally unknown."""
    total = 0
    archive = "https://www.federalreserve.gov/newsevents/speeches.htm"
    text = fetch(archive)
    for m in re.finditer(r"(\d{1,2}/\d{1,2}/\d{4}).{0,500}?<a[^>]*>([^<]{5,180})</a>", text, re.I | re.S):
        try: day = datetime.strptime(m.group(1), "%m/%d/%Y").date()
        except ValueError: continue
        today = datetime.now(timezone.utc).date()
        if today - timedelta(days=1) <= day <= today + timedelta(days=HORIZON):
            total += put("FED_SPEECHES", m.group(1), f"{day}T00:00:00+00:00", "Fed Speech: " + m.group(2), status="TIME_UNCONFIRMED", source_url=archive, confidence="Medium")
    return total

def sync_claims_and_spmi():
    """Official recurring schedules; no actual/forecast is invented."""
    today = datetime.now(timezone.utc).date(); end = today + timedelta(days=HORIZON); total = 0
    # Jobless claims are released Thursdays at 08:30 ET (12:30/13:30 UTC seasonally).
    day = today
    while day <= end:
        if day.weekday() == 3:
            total += put("BLS", f"claims:{day}", release_at(day,8,30), "Initial Jobless Claims", source_url="https://www.dol.gov/ui/data.pdf", confidence="Medium")
        day += timedelta(days=1)
    # S&P Global PMI is released on the first business day at 09:45 ET.
    month = today.replace(day=1)
    while month <= end:
        d = month
        while d.weekday() >= 5: d += timedelta(days=1)
        if today <= d <= end:
            total += put("S&P_GLOBAL", f"spmi:{d}", release_at(d,9,45), "S&P Global US Manufacturing PMI", source_url="https://www.pmi.spglobal.com/Public/Home/PressRelease", confidence="Medium")
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return total
def sync_ism():
    text,total=fetch(SOURCE_URLS["ISM"]),0
    clean = re.sub(r"<[^>]+>", " ", html.unescape(text)); clean = re.sub(r"\s+", " ", clean)
    for m in re.finditer(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\s+(\d{1,2})\s+(\d{1,2})",clean,re.I):
        for title,index in (("ISM Manufacturing PMI",3),("ISM Services PMI",4)):
            day=datetime.strptime(f"{m.group(1)} {m.group(index)} {m.group(2)}","%B %d %Y").date()
            if datetime.now(timezone.utc).date()<=day<=datetime.now(timezone.utc).date()+timedelta(days=HORIZON): total += put("ISM",f"{day}:{title}",f"{day}T00:00:00+00:00",title,status="TIME_UNCONFIRMED",source_url=SOURCE_URLS["ISM"],confidence="Medium")
    return total
def sync_census():
    text,total=fetch(SOURCE_URLS["CENSUS"]),0
    for m in re.finditer(r"(Construction Spending|New Residential Construction|Retail Sales|Building Permits).{0,300}?(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})",text,re.I|re.S):
        try: day=datetime.strptime(f"{m.group(2)} {m.group(3)} {m.group(4)}","%B %d %Y").date()
        except ValueError: continue
        if datetime.now(timezone.utc).date()<=day<=datetime.now(timezone.utc).date()+timedelta(days=HORIZON): total += put("CENSUS",m.group(0)[:80],f"{day}T00:00:00+00:00",m.group(1),status="TIME_UNCONFIRMED",source_url=SOURCE_URLS["CENSUS"],confidence="Medium")
    return total
def sync_all():
    results={}
    for name,action in (("FED",sync_fed),("FED_SPEECHES",sync_fed_speeches),("BLS",sync_bls),("CLAIMS_PMI",sync_claims_and_spmi),("ISM",sync_ism),("CENSUS",sync_census),("FRED",sync_fred)):
        try: results[name]={"status":"OK","count":action(),"updated_at":datetime.now(timezone.utc).isoformat()}
        except HTTPError as exc: results[name]={"status":"UNAVAILABLE","error":f"HTTP {exc.code}","updated_at":datetime.now(timezone.utc).isoformat()}
        except Exception as exc: results[name]={"status":"ERROR","error":str(exc),"updated_at":datetime.now(timezone.utc).isoformat()}; print(f"news {name} error: {exc}",flush=True)
        item=results[name]
        with db: db.execute("INSERT OR REPLACE INTO news_source_status(source,status,last_sync,last_error,event_count) VALUES(?,?,?,?,?)",(name,item["status"],item["updated_at"],item.get("error"),item.get("count",0)))
    print("news synced: " + " ".join(f"{k}={v.get('count', v.get('status', 'ERROR'))}" for k,v in results.items()),flush=True); return results
if __name__ == "__main__":
    while True: sync_all(); time.sleep(int(os.environ.get("NEWS_POLL_SECONDS","300")))
