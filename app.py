import os, re, sqlite3, threading, time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from functools import wraps
from flask import Flask, request, jsonify, session, redirect, render_template_string, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
import urllib.request
from urllib.parse import urljoin, urlparse

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "please-change-this-secret-key")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
APP_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DB_PATH", str(APP_DIR / "history.db"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
INTERVAL_SECONDS = 2 * 60 * 60
AUTO_SCHEDULER = os.environ.get("DISABLE_SCHEDULER") != "1"

SEEDS = [
    ("Hotel Royal Andilana Resort & Spa", "https://www.itaka.pl/wczasy/madagaskar/hotel-royal-andilana-resort-and-spa,NOSROAN/?id%5B0%5D=CgVJdGFrYRIEVklUWBoDUExOIgdOT1NST0FOKAQ6BEwyODdCBgiA6u%2FWBkoGCIDflNcGUAJiBQoDV0FXagUKA05PU3IDCgExegUKA05PU4IBBQoDV0FXigEDCgExkgEGCICN9dYGmgEGCICCmtcGogEFCgNETFiqAQMKAUHiAQkKB1Jlc2FiZWXqAQkKB1Jlc2FiZWXyAQkKB1Jlc2FiZWWCAgMKAUGKAgMKAUE%3D&adults%5B0%5D=2"),
    ("Hotel Vanila & Spa", "https://www.itaka.pl/wczasy/madagaskar/hotel-vanila-and-spa,NOSVANI/?id%5B0%5D=CgVJdGFrYRIEVklUWBoDUExOIgdOT1NWQU5JKAQ6BEwyODdCBgiA9crWBkoGCIDq79YGUAJiBQoDV0FXagUKA05PU3IDCgExegUKA05PU4IBBQoDV0FXigEDCgExkgEGCICY0NYGmgEGCICN9dYGogEFCgNESFOqAQMKAUHiAQkKB1Jlc2FiZWXqAQkKB1Jlc2FiZWXyAQkKB1Jlc2FiZWWCAgMKAUGKAgMKAUE%3D&adults%5B0%5D=2")
]

class Extractor(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.hidden=0; self.image_url=None; self.calendar_prices=[]; self.button=None; self.button_depth=0; self.price_depth=0; self.price_text=[]
    def handle_starttag(self,t,a):
        attrs=dict(a)
        if t=="meta":
            if attrs.get("property","").lower()=="og:image" or attrs.get("name","").lower()=="twitter:image":
                self.image_url=self.image_url or attrs.get("content")
        if t=="button" and attrs.get("aria-label"):
            self.button={"label":attrs["aria-label"],"price":[]}; self.button_depth=1
        elif self.button: self.button_depth+=1
        if self.button and t=="div" and "__price" in attrs.get("class",""): self.price_depth+=1
        if t in ("script","style","noscript"): self.hidden+=1
        if t in ("br","p","div","li"): self.parts.append(" ")
    def handle_endtag(self,t):
        if t in ("script","style","noscript") and self.hidden: self.hidden-=1
        if self.button:
            if t=="div" and self.price_depth: self.price_depth-=1
            self.button_depth-=1
            if self.button_depth<=0:
                found=parse_calendar_price(self.button["label"],"".join(self.button["price"]))
                if found: self.calendar_prices.append(found)
                self.button=None; self.button_depth=0; self.price_depth=0
    def handle_data(self,d):
        if not self.hidden:
            self.parts.append(d)
            if self.button and self.price_depth: self.button["price"].append(d)

PL_MONTHS={"stycznia":"01","styczeń":"01","sty":"01","lutego":"02","luty":"02","lut":"02","marca":"03","marzec":"03","mar":"03","kwietnia":"04","kwiecień":"04","kwi":"04","maja":"05","maj":"05","czerwca":"06","czerwiec":"06","cze":"06","lipca":"07","lipiec":"07","lip":"07","sierpnia":"08","sierpień":"08","sie":"08","września":"09","wrzesień":"09","wrz":"09","października":"10","październik":"10","paź":"10","paz":"10","listopada":"11","listopad":"11","lis":"11","grudnia":"12","grudzień":"12","gru":"12"}
def parse_calendar_price(label, price_text):
    m=re.search(r"(?:poniedziałek|wtorek|środa|czwartek|piątek|sobota|niedziela),?\s*(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})",label,re.I)
    if not m: return None
    month=PL_MONTHS.get(m.group(2).lower())
    digits=re.sub(r"\D","",price_text)
    if not month or not digits: return None
    try: price=float(digits)
    except ValueError: return None
    if price<=0: return None
    return (f"{m.group(3)}-{month}-{int(m.group(1)):02d}",price)

def extract_departure_date(text):
    m=re.search(r"Najlepsza cena w tym wariancie:\s*(\d{1,2})\s+([a-ząćęłńóśźż]+)\s*[-–]\s*\d{1,2}\s+[a-ząćęłńóśźż]+\s+(20\d{2})",text,re.I)
    if not m: return None
    month=PL_MONTHS.get(m.group(2).lower())
    return f"{m.group(3)}-{month}-{int(m.group(1)):02d}" if month else None

def fetch_offer(url):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36","Accept-Language":"pl-PL,pl;q=0.9"})
    with urllib.request.urlopen(req,timeout=35) as res: html=res.read().decode(res.headers.get_content_charset() or "utf-8",errors="replace")
    p=Extractor(); p.feed(html); text=re.sub(r"\s+"," "," ".join(p.parts))
    image_url=urljoin(url,p.image_url) if p.image_url else None
    if image_url and urlparse(image_url).scheme!="https": image_url=None
    departure_date=extract_departure_date(text)
    for pattern in (r"Łącznie\s*:?\s*((?:\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)(?:[,.]\d{2})?)\s*zł", r"Razem\s*:?\s*((?:\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)(?:[,.]\d{2})?)\s*zł"):
        m=re.search(pattern,text,re.I)
        if m:
            val=float(m.group(1).replace(" ","").replace("\u00a0","").replace(",","."))
            if val>0:
                fee_match=re.search(r"\+\s*(\d+)\s*zł\s*\(TFG",text,re.I)
                fee=int(fee_match.group(1)) if fee_match else 0
                calendar_prices=[(d,price+fee) for d,price in p.calendar_prices]
                return val,image_url,departure_date,calendar_prices
    raise ValueError("ITAKA nie udostępniła ceny wybranej oferty. Nie zapisano odczytu.")

def connect():
    con=sqlite3.connect(DB_PATH,timeout=30); con.row_factory=sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("CREATE TABLE IF NOT EXISTS hotels(id INTEGER PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL,people_count INTEGER NOT NULL DEFAULT 2,image_url TEXT)")
    columns={row[1] for row in con.execute("PRAGMA table_info(hotels)").fetchall()}
    if "people_count" not in columns: con.execute("ALTER TABLE hotels ADD COLUMN people_count INTEGER NOT NULL DEFAULT 2")
    if "image_url" not in columns: con.execute("ALTER TABLE hotels ADD COLUMN image_url TEXT")
    con.execute("CREATE TABLE IF NOT EXISTS prices(id INTEGER PRIMARY KEY,hotel_id INTEGER NOT NULL,checked_at TEXT NOT NULL,price REAL NOT NULL,FOREIGN KEY(hotel_id) REFERENCES hotels(id) ON DELETE CASCADE)")
    price_columns={row[1] for row in con.execute("PRAGMA table_info(prices)").fetchall()}
    if "departure_date" not in price_columns: con.execute("ALTER TABLE prices ADD COLUMN departure_date TEXT")
    if "per_person" not in price_columns: con.execute("ALTER TABLE prices ADD COLUMN per_person INTEGER NOT NULL DEFAULT 0")
    if con.execute("SELECT COUNT(*) FROM hotels").fetchone()[0]==0: con.executemany("INSERT INTO hotels(name,url) VALUES(?,?)",SEEDS)
    con.commit(); return con

initdb=connect; c=initdb(); c.close()
def auth_required(fn):
    @wraps(fn)
    def inner(*args,**kwargs):
        if APP_PASSWORD and not session.get("ok"):
            if request.path.startswith("/api/"): return jsonify(error="Zaloguj się ponownie."),401
            return redirect("/login")
        return fn(*args,**kwargs)
    return inner

LOGIN='''<!doctype html><html lang="pl"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Logowanie · Ceny hoteli</title><style>*{box-sizing:border-box}body{margin:0;background:#eff4f3;font:16px system-ui;color:#173d3c;display:grid;min-height:100vh;place-items:center}.box{background:#fff;padding:32px;border-radius:20px;width:min(90%,390px);box-shadow:0 14px 45px #173d3c16}h1{font-size:23px}input,button{width:100%;padding:13px;border-radius:10px;border:1px solid #ccd9d6;font:inherit;margin-top:12px}button{background:#137b70;color:white;border:0;font-weight:700}</style><form class="box" method="post"><h1>Monitor cen hoteli</h1><p>Wpisz hasło aplikacji.</p><input name="password" type="password" placeholder="Hasło" required autofocus><button>Zaloguj</button>{% if error %}<p style="color:#b42318">{{error}}</p>{% endif %}</form></html>'''
@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        if request.form.get("password")==APP_PASSWORD: session["ok"]=True; return redirect("/")
        return render_template_string(LOGIN,error="Nieprawidłowe hasło.")
    return render_template_string(LOGIN,error="")
@app.get("/logout")
def logout(): session.clear(); return redirect("/login")

PAGE='''<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#123f50"><title>Ceny hoteli</title><style>
:root{--ink:#153a43;--muted:#75858c;--teal:#138274;--bg:#f2f6f5;--line:#e6eceb}*{box-sizing:border-box}body{margin:0;background:#f7f5f0;color:var(--ink);font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{position:relative;isolation:isolate;overflow:hidden;text-align:center;color:#fff;min-height:190px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:calc(26px + env(safe-area-inset-top)) 18px 34px;background:#087b91 url("/assets/background.webp") center 52%/cover no-repeat}header:after{content:"";position:absolute;z-index:-1;left:-5%;right:-5%;height:44px;bottom:-22px;border-radius:50% 50% 0 0/100% 100% 0 0;background:#f7f5f0;opacity:.96}header h1{margin:0 0 4px;line-height:0}header h1 img{display:block;width:min(360px,78vw);height:auto;max-height:96px;object-fit:contain}header p{margin:5px 0 0;color:#fff;font-size:14px;text-shadow:0 1px 5px #073746aa}.wrap{max-width:1300px;padding:18px 22px;margin:-20px auto 0;position:relative}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:14px}button{border:0;border-radius:11px;padding:12px 15px;font:600 14px inherit;cursor:pointer}.primary{background:var(--teal);color:white}.secondary{background:#fff;color:var(--ink);box-shadow:0 1px 3px #102f3612}.danger{color:#a53943;background:transparent}.history-btn{background:#eef7fc;color:#0878a6}.offer-btn{background:#148879;color:#fff;box-shadow:0 2px 6px #12827426}.delete-btn{background:#fff0ef;color:#c83232}.terms-btn{background:#f1f7f7;color:#226b72}.departure-choice{background:#fff;border:1px solid var(--line);color:var(--ink);padding:9px 12px;border-radius:10px}.departures{display:none;grid-column:1/-1;padding:12px 0 2px;gap:8px;flex-wrap:wrap}.departures.show{display:flex}.departure-choice.active{border-color:#148879;background:#eef8f5;color:#08786e;font-weight:700}.card{background:white;border:1px solid var(--line);border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 4px 14px #173d3c08}.card h2{font-size:17px;margin:0 0 12px}.hotel{display:grid;grid-template-columns:82px minmax(0,1fr);column-gap:14px;align-items:center;padding:14px 0;border-top:1px solid var(--line);cursor:pointer}.hotel:first-of-type{border:0}.thumb{width:82px;height:82px;object-fit:cover;border-radius:12px;background:#dff0ed;grid-row:1 / span 2}.thumb.missing{display:grid;place-items:center;color:#fff;font-size:25px;background:linear-gradient(140deg,#f3cf8c,#78c4bd 55%,#347f96)}.hotelname{font-weight:700}.price{font-size:21px;font-weight:750;color:#08786e;white-space:nowrap}.hotel>div:nth-of-type(3){background:#eef8f5;border-radius:12px;padding:12px 15px;text-align:center}.meta{color:var(--muted);font-size:12px;margin-top:5px}.rowbuttons{display:flex;gap:8px;align-items:center;margin-top:8px;grid-column:2/-1}.small{padding:8px 10px;font-size:12px}.chartwrap{height:230px}canvas{width:100%;height:100%}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:11px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:600}.note{font-size:12px;color:var(--muted);line-height:1.5}.modal{position:fixed;inset:0;background:#0e293b78;display:none;place-items:center;padding:18px}.modal.show{display:grid}.dialog{background:white;border-radius:18px;padding:22px;width:min(100%,430px)}.dialog h2{margin-top:0}.dialog input{width:100%;padding:12px;border:1px solid #cbd7d5;border-radius:10px;margin:6px 0 12px;font:inherit}.dialog label{font-size:13px;font-weight:650}.dialog .actions{margin:8px 0 0}.empty{color:var(--muted);padding:12px 0}.status{font-size:12px;color:var(--muted);margin-left:auto;align-self:center}@media(min-width:780px){.columns{display:grid;grid-template-columns:1fr 1fr;gap:14px}.columns .card{margin:0}.hotel{grid-template-columns:200px minmax(0,1fr) 270px;column-gap:22px;min-height:130px;padding:15px 0}.thumb{width:200px;height:112px;grid-row:1 / span 2}.hotel .rowbuttons{grid-column:2/-1;margin-top:12px}.chartwrap{height:260px}}
</style></head><body><header><h1><img src="/assets/napis.png" alt="Ceny hoteli"></h1><p>Historia ofert ITAKA · {{ "automatycznie co 2 godziny" if auto_check_enabled else "test · odczyt ręczny" }}</p></header><main class="wrap"><div class="actions"><button class="primary" onclick="openAdd()">＋ Dodaj hotel</button><button class="secondary" id="check" onclick="checkNow()">Sprawdź teraz</button><span class="status" id="status">Gotowe</span></div><section class="card"><h2>Monitorowane oferty</h2><div id="hotels"></div></section><div class="columns"><section class="card"><h2>Wykres ceny za osobę</h2><div class="chartwrap"><canvas id="chart"></canvas></div><div class="meta" id="chartname">Wybierz hotel</div></section><section class="card"><h2>Historia odczytów</h2><div class="tablewrap"><table><thead><tr><th>Data i godzina</th><th>Cena / osoba</th><th>Zmiana / osoba</th></tr></thead><tbody id="history"></tbody></table></div><div class="empty" id="nohistory" hidden>Brak odczytów. Użyj „Sprawdź teraz”.</div></section></div><p class="note">Cena za osobę to łączna cena oferty podzielona przez liczbę podróżnych. Przy dodawaniu hotelu wpisz ich liczbę. Link ITAKA powinien zawierać wybrany termin i parametry wyjazdu.</p></main>
<div class="modal" id="modal"><form class="dialog" onsubmit="addHotel(event)"><h2>Dodaj ofertę</h2><label>Nazwa hotelu<input name="name" required placeholder="np. Hotel nad morzem"></label><label>Pełny link do oferty ITAKA<input name="url" type="url" required placeholder="https://www.itaka.pl/..."></label><label>Liczba podróżnych<input name="people_count" type="number" min="1" max="20" value="2" required></label><div class="actions"><button type="button" class="secondary" onclick="closeAdd()">Anuluj</button><button class="primary">Zapisz hotel</button></div></form></div>
<script>
let hotels=[], selected=null, hist=[], selectedDate=null, expandedHotels=new Set(); const $=s=>document.querySelector(s); const money=v=>new Intl.NumberFormat('pl-PL',{style:'currency',currency:'PLN',maximumFractionDigits:2}).format(v); const date=s=>new Date(s+'Z').toLocaleString('pl-PL',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}); const dateOnly=s=>s?new Date(s+'T00:00:00').toLocaleDateString('pl-PL'):'—';
async function api(path,opt={}){let r=await fetch('/api/'+path,{headers:{'Content-Type':'application/json'},...opt});if(r.status===401){location='/login';throw Error('Zaloguj się ponownie.')}let d=await r.json();if(!r.ok)throw Error(d.error||'Wystąpił błąd');return d}
async function load(){try{let d=await api('hotels');hotels=d.hotels;if(!selected||!hotels.some(h=>h.id===selected)){selected=hotels[0]?.id||null;selectedDate=hotels[0]?.offers?.[0]?.date||hotels[0]?.departure_date||null}else{let h=hotels.find(x=>x.id===selected);if(!selectedDate||(!h?.offers?.some(o=>o.date===selectedDate)&&h?.offers?.length))selectedDate=h?.offers?.[0]?.date||h?.departure_date||null}drawHotels();await loadHistory()}catch(e){status(e.message,true)}}
function status(t,bad=false){$('#status').textContent=t;$('#status').style.color=bad?'#b42318':''}
function drawHotels(){let root=$('#hotels');if(!hotels.length){root.innerHTML='<div class="empty">Dodaj hotel, aby rozpocząć monitorowanie.</div>';return}root.innerHTML=hotels.map(h=>`<article class="hotel" onclick="selectHotel(${h.id})">${h.image_url?`<img class="thumb" src="${esc(h.image_url)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb missing',textContent:'🏝️'}))">`:`<div class="thumb missing">🏝️</div>`}<div><div class="hotelname">${esc(h.name)}</div><div class="meta">${h.people_count} os. · Wylot: ${dateOnly(h.departure_date)} · ${h.checked?`Ostatni odczyt: ${date(h.checked)}`:'Oczekuje na pierwszy odczyt'}</div></div><div><div class="price">${h.price===null?'—':money(h.price)} / os.</div>${h.total_price===null?'':`<div class="meta">Łącznie: ${money(h.total_price)}</div>`}</div><div class="rowbuttons"><button class="history-btn small" onclick="event.stopPropagation();selectHotel(${h.id},'${h.departure_date||''}')">Historia</button><button class="offer-btn small" onclick="event.stopPropagation();window.open('${esc(h.url)}','_blank')">Otwórz ofertę</button><button class="delete-btn small" onclick="event.stopPropagation();removeHotel(${h.id})">Usuń</button><button class="terms-btn small" onclick="event.stopPropagation();toggleDates(${h.id})">Terminy i ceny (${h.offers.length})</button></div><div class="departures ${expandedHotels.has(h.id)?'show':''}">${h.offers.length?h.offers.map(o=>`<button class="departure-choice ${selected===h.id&&selectedDate===o.date?'active':''}" onclick="event.stopPropagation();selectHotel(${h.id},'${o.date}')">Wylot ${dateOnly(o.date)} · ${money(o.price)} / os.</button>`).join(''):'<span class="meta">Kalendarz terminów będzie dostępny po odczycie ITAKA.</span>'}</div></article>`).join('')}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function selectHotel(id,departure){selected=id;selectedDate=departure||hotels.find(h=>h.id===id)?.offers?.[0]?.date||null;drawHotels();await loadHistory()}
function toggleDates(id){expandedHotels.has(id)?expandedHotels.delete(id):expandedHotels.add(id);drawHotels()}
async function loadHistory(){if(!selected){hist=[];$('#history').innerHTML='';$('#nohistory').hidden=false;drawChart();return}let suffix=selectedDate?`?date=${encodeURIComponent(selectedDate)}`:'';let d=await api(`hotels/${selected}/history${suffix}`);hist=d.history;$('#chartname').textContent=(hotels.find(h=>h.id===selected)?.name||'')+(selectedDate?' · wylot '+dateOnly(selectedDate):' · cena za osobę');$('#history').innerHTML=hist.map((x,i)=>`<tr><td>${date(x.checked_at)}</td><td>${money(x.price)} / os.</td><td>${i+1<hist.length?money(x.price-hist[i+1].price):'—'}</td></tr>`).join('');$('#nohistory').hidden=hist.length>0;drawChart()}
function drawChart(){let c=$('#chart'),dpr=devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;c.width=w*dpr;c.height=h*dpr;let x=c.getContext('2d');x.scale(dpr,dpr);x.clearRect(0,0,w,h);if(!hist.length){x.fillStyle='#819096';x.font='14px system-ui';x.textAlign='center';x.fillText('Wykres pojawi się po pierwszym odczycie',w/2,h/2);return}let a=[...hist].reverse(),v=a.map(p=>p.price),lo=Math.min(...v),hi=Math.max(...v),span=Math.max(hi-lo,1),pad={l:48,r:12,t:14,b:26};for(let i=0;i<4;i++){let y=pad.t+i*(h-pad.t-pad.b)/3;x.strokeStyle='#eaf0ef';x.beginPath();x.moveTo(pad.l,y);x.lineTo(w-pad.r,y);x.stroke();x.fillStyle='#829096';x.font='10px system-ui';x.textAlign='left';x.fillText(money(hi-i*span/3),2,y+3)}x.beginPath();a.forEach((p,i)=>{let px=pad.l+i*(w-pad.l-pad.r)/Math.max(a.length-1,1),py=pad.t+(hi-p.price)/span*(h-pad.t-pad.b);i?x.lineTo(px,py):x.moveTo(px,py)});x.strokeStyle='#148477';x.lineWidth=3;x.stroke();a.forEach((p,i)=>{let px=pad.l+i*(w-pad.l-pad.r)/Math.max(a.length-1,1),py=pad.t+(hi-p.price)/span*(h-pad.t-pad.b);x.fillStyle='#148477';x.beginPath();x.arc(px,py,3,0,7);x.fill()})}
function openAdd(){$('#modal').classList.add('show')}function closeAdd(){$('#modal').classList.remove('show')}
async function addHotel(e){e.preventDefault();let f=new FormData(e.target);try{await api('hotels',{method:'POST',body:JSON.stringify({name:f.get('name'),url:f.get('url'),people_count:Number(f.get('people_count'))})});closeAdd();e.target.reset();await load();status('Hotel dodany')}catch(x){status(x.message,true)}}
async function removeHotel(id){if(!confirm('Usunąć monitor razem z historią cen?'))return;try{await api(`hotels/${id}`,{method:'DELETE'});if(selected===id)selected=null;await load()}catch(e){status(e.message,true)}}
async function checkNow(){let b=$('#check');b.disabled=true;status('Sprawdzam ceny…');try{let d=await api('check',{method:'POST'});await load();status(d.message)}catch(e){status(e.message,true)}finally{b.disabled=false}}
window.addEventListener('resize',drawChart);load();setInterval(load,60000);
</script></body></html>'''

@app.get("/assets/<path:filename>")
def asset(filename):
    if filename not in {"background.webp", "napis.png"}: return "", 404
    return send_from_directory(APP_DIR, filename)

@app.get("/")
@auth_required
def index(): return render_template_string(PAGE, auto_check_enabled=AUTO_SCHEDULER)
@app.get("/api/hotels")
@auth_required
def api_hotels():
    c=connect(); rows=c.execute("SELECT h.id,h.name,h.url,h.people_count,h.image_url,p.price,p.checked_at,p.departure_date,p.per_person FROM hotels h LEFT JOIN prices p ON p.id=(SELECT id FROM prices WHERE hotel_id=h.id ORDER BY CASE WHEN departure_date>=date('now') THEN 0 ELSE 1 END,CASE WHEN departure_date>=date('now') THEN departure_date END ASC,id DESC LIMIT 1) ORDER BY h.id").fetchall()
    latest=c.execute("SELECT p.* FROM prices p JOIN (SELECT hotel_id,departure_date,MAX(id) id FROM prices WHERE departure_date IS NOT NULL AND departure_date>=date('now') GROUP BY hotel_id,departure_date) q ON q.id=p.id ORDER BY p.departure_date").fetchall(); c.close()
    offers={}
    for o in latest: offers.setdefault(o['hotel_id'],[]).append(dict(date=o['departure_date'],price=o['price'] if o['per_person'] else o['price']/next((h['people_count'] for h in rows if h['id']==o['hotel_id']),1),checked=o['checked_at']))
    return jsonify(hotels=[dict(id=r['id'],name=r['name'],url=r['url'],people_count=r['people_count'],image_url=r['image_url'],price=(r['price'] if r['per_person'] else r['price']/r['people_count']) if r['price'] is not None else None,total_price=(r['price']*r['people_count'] if r['per_person'] else r['price']),checked=r['checked_at'],departure_date=r['departure_date'],offers=offers.get(r['id'],[])) for r in rows])
@app.get("/api/hotels/<int:hid>/history")
@auth_required
def api_history(hid):
    departure_date=request.args.get('date')
    c=connect(); hotel=c.execute("SELECT people_count FROM hotels WHERE id=?",(hid,)).fetchone()
    rows=c.execute("SELECT price,checked_at,per_person FROM prices WHERE hotel_id=? AND (departure_date=? OR (? IS NULL AND departure_date IS NULL)) ORDER BY id DESC LIMIT 200",(hid,departure_date,departure_date)).fetchall(); c.close()
    count=hotel['people_count'] if hotel else 1
    return jsonify(history=[dict(price=r['price'] if r['per_person'] else r['price']/count,total_price=r['price'],checked_at=r['checked_at']) for r in rows])
@app.post("/api/hotels")
@auth_required
def api_add():
    d=request.get_json(force=True); name=str(d.get('name','')).strip(); url=str(d.get('url','')).strip()
    try: people_count=int(d.get('people_count',2))
    except (TypeError,ValueError): people_count=0
    if not name or not url.startswith('https://www.itaka.pl/') or not 1<=people_count<=20:return jsonify(error='Podaj nazwę, link ITAKA i liczbę podróżnych od 1 do 20.'),400
    c=connect(); cur=c.execute("INSERT INTO hotels(name,url,people_count) VALUES(?,?,?)",(name,url,people_count)); c.commit(); c.close(); return jsonify(id=cur.lastrowid)
@app.delete("/api/hotels/<int:hid>")
@auth_required
def api_remove(hid):
    c=connect(); c.execute("DELETE FROM prices WHERE hotel_id=?",(hid,)); c.execute("DELETE FROM hotels WHERE id=?",(hid,)); c.commit(); c.close(); return jsonify(ok=True)
def run_checks():
    c=connect(); hs=c.execute("SELECT id,url,people_count FROM hotels ORDER BY id").fetchall(); c.close(); errors=0
    for h in hs:
        try:
            total,image_url,departure_date,calendar_prices=fetch_offer(h['url']); stamp=datetime.utcnow().replace(microsecond=0).isoformat()
            calendar_prices=list(dict(calendar_prices).items())
            if departure_date:
                calendar_prices=[(d,total/h['people_count'] if d==departure_date else value) for d,value in calendar_prices]
                if not any(d==departure_date for d,_ in calendar_prices): calendar_prices.append((departure_date,total/h['people_count']))
            c=connect()
            if calendar_prices:
                for date_value,person_price in calendar_prices: c.execute("INSERT INTO prices(hotel_id,checked_at,price,departure_date,per_person) VALUES(?,?,?,?,1)",(h['id'],stamp,person_price,date_value))
            else: c.execute("INSERT INTO prices(hotel_id,checked_at,price,departure_date,per_person) VALUES(?,?,?,?,0)",(h['id'],stamp,total,departure_date))
            if image_url: c.execute("UPDATE hotels SET image_url=? WHERE id=?",(image_url,h['id']))
            c.commit(); c.close()
        except Exception as e:
            errors+=1; app.logger.warning("Price check failed for hotel %s: %s",h['id'],e)
    return len(hs),errors
@app.post("/api/check")
@auth_required
def api_check():
    total,errors=run_checks(); message=f"Odczyt zakończony · {errors} problem(y)" if errors else ("Odczyt zakończony · następny za 2 godz." if AUTO_SCHEDULER else "Odczyt zakończony · tryb ręczny")
    return jsonify(message=message,errors=errors,total=total)
@app.get("/health")
def health(): return "ok",200

def scheduler():
    time.sleep(15)
    while True:
        try: run_checks()
        except Exception: app.logger.exception("Scheduled check failed")
        time.sleep(INTERVAL_SECONDS)

if AUTO_SCHEDULER: threading.Thread(target=scheduler,daemon=True,name="price-scheduler").start()



