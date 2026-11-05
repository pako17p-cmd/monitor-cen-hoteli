"""Durable price and availability series keyed by departure, airport and nights.
Legacy observations have no duration and are deliberately not relabelled.
"""
TABLES={
 'stay_offers':('hotel_id','airport_code','departure_date','nights','days','url','return_airport','return_name','last_seen_at','status','price'),
 'stay_prices':('id','hotel_id','airport_code','departure_date','nights','checked_at','price'),
 'stay_events':('id','hotel_id','airport_code','departure_date','nights','checked_at','from_status','to_status','price','previous_price'),
}

def ensure_schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS stay_offers(hotel_id INTEGER NOT NULL,airport_code TEXT NOT NULL,departure_date TEXT NOT NULL,nights INTEGER NOT NULL,days INTEGER,url TEXT,return_airport TEXT,return_name TEXT,last_seen_at TEXT,status TEXT NOT NULL,price REAL,PRIMARY KEY(hotel_id,airport_code,departure_date,nights),FOREIGN KEY(hotel_id) REFERENCES hotels(id) ON DELETE CASCADE)')
    c.execute('CREATE TABLE IF NOT EXISTS stay_prices(id INTEGER PRIMARY KEY,hotel_id INTEGER NOT NULL,airport_code TEXT NOT NULL,departure_date TEXT NOT NULL,nights INTEGER NOT NULL,checked_at TEXT NOT NULL,price REAL NOT NULL,FOREIGN KEY(hotel_id) REFERENCES hotels(id) ON DELETE CASCADE)')
    c.execute('CREATE TABLE IF NOT EXISTS stay_events(id INTEGER PRIMARY KEY,hotel_id INTEGER NOT NULL,airport_code TEXT NOT NULL,departure_date TEXT NOT NULL,nights INTEGER NOT NULL,checked_at TEXT NOT NULL,from_status TEXT NOT NULL,to_status TEXT NOT NULL,price REAL,previous_price REAL,UNIQUE(hotel_id,airport_code,departure_date,nights,checked_at,to_status),FOREIGN KEY(hotel_id) REFERENCES hotels(id) ON DELETE CASCADE)')

def export_state(c):
    return {table:[dict(row) for row in c.execute('SELECT '+','.join(columns)+' FROM '+table+' ORDER BY '+('id' if 'id' in columns else 'hotel_id,departure_date,airport_code,nights'))] for table,columns in TABLES.items()}

def clear(c):
    for table in reversed(TABLES):c.execute('DELETE FROM '+table)

def restore(c,state):
    for table,columns in TABLES.items():
        c.executemany('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[tuple(row.get(column) for column in columns) for row in state.get(table,[])])

def record(c,hotel_id,variants,stamp,max_departure,unconfirmed_lengths=frozenset()):
    current={(v['code'],v['date'],v['nights']):v for v in variants if isinstance(v.get('nights'),int) and v['nights']>=6}
    def event(old,status,key,price=None):
        if old and old['status']!=status:
            c.execute('INSERT OR IGNORE INTO stay_events(hotel_id,airport_code,departure_date,nights,checked_at,from_status,to_status,price,previous_price) VALUES(?,?,?,?,?,?,?,?,?)',(hotel_id,*key,stamp,old['status'],status,price,old['price']))
    for key,v in current.items():
        old=c.execute('SELECT * FROM stay_offers WHERE hotel_id=? AND airport_code=? AND departure_date=? AND nights=?',(hotel_id,*key)).fetchone()
        last=c.execute('SELECT price FROM stay_prices WHERE hotel_id=? AND airport_code=? AND departure_date=? AND nights=? ORDER BY id DESC LIMIT 1',(hotel_id,*key)).fetchone()
        if last is None or abs(last['price']-v['price'])>=0.01:
            c.execute('INSERT INTO stay_prices(hotel_id,airport_code,departure_date,nights,checked_at,price) VALUES(?,?,?,?,?,?)',(hotel_id,*key,stamp,v['price']))
        event(old,'available',key,v['price'])
        c.execute("INSERT INTO stay_offers(hotel_id,airport_code,departure_date,nights,days,url,return_airport,return_name,last_seen_at,status,price) VALUES(?,?,?,?,?,?,?,?,?,'available',?) ON CONFLICT(hotel_id,airport_code,departure_date,nights) DO UPDATE SET days=excluded.days,url=excluded.url,return_airport=excluded.return_airport,return_name=excluded.return_name,last_seen_at=excluded.last_seen_at,status=excluded.status,price=excluded.price",(hotel_id,*key,v.get('days'),v.get('url'),v.get('return_code'),v.get('return_name'),stamp,v['price']))
    for old in c.execute("SELECT * FROM stay_offers WHERE hotel_id=? AND departure_date>=date('now') AND departure_date<=?",(hotel_id,max_departure)).fetchall():
        key=old['airport_code'],old['departure_date'],old['nights']
        if key not in current and (old['airport_code'],old['days']) not in unconfirmed_lengths:
            event(old,'unavailable',key)
            c.execute("UPDATE stay_offers SET status='unavailable' WHERE hotel_id=? AND airport_code=? AND departure_date=? AND nights=?",(hotel_id,*key))
