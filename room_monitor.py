"""Per-stay ITAKA room-count monitoring and email notifications."""
import html
import os
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone

def ensure_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS room_monitors(
        hotel_id INTEGER NOT NULL, airport_code TEXT NOT NULL, departure_date TEXT NOT NULL,
        nights INTEGER NOT NULL, monitor_enabled INTEGER NOT NULL DEFAULT 0,
        email_enabled INTEGER NOT NULL DEFAULT 0, last_count INTEGER, last_checked_at TEXT,
        PRIMARY KEY(hotel_id,airport_code,departure_date,nights),
        FOREIGN KEY(hotel_id) REFERENCES hotels(id) ON DELETE CASCADE)""")
    c.execute("""CREATE TABLE IF NOT EXISTS room_count_history(
        id INTEGER PRIMARY KEY, hotel_id INTEGER NOT NULL, airport_code TEXT NOT NULL,
        departure_date TEXT NOT NULL, nights INTEGER NOT NULL, checked_at TEXT NOT NULL,
        room_count INTEGER NOT NULL, source_url TEXT, email_required INTEGER NOT NULL DEFAULT 0,
        email_sent_at TEXT, FOREIGN KEY(hotel_id) REFERENCES hotels(id) ON DELETE CASCADE)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_room_history_stay ON room_count_history(hotel_id,airport_code,departure_date,nights,id)")

def export_state(c):
    return {
        "room_monitors":[dict(r) for r in c.execute("SELECT hotel_id,airport_code,departure_date,nights,monitor_enabled,email_enabled,last_count,last_checked_at FROM room_monitors ORDER BY hotel_id,airport_code,departure_date,nights")],
        "room_count_history":[dict(r) for r in c.execute("SELECT id,hotel_id,airport_code,departure_date,nights,checked_at,room_count,source_url,email_required,email_sent_at FROM room_count_history ORDER BY id")]
    }

def clear(c):
    c.execute("DELETE FROM room_count_history")
    c.execute("DELETE FROM room_monitors")

def restore(c,state):
    c.executemany("INSERT INTO room_monitors(hotel_id,airport_code,departure_date,nights,monitor_enabled,email_enabled,last_count,last_checked_at) VALUES(?,?,?,?,?,?,?,?)",
        [(r["hotel_id"],r["airport_code"],r["departure_date"],r["nights"],r.get("monitor_enabled",0),r.get("email_enabled",0),r.get("last_count"),r.get("last_checked_at")) for r in state.get("room_monitors",[])])
    c.executemany("INSERT INTO room_count_history(id,hotel_id,airport_code,departure_date,nights,checked_at,room_count,source_url,email_required,email_sent_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(r["id"],r["hotel_id"],r["airport_code"],r["departure_date"],r["nights"],r["checked_at"],r["room_count"],r.get("source_url"),r.get("email_required",0),r.get("email_sent_at")) for r in state.get("room_count_history",[])])

def set_settings(c,hotel_id,airport_code,departure_date,nights,monitor_enabled,email_enabled):
    c.execute("""INSERT INTO room_monitors(hotel_id,airport_code,departure_date,nights,monitor_enabled,email_enabled)
        VALUES(?,?,?,?,?,?) ON CONFLICT(hotel_id,airport_code,departure_date,nights) DO UPDATE SET
        monitor_enabled=excluded.monitor_enabled,email_enabled=excluded.email_enabled""",
        (hotel_id,airport_code,departure_date,nights,int(bool(monitor_enabled)),int(bool(email_enabled))))
    if not email_enabled:
        c.execute("UPDATE room_count_history SET email_required=0 WHERE hotel_id=? AND airport_code=? AND departure_date=? AND nights=? AND email_sent_at IS NULL",(hotel_id,airport_code,departure_date,nights))

def get_stays(c,hotel_id,airport_code):
    rows=c.execute("""SELECT s.airport_code,s.departure_date,s.nights,s.days,s.url,s.status,
        m.monitor_enabled,m.email_enabled,m.last_count,m.last_checked_at
        FROM stay_offers s LEFT JOIN room_monitors m ON m.hotel_id=s.hotel_id AND m.airport_code=s.airport_code
        AND m.departure_date=s.departure_date AND m.nights=s.nights
        WHERE s.hotel_id=? AND s.airport_code=? AND s.departure_date>=date('now')
        ORDER BY s.departure_date,s.nights""",(hotel_id,airport_code)).fetchall()
    return [dict(r) | {"monitor_enabled":bool(r["monitor_enabled"]),"email_enabled":bool(r["email_enabled"])} for r in rows]

def get_history(c,hotel_id,airport_code,departure_date,nights,limit=100):
    rows=c.execute("SELECT checked_at,room_count,email_required,email_sent_at FROM room_count_history WHERE hotel_id=? AND airport_code=? AND departure_date=? AND nights=? ORDER BY id DESC LIMIT ?",
        (hotel_id,airport_code,departure_date,nights,limit)).fetchall()
    return [dict(r) for r in rows]

_ROOM_PATTERNS=(
    re.compile(r"\b(?:został(?:y|o)?|pozostał(?:y|o)?|dostępne\s+są|ostatnie|tylko)\s+(?:jeszcze\s+)?(?:tylko\s+)?(\d{1,3})\s+(?:pokój|pokoje|pokoi)\b",re.I),
    re.compile(r"\b(?:only|just)\s+(\d{1,3})\s+rooms?\s+(?:left|available)\b",re.I),
    re.compile(r"\b(\d{1,3})\s+ostatnie\s+(?:pokój|pokoje|pokoi)\b",re.I),
)

def parse_room_count(source):
    """Return an explicit, unambiguous count; missing text is never interpreted as zero."""
    if not source:return None
    text=html.unescape(str(source))
    text=re.sub(r"\\u([0-9a-fA-F]{4})",lambda m:chr(int(m.group(1),16)),text)
    text=re.sub(r"<[^>]*>"," ",text)
    text=re.sub(r"\s+"," ",text)
    # If the page mentions more than one numeric room quantity, the result is ambiguous.
    # Do not accept a matching "left" phrase while ignoring another room count elsewhere.
    mentions=re.findall(r"\\b(\\d{1,3})\\s+(?:pokój|pokoje|pokoi|rooms?)\\b",text,re.I)
    if len(mentions)>1:
        return None
    counts=set()
    for pattern in _ROOM_PATTERNS:
        counts.update(int(m.group(1)) for m in pattern.finditer(text))
    return next(iter(counts)) if len(counts)==1 else None

def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def _send_email(subject,body):
    username=os.environ.get("SMTP_USER","").strip()
    password=os.environ.get("SMTP_PASSWORD","").strip()
    recipient=os.environ.get("ROOM_ALERT_EMAIL","").strip()
    if not username or not password or not recipient:
        return False,"SMTP_USER, SMTP_PASSWORD lub ROOM_ALERT_EMAIL nie są skonfigurowane"
    message=EmailMessage()
    message["From"]=username
    message["To"]=recipient
    message["Subject"]=subject
    message.set_content(body)
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST","smtp.gmail.com"),int(os.environ.get("SMTP_PORT","465")),timeout=25) as smtp:
        smtp.login(username,password)
        smtp.send_message(message)
    return True,None

def _send_pending(c):
    pending=c.execute("""SELECT h.id,h.hotel_id,h.airport_code,h.departure_date,h.nights,h.room_count,h.checked_at,h.source_url,
        t.name AS hotel_name FROM room_count_history h JOIN hotels t ON t.id=h.hotel_id
        WHERE h.email_required=1 AND h.email_sent_at IS NULL ORDER BY h.id""").fetchall()
    for row in pending:
        subject=f"ITAKA: {row['hotel_name']} — {row['room_count']} pokoi"
        body=(f"Hotel: {row['hotel_name']}\nTermin wyjazdu: {row['departure_date']}\nDługość pobytu: {row['nights']} nocy\n"
              f"Liczba dostępnych pokoi: {row['room_count']}\nOdczyt: {row['checked_at']}\nOferta: {row['source_url'] or '(brak linku)'}\n")
        try:sent,error=_send_email(subject,body)
        except Exception as exc:sent,error=False,str(exc)
        if sent:
            c.execute("UPDATE room_count_history SET email_sent_at=? WHERE id=?",(_utc_now(),row["id"]))
            c.commit()
        else:
            print(f"Room-count email pending for history row {row['id']}: {error}")

def run_checks(connect,fetch_html):
    """Check only enabled saved ITAKA stays; preserve last value on missing/ambiguous text."""
    c=connect()
    try:
        monitors=c.execute("""SELECT m.hotel_id,m.airport_code,m.departure_date,m.nights,m.email_enabled,s.url,t.name AS hotel_name
            FROM room_monitors m JOIN stay_offers s ON s.hotel_id=m.hotel_id AND s.airport_code=m.airport_code
              AND s.departure_date=m.departure_date AND s.nights=m.nights
            JOIN hotels t ON t.id=m.hotel_id
            WHERE m.monitor_enabled=1 AND s.url IS NOT NULL AND s.url!='' AND lower(t.url) LIKE '%itaka.pl%' AND s.departure_date>=date('now')
            ORDER BY m.hotel_id,m.departure_date,m.nights""").fetchall()
    finally:c.close()
    checked=errors=0
    for monitor in monitors:
        try:
            count=parse_room_count(fetch_html(monitor["url"]))
            if count is None:
                print(f"Room count not found; keeping last value: hotel {monitor['hotel_id']} {monitor['departure_date']} {monitor['nights']} nights")
                continue
            c=connect()
            try:
                previous=c.execute("SELECT last_count FROM room_monitors WHERE hotel_id=? AND airport_code=? AND departure_date=? AND nights=?",
                    (monitor["hotel_id"],monitor["airport_code"],monitor["departure_date"],monitor["nights"])).fetchone()
                old=previous["last_count"] if previous else None
                changed=old is None or old!=count
                stamp=_utc_now()
                c.execute("INSERT INTO room_count_history(hotel_id,airport_code,departure_date,nights,checked_at,room_count,source_url,email_required) VALUES(?,?,?,?,?,?,?,?)",
                    (monitor["hotel_id"],monitor["airport_code"],monitor["departure_date"],monitor["nights"],stamp,count,monitor["url"],int(changed and bool(monitor["email_enabled"]))))
                c.execute("UPDATE room_monitors SET last_count=?,last_checked_at=? WHERE hotel_id=? AND airport_code=? AND departure_date=? AND nights=?",
                    (count,stamp,monitor["hotel_id"],monitor["airport_code"],monitor["departure_date"],monitor["nights"]))
                c.commit()
            finally:c.close()
            checked+=1
            if changed:
                direction="pierwszy odczyt" if old is None else ("wzrost" if count>old else "spadek")
                print(f"Room count {direction}: hotel {monitor['hotel_id']} {monitor['departure_date']} {monitor['nights']} nights: {old} -> {count}")
        except Exception as exc:
            errors+=1
            print(f"Room-count check failed for hotel {monitor['hotel_id']} {monitor['departure_date']}: {exc}")
    c=connect()
    try:_send_pending(c)
    finally:c.close()
    return checked,errors
