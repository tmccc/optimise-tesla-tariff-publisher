"""Bounded private evidence store; no network or control operations."""
from datetime import datetime, timezone
import json
import math
import sqlite3

POWER_FIELDS = ('grid_import','grid_export','battery_charge','battery_discharge','solar','home')


def finite(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        return None
    return float(value)


def epoch(value):
    if isinstance(value,(int,float)) and not isinstance(value,bool):return float(value) if math.isfinite(value) else None
    if not isinstance(value,str):return None
    try:
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        return d.timestamp() if d.tzinfo else None
    except ValueError:return None


def iso(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')


def integrate_pair(first,last,record_from,quality='unverified'):
    """Trapezoidal directional W integration; split at UTC tariff boundaries."""
    a,b=finite(first.get('ts')),finite(last.get('ts'))
    if a is None or b is None or not 0<b-a<=30:return []
    if any(finite(x.get(k+'_w')) is None or x[k+'_w']<0 for x in (first,last) for k in POWER_FIELDS):return []
    begin=max(a,record_from)
    if begin>=b:return []
    cuts=[begin]
    boundary=(math.floor(begin/1800)+1)*1800
    while boundary<b:cuts.append(boundary);boundary+=1800
    cuts.append(b)
    rows=[]
    for start,end in zip(cuts,cuts[1:]):
        row={'start':start,'end':end,'quality':quality}
        for field in POWER_FIELDS:
            v0,v1=first[field+'_w'],last[field+'_w']
            p0=v0+(v1-v0)*(start-a)/(b-a);p1=v0+(v1-v0)*(end-a)/(b-a)
            row[field+'_kwh']=(p0+p1)/2*(end-start)/3_600_000
        rows.append(row)
    return rows


class Store:
    def __init__(self,path):
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS samples(ts REAL PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations(ts REAL PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS segments(start REAL PRIMARY KEY,end REAL NOT NULL,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quotes(kind TEXT,start REAL,end REAL,price REAL,first_seen REAL,revisions INTEGER DEFAULT 0,PRIMARY KEY(kind,start));
        CREATE TABLE IF NOT EXISTS price_events(captured_at REAL,kind TEXT,start REAL,price REAL);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);''')

    def metadata(self,key,default=None):
        row=self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_metadata(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',(key,json.dumps(value,allow_nan=False)))

    def last_sample(self):
        row=self.db.execute('SELECT MAX(ts) FROM samples').fetchone();return row[0]

    def previous_observation(self):
        row=self.db.execute('SELECT data FROM observations ORDER BY ts DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def add_observation(self,observation):
        self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?)',(observation['captured_at'],json.dumps(observation,allow_nan=False)))

    def add_samples(self,rows,record_from,current,previous,record_until=None):
        accepted=[]
        for raw in rows:
            ts=finite(raw.get('ts'))
            upper=min(current['captured_at']+2,record_until) if record_until is not None else current['captured_at']+2
            if ts is None or not record_from-30<=ts<=upper:continue
            if any(finite(raw.get(k+'_w')) is None or raw[k+'_w']<0 for k in POWER_FIELDS):continue
            row={'ts':ts,**{k+'_w':raw[k+'_w'] for k in POWER_FIELDS},'soe':finite(raw.get('soe'))}
            self.db.execute('INSERT OR IGNORE INTO samples VALUES(?,?)',(ts,json.dumps(row,allow_nan=False)))
            accepted.append(ts)
        if not accepted:return
        start=min(accepted)
        before=self.db.execute('SELECT data FROM samples WHERE ts<? ORDER BY ts DESC LIMIT 1',(start,)).fetchone()
        points=([json.loads(before[0])] if before else [])+[json.loads(r[0]) for r in self.db.execute('SELECT data FROM samples WHERE ts>=? ORDER BY ts',(start,))]
        for a,b in zip(points,points[1:]):
            monitored=bool(previous and previous.get('source_fresh') and current.get('source_fresh') and
                           0<current['captured_at']-previous['captured_at']<=90 and
                           a['ts']>=previous['captured_at']-15 and b['ts']<=current['captured_at']+2)
            for segment in integrate_pair(a,b,record_from,'monitored' if monitored else 'unverified'):
                self.db.execute('INSERT OR IGNORE INTO segments VALUES(?,?,?)',(segment['start'],segment['end'],json.dumps(segment,allow_nan=False)))

    def add_quote(self,kind,start,end,price,captured_at):
        price=finite(price);start,end=epoch(start),epoch(end)
        if price is None or start is None or end is None or end-start!=1800:return
        if start%1800!=0:return
        old=self.db.execute('SELECT end,price FROM quotes WHERE kind=? AND start=?',(kind,start)).fetchone()
        if old and old[0]==end and old[1]==price:return
        if old:self.db.execute('UPDATE quotes SET end=?,price=?,revisions=revisions+1 WHERE kind=? AND start=?',(end,price,kind,start))
        else:self.db.execute('INSERT INTO quotes(kind,start,end,price,first_seen) VALUES(?,?,?,?,?)',(kind,start,end,price,captured_at))
        self.db.execute('INSERT INTO price_events VALUES(?,?,?,?)',(captured_at,kind,start,price))

    def priced_segments(self,start,end):
        prices={(kind,begin):(finish,price) for kind,begin,finish,price in self.db.execute('SELECT kind,start,end,price FROM quotes WHERE end>? AND start<?',(start,end))}
        result=[]
        for (raw,) in self.db.execute('SELECT data FROM segments WHERE end>? AND start<? ORDER BY start',(start,end)):
            row=json.loads(raw)
            for key in ('optimise_import','optimise_export','agile_import','agile_export'):
                quote=prices.get((key,math.floor(row['start']/1800)*1800))
                row[key+'_p']=quote[1] if quote and quote[0]>=row['end'] else None
            result.append(row)
        return result

    def inventory_near(self,target,tolerance=120):
        row=self.db.execute('SELECT data FROM observations WHERE ts BETWEEN ? AND ? ORDER BY ABS(ts-?) LIMIT 1',(target-tolerance,target+tolerance,target)).fetchone()
        return json.loads(row[0]) if row else None

    def observations_between(self,start,end):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM observations WHERE ts>=? AND ts<=? ORDER BY ts',(start,end))]

    def sample_count(self):return self.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
    def quote_revisions(self):return self.db.execute('SELECT COALESCE(SUM(revisions),0) FROM quotes WHERE kind LIKE "optimise_%"').fetchone()[0]
    def commit(self):self.db.commit()
