import base64, json, os, re, sqlite3, threading, time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from functools import wraps
from flask import Flask, request, jsonify, session, redirect, render_template_string, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
import urllib.request
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse
import github_state

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "please-change-this-secret-key")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
APP_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DB_PATH", str(APP_DIR / "history.db"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
INTERVAL_SECONDS = 2 * 60 * 60
AUTO_SCHEDULER = os.environ.get("DISABLE_SCHEDULER") != "1"
GITHUB_AUTOMATION_ENABLED = bool(os.environ.get("GITHUB_TOKEN", "").strip())

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

def fetch_dynamic_calendar(url):
    """Read ITAKA's client-rendered calendar, which includes later months."""
    from playwright.sync_api import sync_playwright
    collected=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=["--no-sandbox"])
        try:
            page=browser.new_page(locale="pl-PL",timezone_id="Europe/Warsaw")
            page.goto(url,wait_until="domcontentloaded",timeout=60000)
            when=page.get_by_role("button",name=re.compile(r"Kiedy:"))
            when.wait_for(state="visible",timeout=30000)
            when.click()
            page.get_by_role("button",name="Go to the Next Month").wait_for(state="visible",timeout=10000)
            def read_visible_months():
                return page.locator("button[aria-label]").evaluate_all("buttons => buttons.map(b => { const p=[...b.querySelectorAll('div')].find(x => /price/i.test(x.className)); return {label:b.getAttribute('aria-label')||'',price:p?.textContent||''}; })")
            collected.extend(read_visible_months())
            for _ in range(2):
                page.get_by_role("button",name="Go to the Next Month").click()
                collected.extend(read_visible_months())
        finally:
            browser.close()
    result=[]
    for item in collected:
        found=parse_calendar_price(item.get("label",""),item.get("price",""))
        if found: result.append(found)
    return list(dict(result).items())

def is_rainbow_url(url):
    host=(urlparse(url).hostname or "").lower()
    return urlparse(url).scheme=="https" and (host=="r.pl" or host.endswith(".r.pl"))

def sanitize_rainbow_url(url):
    """Remove passenger birth-date query values while keeping the offer key."""
    parts=urlparse(url)
    query=[(key,value) for key,value in parse_qsl(parts.query,keep_blank_values=True)
           if not key.lower().startswith(("wiek","dataurodzenia","birth"))]
    return urlunparse(parts._replace(query=urlencode(query)))

def is_itaka_url(url):
    host=(urlparse(url).hostname or "").lower()
    return host=="itaka.pl" or host.endswith(".itaka.pl")

def is_supported_offer_url(url):
    parts=urlparse(url)
    host=(parts.hostname or "").lower()
    return parts.scheme=="https" and (
        host=="www.itaka.pl" or host=="itaka.pl" or host.endswith(".itaka.pl")
        or host=="r.pl" or host.endswith(".r.pl")
    )

RAINBOW_MONTHS={"sty":"01","lut":"02","mar":"03","kwi":"04","maj":"05","cze":"06","lip":"07","sie":"08","wrz":"09","paź":"10","paz":"10","lis":"11","gru":"12"}
RAINBOW_MONTH_NAMES={"01":"styczeń","02":"luty","03":"marzec","04":"kwiecień","05":"maj","06":"czerwiec","07":"lipiec","08":"sierpień","09":"wrzesień","10":"październik","11":"listopad","12":"grudzień"}

def parse_rainbow_calendar_price(label, visible_text=""):
    label=re.sub(r"\s+"," ",(label or "")).replace("\u00a0"," ").strip()
    visible_text=re.sub(r"\s+"," ",(visible_text or "")).replace("\u00a0"," ").strip()
    date_pattern=r"(?<!\d)(\d{1,2})\s+(sty|lut|mar|kwi|maj|cze|lip|sie|wrz|paź|paz|lis|gru)\s+(20\d{2})"
    source=label
    m=re.search(date_pattern,label,re.I)
    if not m:
        source=visible_text
        m=re.search(date_pattern,visible_text,re.I)
    if not m:
        return None
    month=RAINBOW_MONTHS.get(m.group(2).lower())
    raw=None
    suffix=source[m.end():]
    direct=re.match(r"\s*[,·-]\s*([\d\s]+(?:[,.]\d{2})?)",suffix)
    if direct:
        raw=direct.group(1)
    else:
        # Rainbow exposes the accessible date label separately from the day and price text.
        # The visible text often starts with the day number again, so try removing it first.
        day_prefix=re.sub(rf"^\s*{int(m.group(1))}\s+", "", visible_text, count=1)
        for candidate in (day_prefix,visible_text):
            price_match=re.search(r"(?<!\d)(\d[\d\s]*(?:[,.]\d{2})?)",candidate)
            if price_match:
                digits=re.sub(r"\D","",price_match.group(1))
                if len(digits)>=4:
                    raw=price_match.group(1)
                    break
    if raw is None:
        return None
    digits=re.sub(r"\D","",raw)
    if len(digits)<4:
        return None
    try:
        price=float(re.sub(r"\s","",raw).replace(",","."))
        departure=datetime.strptime(f"{m.group(3)}-{month}-{int(m.group(1)):02d}","%Y-%m-%d").date().isoformat()
    except (TypeError,ValueError):
        return None
    return (departure,price) if month and price>0 else None

def rainbow_calendar_target():
    today=datetime.utcnow().date()
    month_index=today.month-1+2
    year=today.year+month_index//12
    month=month_index%12+1
    return f"{RAINBOW_MONTH_NAMES[f'{month:02d}']} {year}"

def fetch_rainbow_offer(url, people_count=2):
    """Read the selected Rainbow offer and the visible departure-price calendar through the end of the second future month."""
    from playwright.sync_api import sync_playwright
    safe_url=sanitize_rainbow_url(url)
    calendar_prices=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=["--no-sandbox"])
        try:
            page=browser.new_page(locale="pl-PL",timezone_id="Europe/Warsaw")
            page.goto(safe_url,wait_until="domcontentloaded",timeout=60000)
            try:
                page.get_by_text(re.compile(r"Cena\s+razem",re.I)).first.wait_for(state="visible",timeout=30000)
            except Exception:
                page.locator("body").wait_for(state="visible",timeout=10000)

            # Rainbow blocks the offer calendar with a consent dialog on a fresh browser.
            # Keep optional cookies disabled: open settings and save the required-only defaults.
            consent_overlay=page.locator(".r-consent-bottom-notif__modal:visible")
            for _ in range(3):
                if not consent_overlay.count():
                    break
                save_settings=page.get_by_role("button",name=re.compile(r"ZAPISZ I WYJDŹ",re.I)).last
                if save_settings.count() and save_settings.is_visible():
                    save_settings.click(timeout=10000)
                else:
                    cookie_settings=page.get_by_role("button",name=re.compile(r"USTAWIENIA PLIKÓW COOKIE",re.I)).last
                    if not cookie_settings.count() or not cookie_settings.is_visible():
                        raise ValueError("Rainbow wyświetliło okno cookies, którego nie udało się zamknąć.")
                    cookie_settings.click(timeout=10000)
            consent_overlay.wait_for(state="hidden",timeout=15000)

            # The consent prompt can appear asynchronously while the offer configurator loads.
            # Check it again after the loader settles, before touching the date control.
            page.locator('[id-dti="loader"]').wait_for(state="hidden",timeout=30000)
            consent_overlay=page.locator(".r-consent-bottom-notif__modal:visible")
            for _ in range(3):
                if not consent_overlay.count():
                    break
                save_settings=page.get_by_role("button",name=re.compile(r"ZAPISZ I WYJDŹ",re.I)).last
                if save_settings.count() and save_settings.is_visible():
                    save_settings.click(timeout=10000)
                else:
                    cookie_settings=page.get_by_role("button",name=re.compile(r"USTAWIENIA PLIKÓW COOKIE",re.I)).last
                    if not cookie_settings.count() or not cookie_settings.is_visible():
                        raise ValueError("Rainbow wyświetliło okno cookies, którego nie udało się zamknąć.")
                    cookie_settings.click(timeout=10000)
            consent_overlay.wait_for(state="hidden",timeout=15000)
            page_text=page.locator("body").inner_text(timeout=20000)
            image_url=None
            image=page.locator('meta[property="og:image"]')
            if image.count():
                image_url=image.first.get_attribute("content")

            date_picker=page.get_by_role("button",name=re.compile(r"\d{2}\.\d{2}\.20\d{2}\s*[-–]\s*\d{2}\.\d{2}\.20\d{2}"))
            date_picker.wait_for(state="visible",timeout=15000)
            date_picker.click()
            day_button=page.get_by_role("button",name=re.compile(r"\d{1,2}\s+(?:sty|lut|mar|kwi|maj|cze|lip|sie|wrz|paź|paz|lis|gru)\s+20\d{2}",re.I))
            day_button.first.wait_for(state="visible",timeout=15000)
            seen={}
            def collect_visible_calendar_prices():
                buttons=page.get_by_role("button").evaluate_all("""buttons => buttons.map(b => ({label:b.getAttribute('aria-label')||b.getAttribute('title')||'', text:b.innerText||b.textContent||''}))""")
                for button in buttons:
                    found=parse_rainbow_calendar_price(button.get("label",""),button.get("text",""))
                    if found:
                        seen[found[0]]=found[1]
            collect_visible_calendar_prices()
            target_month=rainbow_calendar_target()
            target_year=target_month[-4:]
            target_name=target_month.rsplit(" ",1)[0].casefold()
            target_month_number=next((number for number,name in RAINBOW_MONTH_NAMES.items() if name.casefold()==target_name),None)
            target_prefix=f"{target_year}-{target_month_number}-" if target_month_number else None
            for _ in range(4):
                if target_prefix and any(date.startswith(target_prefix) for date in seen):
                    break
                next_button=page.get_by_role("button",name="Następna",exact=True)
                if not next_button.count() or not next_button.is_visible():
                    break
                next_button.click()
                collect_visible_calendar_prices()
            if target_prefix and not any(date.startswith(target_prefix) for date in seen):
                app.logger.warning("Rainbow nie pokazało cen do miesiąca %s; zapisuję dostępne terminy",target_month)
            collect_visible_calendar_prices()
            calendar_prices=list(seen.items())
            if not calendar_prices:
                samples=page.get_by_role("button").evaluate_all("""buttons => buttons.filter(b => /20\\d{2}/.test((b.getAttribute('aria-label')||'')+' '+(b.getAttribute('title')||'')+' '+(b.innerText||b.textContent||''))).slice(0,20).map(b => ({label:b.getAttribute('aria-label')||b.getAttribute('title')||'', text:b.innerText||b.textContent||''}))""")
                app.logger.warning("Rainbow calendar sample buttons: %s",samples)
                raise ValueError("Rainbow nie udostępniło cen w kalendarzu wyjazdów.")
        finally:
            browser.close()
    text=re.sub(r"\s+"," ",page_text).replace("\u00a0"," ")
    total_match=re.search(r"Cena\s+razem\s*:?\s*([\d\s.,]+)\s*zł",text,re.I)
    if total_match:
        raw=re.sub(r"\s","",total_match.group(1)).replace(",",".")
        total=float(raw)
    else:
        per_person_match=re.search(r"([\d\s.,]+)\s*zł\s*/\s*os\.",text,re.I)
        if not per_person_match:
            raise ValueError("Rainbow nie udostępniło teraz ceny wybranej oferty.")
        raw=re.sub(r"\s","",per_person_match.group(1)).replace(",",".")
        total=round(float(raw)*max(1,int(people_count)),2)
    price_position=total_match.start() if total_match else per_person_match.start()
    nearby=text[max(0,price_position-1200):price_position]
    date_matches=list(re.finditer(r"(?<!\d)(\d{2}\.\d{2}\.20\d{2})\s*[-–]\s*\d{2}\.\d{2}\.20\d{2}",nearby))
    if not date_matches:
        raise ValueError("Rainbow nie udostępniło daty wybranego wylotu.")
    departure_date=datetime.strptime(date_matches[-1].group(1),"%d.%m.%Y").date().isoformat()
    image_url=urljoin(url,image_url) if image_url else None
    if image_url and urlparse(image_url).scheme!="https": image_url=None
    airport_match=re.search(r"(Warszawa(?:\s*(?:-|–)?\s*(?:Chopin|Modlin))?|Poznań|Katowice|Wrocław|Gdańsk|Kraków|Rzeszów|Łódź|Szczecin|Bydgoszcz)\s+wylot\s+\d{1,2}:\d{2}",text,re.I)
    airport_name=airport_match.group(1).strip() if airport_match else "Lotnisko z oferty"
    airport_name=re.sub(r"\s+"," ",airport_name)
    airport_code="R-"+re.sub(r"[^A-Z0-9]+","-",airport_name.upper().replace("Ł","L").replace("Ó","O").replace("Ą","A").replace("Ę","E").replace("Ś","S").replace("Ć","C").replace("Ń","N").replace("Ż","Z").replace("Ź","Z")).strip("-")
    return total,image_url,departure_date,calendar_prices,True,airport_code,airport_name

def rainbow_airport_key(name):
    normalized=name.upper().replace("Ł","L").replace("Ó","O").replace("Ą","A").replace("Ę","E").replace("Ś","S").replace("Ć","C").replace("Ń","N").replace("Ż","Z").replace("Ź","Z")
    return "R-"+re.sub(r"[^A-Z0-9]+","-",normalized).strip("-")

def itaka_airport_options(url):
    """Return only enabled departure airports shown on this ITAKA offer."""
    from playwright.sync_api import sync_playwright
    current_code,current_name=airport_code_from_url(url)
    found=[]
    code_by_label={name.casefold():code for code,name in AIRPORT_NAMES.items()}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=["--no-sandbox"])
        try:
            page=browser.new_page(locale="pl-PL",timezone_id="Europe/Warsaw")
            # Don't wait for ITAKA analytics and tracking requests to finish.
            # ITAKA may keep DOMContentLoaded open because of analytics. Commit means
            # navigation has started; offer controls can hydrate afterwards.
            page.goto(url,wait_until="commit",timeout=20000)
            control=page.get_by_text(re.compile(r"Skąd i jak",re.I)).first
            try: control.wait_for(state="visible",timeout=5000)
            except Exception: pass
            if control.count() and control.is_visible():
                try: control.click(timeout=5000)
                except Exception: pass
            page.wait_for_timeout(700)
            items=page.locator('input[type="checkbox"]').evaluate_all("""els=>els.map(e=>({value:(e.value||'').toUpperCase(),disabled:e.disabled,label:[...(e.labels||[])].map(x=>x.innerText||'').join(' ')+' '+(e.parentElement?.innerText||'')}))""")
            for item in items:
                if item["disabled"]: continue
                label=re.sub(r"\s+"," ",item["label"]).strip()
                code=item["value"] if item["value"] in AIRPORT_NAMES else None
                if not code:
                    code=next((c for name,c in code_by_label.items() if name in label.casefold()),None)
                if code and not any(o["code"]==code for o in found):
                    found.append({"code":code,"name":AIRPORT_NAMES[code]})
        finally:
            browser.close()
    if current_code in AIRPORT_NAMES and not any(o["code"]==current_code for o in found):
        found.insert(0,{"code":current_code,"name":current_name})
    return found or [{"code":current_code,"name":current_name}]

def dismiss_rainbow_cookie_overlay(page):
    """Save the current necessary-only cookie choice, including delayed overlays."""
    overlay=page.locator(".r-consent-bottom-notif__modal:visible")
    for _ in range(3):
        if not overlay.count(): return
        save=page.get_by_role("button",name=re.compile(r"ZAPISZ I WYJDŹ",re.I)).last
        try:
            save.wait_for(state="visible",timeout=2500)
        except Exception:
            settings=page.get_by_role("button",name=re.compile(r"USTAWIENIA PLIKÓW COOKIE",re.I)).last
            try:
                settings.wait_for(state="visible",timeout=5000)
                settings.click(timeout=10000)
                save.wait_for(state="visible",timeout=8000)
            except Exception as e:
                labels=page.locator("button").evaluate_all("""els=>els.filter(b=>b.offsetParent!==null).map(b=>(b.innerText||b.textContent||'').trim()).filter(Boolean).slice(0,20)""")
                # Rainbow sometimes leaves a stale consent overlay in the DOM without its
                # controls, although the actual date or flight control is already usable.
                flight_control=page.get_by_role("button",name=re.compile(r"wylot[ ]+[0-9]{1,2}:[0-9]{2}",re.I)).first
                date_control=page.get_by_role("button",name=re.compile(r"[0-9]{2}[.][0-9]{2}[.]20[0-9]{2}[ ]*[-–][ ]*[0-9]{2}[.][0-9]{2}[.]20[0-9]{2}"))
                if (flight_control.count() and flight_control.is_visible()) or (date_control.count() and date_control.is_visible()):
                    app.logger.warning("Rainbow consent overlay has no controls; continuing because offer controls are visible: %s",labels)
                    return
                app.logger.warning("Rainbow consent controls not ready; visible buttons: %s",labels)
                raise ValueError("Rainbow wyświetliło okno cookies, ale nie załadowało jego przycisków.") from e
        save.click(timeout=10000)
        try: overlay.wait_for(state="hidden",timeout=10000)
        except Exception: page.wait_for_timeout(500)
    if overlay.count():
        raise ValueError("Rainbow ponownie wyświetliło okno cookies.")


RAINBOW_FLIGHT_CARD_SELECTOR='[data-test-id^="r-component:pojedyncze-polaczenie:container-"]'

def read_rainbow_flight_cards(page):
    """Read complete flight cards, including the selected flight and h6 date headings."""
    return page.evaluate(r"""() => {
        const selector='[data-test-id^="r-component:pojedyncze-polaczenie:container-"]';
        const nodes=[...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"],'+selector)];
        let heading='',i=0;
        const result=[];
        for (const node of nodes) {
            const text=node.innerText||node.textContent||'';
            if ((node.getAttribute('data-test-id')||'').startsWith('r-component:pojedyncze-polaczenie:container-')) {
                const action=[...node.querySelectorAll('button')].find(button=>/^\s*(WYBIERZ|WYBRANY)\s*$/i.test(button.innerText||button.textContent||''));
                result.push({i:i++,text,heading,selected:/^\s*WYBRANY\s*$/i.test(action?.innerText||action?.textContent||'')});
            } else if (/\d{1,2}\s+[a-ząćęłńóśźż]+\s+20\d{2}\s*[-–]/i.test(text)) {
                heading=text.trim();
            }
        }
        return result;
    }""")

def rainbow_airports_from_cards(cards, departure_date):
    """Keep only airports actually present under the requested departure date."""
    found=[]
    airport_re=re.compile(r"Warszawa(?:\s*(?:-|–)?\s*(?:Chopin|Modlin))?|Poznań|Katowice|Wrocław|Gdańsk|Kraków|Rzeszów|Łódź|Szczecin|Bydgoszcz",re.I)
    for card in cards:
        heading=re.search(r"(?<!\d)(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})",card.get("heading",""),re.I)
        if not heading:
            continue
        month=PL_MONTHS.get(heading.group(2).lower())
        if not month:
            continue
        try:
            date=datetime.strptime(f"{heading.group(3)}-{month}-{int(heading.group(1)):02d}","%Y-%m-%d").date().isoformat()
        except ValueError:
            continue
        if date!=departure_date:
            continue
        match=airport_re.search(card.get("text",""))
        if not match:
            continue
        name=re.sub(r"\s+"," ",match.group(0)).strip()
        code=rainbow_airport_key(name)
        if not any(item["code"]==code for item in found):
            found.append(dict(code=code,name=name,departure_date=date,i=card["i"],selected=card.get("selected",False)))
    return found

def open_rainbow_control(page, control, expected):
    """Wait for hydrated controls and recheck late consent overlays before retrying."""
    for attempt in range(3):
        dismiss_rainbow_cookie_overlay(page)
        control.wait_for(state="visible",timeout=20000)
        # A stale visual loader can cover a working control. A DOM click invokes the
        # intended button without discarding consent state or changing page styles.
        control.evaluate("(button)=>button.click()")
        try:
            expected.wait_for(state="visible",timeout=7000)
            return
        except Exception:
            if attempt==2:
                raise
    raise ValueError("Rainbow nie otworzyło wybranego panelu.")

def rainbow_airport_variants(url, departure_date=None, selected_code=None):
    """Inspect or select Rainbow flight alternatives for one specific departure date."""
    from playwright.sync_api import sync_playwright
    safe_url=sanitize_rainbow_url(url)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=["--no-sandbox"])
        try:
            page=browser.new_page(locale="pl-PL",timezone_id="Europe/Warsaw")
            page.goto(safe_url,wait_until="domcontentloaded",timeout=60000)
            page.get_by_text(re.compile(r"Cena\s+razem",re.I)).first.wait_for(state="visible",timeout=30000)
            dismiss_rainbow_cookie_overlay(page)
            try:
                page.locator('[id-dti="loader"]').wait_for(state="hidden",timeout=10000)
            except Exception:
                pass
            dismiss_rainbow_cookie_overlay(page)
            range_pattern=re.compile(r"[0-9]{2}[.][0-9]{2}[.]20[0-9]{2}[ ]*[-–][ ]*[0-9]{2}[.][0-9]{2}[.]20[0-9]{2}")
            picker=page.get_by_role("button",name=range_pattern).first
            picker.wait_for(state="visible",timeout=20000)
            current_match=re.search(r"\d{2}\.\d{2}\.20\d{2}",picker.inner_text())
            current_date=datetime.strptime(current_match.group(0),"%d.%m.%Y").date().isoformat() if current_match else None
            target=datetime.strptime(departure_date or current_date,"%Y-%m-%d").date()
            requested_date=target.isoformat()
            if current_date!=requested_date:
                day_pattern=re.compile(r"\d{1,2}\s+[a-ząćęłńóśźż]+\s+20\d{2}",re.I)
                open_rainbow_control(page,picker,page.get_by_role("button",name=day_pattern).first)
                month_labels={"01":"(?:sty|stycznia)","02":"(?:lut|lutego)","03":"(?:mar|marca)","04":"(?:kwi|kwietnia)","05":"(?:maj|maja)","06":"(?:cze|czerwca)","07":"(?:lip|lipca)","08":"(?:sie|sierpnia)","09":"(?:wrz|września)","10":"(?:paź|paz|października)","11":"(?:lis|listopada)","12":"(?:gru|grudnia)"}
                target_button=page.get_by_role("button",name=re.compile(rf"(?<![0-9])0?{target.day}\s+{month_labels[target.strftime('%m')]}\s+{target.year}",re.I)).first
                for _ in range(12):
                    if target_button.count() and target_button.is_visible():
                        break
                    nxt=page.get_by_role("button",name="Następna",exact=True)
                    if not nxt.count() or not nxt.is_visible():
                        break
                    nxt.click(timeout=5000)
                    try:
                        target_button.wait_for(state="visible",timeout=1000)
                    except Exception:
                        pass
                if not target_button.count() or not target_button.is_visible() or not target_button.is_enabled():
                    raise ValueError(f"Rainbow nie udostępniło terminu {requested_date} w kalendarzu.")
                target_button.click(timeout=10000)
                # A day click only previews the trip. ZAPISZ applies the new date and offer key.
                save=page.get_by_role("button",name="ZAPISZ",exact=True)
                save.wait_for(state="visible",timeout=10000)
                before_save=page.url
                save.click(timeout=10000)
                page.get_by_role("button",name=re.compile(r"^\s*"+re.escape(target.strftime("%d.%m.%Y"))+r"\s*[-–]")).first.wait_for(state="visible",timeout=30000)
                page.wait_for_function("(previous)=>location.href!==previous",arg=before_save,timeout=30000)
                dismiss_rainbow_cookie_overlay(page)
            flight=page.get_by_role("button",name=re.compile(r"wylot\s+[0-9]{1,2}:[0-9]{2}",re.I)).first
            flight.wait_for(state="visible",timeout=20000)
            flight_cards=page.locator(RAINBOW_FLIGHT_CARD_SELECTOR)
            open_rainbow_control(page,flight,flight_cards.first)
            options=rainbow_airports_from_cards(read_rainbow_flight_cards(page),requested_date)
            if not options:
                raise ValueError(f"Rainbow nie pokazało kart lotów dla terminu {requested_date}.")
            airports=[dict(code=item["code"],name=item["name"],departure_date=requested_date) for item in options]
            if selected_code:
                option=next((item for item in options if item["code"]==selected_code),None)
                if option is None:
                    raise ValueError("To lotnisko nie jest dostępne dla wybranego terminu Rainbow.")
                if not option["selected"]:
                    before_select=page.url
                    flight_cards.nth(option["i"]).get_by_role("button",name="WYBIERZ",exact=True).click(timeout=10000)
                    page.wait_for_function("(previous)=>location.href!==previous",arg=before_select,timeout=30000)
                    page.get_by_role("button",name=re.compile(r"^\s*"+re.escape(option["name"])+r"\s+wylot",re.I)).first.wait_for(state="visible",timeout=20000)
            return {"url":sanitize_rainbow_url(page.url),"airports":airports}
        except Exception:
            if os.environ.get("GITHUB_ACTIONS")=="true":
                debug_dir=Path(os.environ.get("RUNNER_TEMP",str(APP_DIR)))/"rainbow-debug"
                debug_dir.mkdir(parents=True,exist_ok=True)
                filename=re.sub(r"[^a-zA-Z0-9_-]","-",urlparse(safe_url).path.strip("/"))+"-"+str(departure_date or "selected")
                try:
                    page.screenshot(path=str(debug_dir/(filename+".png")),full_page=True)
                    labels=page.get_by_role("button").evaluate_all("buttons=>buttons.filter(b=>b.offsetParent!==null).map(b=>b.getAttribute('aria-label')||b.innerText||b.textContent||'').slice(0,35)")
                    app.logger.warning("Rainbow debug buttons for %s: %s",departure_date,labels)
                except Exception:
                    pass
            raise
        finally:
            browser.close()

def fetch_offer(url, people_count=2):
    if is_rainbow_url(url):
        return fetch_rainbow_offer(url,people_count)

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
                calendar_observed=False
                try:
                    dynamic_prices=fetch_dynamic_calendar(url)
                    calendar_observed=True
                    calendar_prices=[(d,price+fee) for d,price in dynamic_prices]
                except Exception as e:
                    app.logger.warning("Dynamic ITAKA calendar unavailable; using page HTML: %s",e)
                return val,image_url,departure_date,calendar_prices,calendar_observed
    try:
        dynamic_prices=fetch_dynamic_calendar(url)
        return None,image_url,departure_date,dynamic_prices,True
    except Exception as e:
        app.logger.warning("Could not inspect ITAKA calendar after missing offer price: %s",e)
    if p.calendar_prices:
        return None,image_url,departure_date,p.calendar_prices,False
    raise ValueError("ITAKA nie udostępniła ceny wybranej oferty. Nie zapisano odczytu.")

def country_from_url(url):
    path=urlparse(url).path.lower()
    if "wyspy-kanaryjskie" in path or "teneryfa" in path: return "Hiszpania"
    parts=[p for p in path.split("/") if p]
    known={"madagaskar":"Madagaskar","grecja":"Grecja","hiszpania":"Hiszpania","turcja":"Turcja","egipt":"Egipt","włochy":"Włochy","wlochy":"Włochy","portugalia":"Portugalia","cypr":"Cypr","tunezja":"Tunezja","maroko":"Maroko","chorwacja":"Chorwacja","dominikana":"Dominikana","meksyk":"Meksyk","kenia":"Kenia","malediwy":"Malediwy","maldives":"Malediwy","wyspy-zielonego-przyladka":"Republika Zielonego Przylądka"}
    for part in parts:
        if part in known: return known[part]
    return "Kraj nieustalony"

AIRPORT_NAMES={"WAW":"Warszawa-Okęcie","POZ":"Poznań","KTW":"Katowice","WRO":"Wrocław","GDN":"Gdańsk","KRK":"Kraków","RZE":"Rzeszów","LCJ":"Łódź","SZZ":"Szczecin","BZG":"Bydgoszcz"}
def airport_code_from_url(url):
    query=parse_qsl(urlparse(url).query,keep_blank_values=True)
    direct=next((value.upper() for key,value in query if key.lower()=="airports"),"")
    if direct in AIRPORT_NAMES: return direct,AIRPORT_NAMES[direct]
    # ITAKA embeds the selected departure airport in its base64-encoded id[0] offer key.
    # Decode it before falling back to the generic label so existing seeded offers also
    # display their airport when the page/API cannot be inspected.
    for key,value in query:
        if not key.lower().startswith("id") or not value: continue
        candidates=[value]
        try: candidates.append(unquote(value))
        except Exception: pass
        for candidate in candidates:
            try:
                token=candidate.strip()
                token += "="*((4-len(token)%4)%4)
                decoded=base64.urlsafe_b64decode(token.encode("ascii")).decode("latin1",errors="ignore").upper()
            except Exception:
                continue
            for code in AIRPORT_NAMES:
                if code in decoded:
                    return code,AIRPORT_NAMES[code]
    return "DEFAULT","Lotnisko z oferty"

def connect():
    fresh_database=not Path(DB_PATH).exists()
    con=sqlite3.connect(DB_PATH,timeout=30); con.row_factory=sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("CREATE TABLE IF NOT EXISTS hotels(id INTEGER PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL,people_count INTEGER NOT NULL DEFAULT 2,image_url TEXT,last_checked_at TEXT)")
    columns={row[1] for row in con.execute("PRAGMA table_info(hotels)").fetchall()}
    if "people_count" not in columns: con.execute("ALTER TABLE hotels ADD COLUMN people_count INTEGER NOT NULL DEFAULT 2")
    if "image_url" not in columns: con.execute("ALTER TABLE hotels ADD COLUMN image_url TEXT")
    if "last_checked_at" not in columns: con.execute("ALTER TABLE hotels ADD COLUMN last_checked_at TEXT")
    if "country" not in columns: con.execute("ALTER TABLE hotels ADD COLUMN country TEXT")
    if "airport_code" not in columns: con.execute("ALTER TABLE hotels ADD COLUMN airport_code TEXT NOT NULL DEFAULT 'DEFAULT'")
    if "airport_name" not in columns: con.execute("ALTER TABLE hotels ADD COLUMN airport_name TEXT")
    if "airport_options" not in columns: con.execute("ALTER TABLE hotels ADD COLUMN airport_options TEXT NOT NULL DEFAULT '[]'")
    con.execute("UPDATE hotels SET country=COALESCE(country,'Kraj nieustalony'),airport_name=COALESCE(airport_name,'Lotnisko z oferty')")
    for row in con.execute("SELECT id,url,country,airport_code,airport_name FROM hotels").fetchall():
        inferred_country=country_from_url(row["url"])
        inferred_code,inferred_name=airport_code_from_url(row["url"])
        inferred_airport=AIRPORT_NAMES.get(row["airport_code"])
        if inferred_country!="Kraj nieustalony" and row["country"] in (None,"","Kraj nieustalony"):
            con.execute("UPDATE hotels SET country=? WHERE id=?",(inferred_country,row["id"]))
        if inferred_code!="DEFAULT" and row["airport_code"] in (None,"","DEFAULT"):
            con.execute("UPDATE hotels SET airport_code=?,airport_name=? WHERE id=?",(inferred_code,inferred_name,row["id"]))
        elif inferred_airport and row["airport_name"] in (None,"","Lotnisko z oferty"):
            con.execute("UPDATE hotels SET airport_name=? WHERE id=?",(inferred_airport,row["id"]))
    con.execute("CREATE TABLE IF NOT EXISTS prices(id INTEGER PRIMARY KEY,hotel_id INTEGER NOT NULL,checked_at TEXT NOT NULL,price REAL NOT NULL,FOREIGN KEY(hotel_id) REFERENCES hotels(id) ON DELETE CASCADE)")
    price_columns={row[1] for row in con.execute("PRAGMA table_info(prices)").fetchall()}
    if "departure_date" not in price_columns: con.execute("ALTER TABLE prices ADD COLUMN departure_date TEXT")
    if "per_person" not in price_columns: con.execute("ALTER TABLE prices ADD COLUMN per_person INTEGER NOT NULL DEFAULT 0")
    if "airport_code" not in price_columns: con.execute("ALTER TABLE prices ADD COLUMN airport_code TEXT NOT NULL DEFAULT 'DEFAULT'")
    con.execute("CREATE TABLE IF NOT EXISTS tracked_departures_by_airport(hotel_id INTEGER NOT NULL,airport_code TEXT NOT NULL,departure_date TEXT NOT NULL,last_seen_at TEXT,missing_checks INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'available',current_price REAL,previous_price REAL,PRIMARY KEY(hotel_id,airport_code,departure_date),FOREIGN KEY(hotel_id) REFERENCES hotels(id) ON DELETE CASCADE)")
    con.execute("CREATE TABLE IF NOT EXISTS tracked_departures(hotel_id INTEGER NOT NULL,departure_date TEXT NOT NULL,last_seen_at TEXT,missing_checks INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'available',PRIMARY KEY(hotel_id,departure_date),FOREIGN KEY(hotel_id) REFERENCES hotels(id) ON DELETE CASCADE)")
    departure_columns={row[1] for row in con.execute("PRAGMA table_info(tracked_departures)").fetchall()}
    if "current_price" not in departure_columns: con.execute("ALTER TABLE tracked_departures ADD COLUMN current_price REAL")
    if "previous_price" not in departure_columns: con.execute("ALTER TABLE tracked_departures ADD COLUMN previous_price REAL")
    # Attribute legacy history to each hotel's currently selected airport on migration.
    for hotel in con.execute("SELECT id,url,airport_code,airport_name,country FROM hotels").fetchall():
        code,name=airport_code_from_url(hotel["url"])
        con.execute("UPDATE hotels SET airport_code=CASE WHEN airport_code='DEFAULT' THEN ? ELSE airport_code END,airport_name=CASE WHEN airport_name='Lotnisko z oferty' THEN ? ELSE airport_name END,country=CASE WHEN country='Kraj nieustalony' THEN ? ELSE country END WHERE id=?",(code,name,country_from_url(hotel["url"]),hotel["id"]))
        con.execute("UPDATE prices SET airport_code=? WHERE hotel_id=? AND airport_code='DEFAULT'",(code,hotel["id"]))
        con.execute("INSERT OR IGNORE INTO tracked_departures_by_airport(hotel_id,airport_code,departure_date,last_seen_at,missing_checks,status,current_price,previous_price) SELECT hotel_id,?,departure_date,last_seen_at,missing_checks,status,current_price,previous_price FROM tracked_departures WHERE hotel_id=?",(code,hotel["id"]))
    con.execute("INSERT OR IGNORE INTO tracked_departures(hotel_id,departure_date,last_seen_at,status) SELECT hotel_id,departure_date,MAX(checked_at),'available' FROM prices WHERE departure_date IS NOT NULL GROUP BY hotel_id,departure_date")
    con.execute("UPDATE tracked_departures SET current_price=(SELECT CASE WHEN p.per_person=1 THEN p.price ELSE p.price/h.people_count END FROM prices p JOIN hotels h ON h.id=p.hotel_id WHERE p.hotel_id=tracked_departures.hotel_id AND p.departure_date=tracked_departures.departure_date ORDER BY p.id DESC LIMIT 1) WHERE current_price IS NULL")
    if fresh_database and con.execute("SELECT COUNT(*) FROM hotels").fetchone()[0]==0: con.executemany("INSERT INTO hotels(name,url) VALUES(?,?)",SEEDS)
    con.commit(); return con

initdb=connect; c=initdb(); c.close()

def state_from_database():
    c=connect()
    state={"hotels":[dict(row) for row in c.execute("SELECT id,name,url,people_count,image_url,last_checked_at,country,airport_code,airport_name,airport_options FROM hotels ORDER BY id")],
           "prices":[dict(row) for row in c.execute("SELECT id,hotel_id,checked_at,price,departure_date,per_person,airport_code FROM prices ORDER BY id")],
           "departures":[dict(row) for row in c.execute("SELECT hotel_id,airport_code,departure_date,last_seen_at,missing_checks,status,current_price,previous_price FROM tracked_departures_by_airport ORDER BY hotel_id,airport_code,departure_date")],
           "updated_at":datetime.utcnow().replace(microsecond=0).isoformat()}
    c.close()
    return state

def save_state_to_github():
    if not os.environ.get("GITHUB_TOKEN"):
        return False
    github_state.write_state(state_from_database())
    return True

def sync_state_from_github():
    """Refresh the local cache from the dedicated GitHub history branch."""
    if not os.environ.get("GITHUB_TOKEN"):
        return False
    state=github_state.read_state()
    if state is None:
        save_state_to_github()
        return False
    if not isinstance(state.get("hotels"),list) or not isinstance(state.get("prices"),list):
        raise RuntimeError("Dane historii GitHub mają nieprawidłowy format.")
    c=connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM prices")
        c.execute("DELETE FROM tracked_departures_by_airport")
        c.execute("DELETE FROM tracked_departures")
        c.execute("DELETE FROM hotels")
        c.executemany("INSERT INTO hotels(id,name,url,people_count,image_url,last_checked_at,country,airport_code,airport_name,airport_options) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      [(h["id"],h["name"],h["url"],h.get("people_count",2),h.get("image_url"),h.get("last_checked_at"),h.get("country") or country_from_url(h["url"]),h.get("airport_code") or airport_code_from_url(h["url"])[0],h.get("airport_name") or airport_code_from_url(h["url"])[1],h.get("airport_options") if isinstance(h.get("airport_options"),str) else json.dumps(h.get("airport_options",[]),ensure_ascii=False)) for h in state["hotels"]])
        c.executemany("INSERT INTO prices(id,hotel_id,checked_at,price,departure_date,per_person,airport_code) VALUES(?,?,?,?,?,?,?)",
                      [(p["id"],p["hotel_id"],p["checked_at"],p["price"],p.get("departure_date"),p.get("per_person",0),p.get("airport_code") or "DEFAULT") for p in state["prices"]])
        c.executemany("INSERT INTO tracked_departures_by_airport(hotel_id,airport_code,departure_date,last_seen_at,missing_checks,status,current_price,previous_price) VALUES(?,?,?,?,?,?,?,?)",
                      [(d["hotel_id"],d.get("airport_code","DEFAULT"),d["departure_date"],d.get("last_seen_at"),d.get("missing_checks",0),d.get("status","available"),d.get("current_price"),d.get("previous_price")) for d in state.get("departures",[])])
        c.execute("INSERT OR IGNORE INTO tracked_departures(hotel_id,departure_date,last_seen_at,status) SELECT hotel_id,departure_date,MAX(checked_at),'available' FROM prices WHERE departure_date IS NOT NULL GROUP BY hotel_id,departure_date")
        c.execute("UPDATE tracked_departures SET current_price=(SELECT CASE WHEN p.per_person=1 THEN p.price ELSE p.price/h.people_count END FROM prices p JOIN hotels h ON h.id=p.hotel_id WHERE p.hotel_id=tracked_departures.hotel_id AND p.departure_date=tracked_departures.departure_date ORDER BY p.id DESC LIMIT 1) WHERE current_price IS NULL")
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return True

def trigger_price_check():
    github_state.dispatch_price_check()
def auth_required(fn):
    @wraps(fn)
    def inner(*args,**kwargs):
        if APP_PASSWORD and not session.get("ok"):
            if request.path.startswith("/api/"): return jsonify(error="Zaloguj się ponownie."),401
            return redirect("/login")
        return fn(*args,**kwargs)
    return inner

LOGIN='''<!doctype html><html lang="pl"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Logowanie · Ceny hoteli</title><style>*{box-sizing:border-box}body{margin:0;background:#eff4f3;font:16px system-ui;color:#173d3c;display:grid;min-height:100vh;place-items:center}.box{background:#fff;padding:32px;border-radius:20px;width:min(90%,390px);box-shadow:0 14px 45px #173d3c16}h1{font-size:23px}input,button{width:100%;padding:13px;border-radius:10px;border:1px solid #ccd9d6;font:inherit;margin-top:12px}button{background:#137b70;color:white;border:0;font-weight:700} .comparison-insight{grid-column:1/-1}.comparison-layout{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;margin-top:10px}.comparison-terms{display:grid;gap:7px}.comparison-term{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding-bottom:7px;border-bottom:1px solid var(--line)}.comparison-term:last-child{padding:0;border:0}.comparison-term span{color:var(--ink);font-size:14px}.comparison-term strong{color:#08786e;font-size:15px;margin:0}.comparison-difference{min-width:118px;text-align:center;border-radius:10px;background:#eef8f5;padding:10px}.comparison-difference span,.comparison-difference small{display:block;color:var(--muted);font-size:11px}.comparison-difference strong{display:block;color:#08786e;font-size:15px;margin:5px 0 2px}.comparison-empty{margin-top:9px;color:var(--muted);font-size:13px}</style><form class="box" method="post"><h1>Monitor cen hoteli</h1><p>Wpisz hasło aplikacji.</p><input name="password" type="password" placeholder="Hasło" required autofocus><button>Zaloguj</button>{% if error %}<p style="color:#b42318">{{error}}</p>{% endif %}</form></html>'''
@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        if request.form.get("password")==APP_PASSWORD: session["ok"]=True; return redirect("/")
        return render_template_string(LOGIN,error="Nieprawidłowe hasło.")
    return render_template_string(LOGIN,error="")
@app.get("/logout")
def logout(): session.clear(); return redirect("/login")

PAGE='''<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#123f50"><title>Ceny hoteli</title><style>
:root{--ink:#153a43;--muted:#75858c;--teal:#138274;--bg:#f2f6f5;--line:#e6eceb}*{box-sizing:border-box}body{margin:0;background:#f7f5f0;color:var(--ink);font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{position:relative;isolation:isolate;overflow:hidden;text-align:center;color:#fff;min-height:190px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:calc(26px + env(safe-area-inset-top)) 18px 34px;background:#087b91 url("/assets/background.webp") center 52%/cover no-repeat}header:after{content:"";position:absolute;z-index:-1;left:-5%;right:-5%;height:44px;bottom:-22px;border-radius:50% 50% 0 0/100% 100% 0 0;background:#f7f5f0;opacity:.96}header h1{margin:0 0 4px;line-height:0}header h1 img{display:block;width:min(360px,78vw);height:auto;max-height:96px;object-fit:contain}header p{margin:5px 0 0;color:#fff;font-size:14px;text-shadow:0 1px 5px #073746aa}.wrap{max-width:1300px;padding:18px 22px;margin:-20px auto 0;position:relative}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:14px}button{border:0;border-radius:11px;padding:12px 15px;font:600 14px inherit;cursor:pointer}.primary{background:var(--teal);color:white}.secondary{background:#fff;color:var(--ink);box-shadow:0 1px 3px #102f3612}.danger{color:#a53943;background:transparent}.history-btn{background:#eef7fc;color:#0878a6}.offer-btn{background:#148879;color:#fff;box-shadow:0 2px 6px #12827426}.delete-btn{background:#fff0ef;color:#c83232}.terms-btn{background:#f1f7f7;color:#226b72}.departure-choice{background:#fff;border:1px solid var(--line);color:var(--ink);padding:9px 12px;border-radius:10px}.departures{display:none;grid-column:1/-1;padding:12px 0 2px;gap:8px;flex-wrap:wrap}.departures.show{display:flex}.departure-choice.active{border-color:#148879;background:#eef8f5;color:#08786e;font-weight:700}.card{background:white;border:1px solid var(--line);border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 4px 14px #173d3c08}.card h2{font-size:17px;margin:0 0 12px}.hotel{display:grid;grid-template-columns:82px minmax(0,1fr);column-gap:14px;align-items:center;padding:14px 0;border-top:1px solid var(--line);cursor:pointer}.hotel:first-of-type{border:0}.thumb{width:82px;height:82px;object-fit:cover;border-radius:12px;background:#dff0ed;grid-row:1 / span 2}.thumb.missing{display:grid;place-items:center;color:#fff;font-size:25px;background:linear-gradient(140deg,#f3cf8c,#78c4bd 55%,#347f96)}.hotelname{font-weight:700}.price{font-size:21px;font-weight:750;color:#08786e;white-space:nowrap}.pricebox{background:#eef8f5;border-radius:12px;padding:12px 15px;text-align:center}.airport-label{margin-top:4px;color:#176f70}.airport-select{margin-top:7px;max-width:260px;padding:7px 10px;border:1px solid #c9ded9;border-radius:9px;background:#f7fbfa;color:#15344a;font:inherit;font-size:13px;cursor:pointer}.airport-select:focus{outline:2px solid #148477;outline-offset:1px}.meta{color:var(--muted);font-size:12px;margin-top:5px}.rowbuttons{display:flex;gap:8px;align-items:center;margin-top:8px;grid-column:2/-1}.small{padding:8px 10px;font-size:12px}.chartwrap{height:230px}canvas{width:100%;height:100%}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:11px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:600}.note{font-size:12px;color:var(--muted);line-height:1.5}.modal{position:fixed;inset:0;background:#0e293b78;display:none;place-items:center;padding:18px}.modal.show{display:grid}.dialog{background:white;border-radius:18px;padding:22px;width:min(100%,430px)}.dialog h2{margin-top:0}.dialog input{width:100%;padding:12px;border:1px solid #cbd7d5;border-radius:10px;margin:6px 0 12px;font:inherit}.dialog label{font-size:13px;font-weight:650}.dialog .actions{margin:8px 0 0}.empty{color:var(--muted);padding:12px 0}.status{font-size:12px;color:var(--muted);margin-left:auto;align-self:center}@media(min-width:780px){.columns{display:grid;grid-template-columns:1fr 1fr;gap:14px}.columns .card{margin:0}.hotel{grid-template-columns:200px minmax(0,1fr) 270px;column-gap:22px;min-height:130px;padding:15px 0}.thumb{width:200px;height:112px;grid-row:1 / span 2}.hotel .rowbuttons{grid-column:2/-1;margin-top:12px}.chartwrap{height:260px}}
.insight-grid{display:grid;grid-template-columns:1fr;gap:10px}.insight{background:#f7faf9;border:1px solid var(--line);border-radius:12px;padding:14px;min-width:0}.insight span,.insight small{display:block;color:var(--muted);font-size:12px}.insight strong{display:block;margin:8px 0 4px;color:#08786e;font-size:16px;line-height:1.45}.insight.muted{color:var(--muted)}.departure-choice.inactive{background:#f0f1f1;color:#7b8588;border-color:#e2e5e5}.departure-choice.inactive.active{border-color:#8d999c}.term-status{margin-left:9px;font-size:11px;color:#788285}.best-badge{margin-left:9px;border-radius:20px;background:#ddf3e9;color:#08786e;padding:3px 8px;font-size:11px}@media(min-width:780px){.insight-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}</style></head><body><header><h1><img src="/assets/napis.png" alt="Ceny hoteli"></h1><p>Historia ofert ITAKA · {{ "automatycznie co 2 godziny" if auto_check_enabled else "test · odczyt ręczny" }}</p></header><main class="wrap"><div class="actions"><button class="primary" onclick="openAdd()">＋ Dodaj hotel</button><button class="secondary" id="check" onclick="checkNow()">Sprawdź teraz</button><span class="status" id="status">Gotowe</span></div><section class="card"><h2>Monitorowane oferty</h2><div id="hotels"></div></section><section class="card"><h2>Analiza wybranego terminu</h2><div id="insights" class="insight-grid"></div></section><div class="columns"><section class="card"><h2>Wykres ceny za osobę</h2><div class="chartwrap"><canvas id="chart"></canvas></div><div class="meta" id="chartname">Wybierz hotel</div></section><section class="card"><h2>Historia odczytów</h2><div class="tablewrap"><table><thead><tr><th>Data i godzina</th><th>Cena / osoba</th><th>Zmiana / osoba</th></tr></thead><tbody id="history"></tbody></table></div><div class="empty" id="nohistory" hidden>Brak odczytów. Użyj „Sprawdź teraz”.</div></section></div><p class="note">Cena za osobę to łączna cena oferty podzielona przez liczbę podróżnych. Przy dodawaniu hotelu wpisz ich liczbę. Link do ITAKA lub Rainbow powinien zawierać wybrany termin i parametry wyjazdu. Dla Rainbow aplikacja zapamiętuje link do wybranej oferty bez dat urodzenia w parametrach.</p></main>
<div class="modal" id="modal"><form class="dialog" onsubmit="addHotel(event)"><h2>Dodaj ofertę</h2><label>Nazwa hotelu<input name="name" required placeholder="np. Hotel nad morzem"></label><label>Pełny link do oferty ITAKA lub Rainbow<input name="url" type="url" required placeholder="https://www.itaka.pl/... lub https://r.pl/..."></label><label>Liczba podróżnych<input name="people_count" type="number" min="1" max="20" value="2" required></label><div class="actions"><button type="button" class="secondary" onclick="closeAdd()">Anuluj</button><button class="primary">Zapisz hotel</button></div></form></div>
<script>
let hotels=[], selected=null, hist=[], insights=null, selectedDate=null, expandedHotels=new Set(); const $=s=>document.querySelector(s); const money=v=>new Intl.NumberFormat('pl-PL',{style:'currency',currency:'PLN',maximumFractionDigits:2}).format(v); const date=s=>new Date(s+'Z').toLocaleString('pl-PL',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}); const dateOnly=s=>s?new Date(s+'T00:00:00').toLocaleDateString('pl-PL'):'—';
async function api(path,opt={}){let r=await fetch('/api/'+path,{headers:{'Content-Type':'application/json'},...opt});if(r.status===401){location='/login';throw Error('Zaloguj się ponownie.')}let d=await r.json();if(!r.ok)throw Error(d.error||'Wystąpił błąd');return d}
async function load(){try{let d=await api('hotels');hotels=d.hotels;if(!selected||!hotels.some(h=>h.id===selected)){selected=hotels[0]?.id||null;selectedDate=hotels[0]?.offers?.[0]?.date||hotels[0]?.departure_date||null}else{let h=hotels.find(x=>x.id===selected);if(!selectedDate||(!h?.offers?.some(o=>o.date===selectedDate)&&h?.offers?.length))selectedDate=h?.offers?.[0]?.date||h?.departure_date||null}drawHotels();await loadHistory()}catch(e){status(e.message,true)}}
function status(t,bad=false){$('#status').textContent=t;$('#status').style.color=bad?'#b42318':''}
function drawHotels(){let root=$('#hotels');if(!hotels.length){root.innerHTML='<div class="empty">Dodaj hotel, aby rozpocząć monitorowanie.</div>';return}root.innerHTML=hotels.map(h=>{let chosenDate=selected===h.id&&selectedDate?h.offers.find(o=>o.date===selectedDate):null;let shownDate=chosenDate?.date||h.departure_date;let shownPrice=chosenDate?.price??h.price;let shownTotal=shownPrice===null?null:shownPrice*h.people_count;let active=h.offers.filter(o=>o.status==='available');let cheapest=active.length?Math.min(...active.map(o=>o.price)):null;return `<article class="hotel" onclick="selectHotel(${h.id})">${h.image_url?`<img class="thumb" src="${esc(h.image_url)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb missing',textContent:'🏝️'}))">`:`<div class="thumb missing">🏝️</div>`}<div><div class="hotelname">${esc(h.name)}</div><div class="meta">${esc(h.country||'Kraj nieustalony')} · ${h.people_count} os. · Wylot: ${dateOnly(shownDate)} · ${h.checked?`Ostatni odczyt: ${date(h.checked)}`:'Oczekuje na pierwszy odczyt'}</div><div class="meta airport-label">Lotnisko: ${esc(h.airport_name||'Lotnisko z oferty')}</div>${h.airport_options?.length>1?`<select class="airport-select" onclick="event.stopPropagation()" onchange="changeAirport(${h.id},this.value)"><option value="">Zmień lotnisko…</option>${h.airport_options.map(o=>`<option value="${esc(o.code)}" ${o.code===h.airport_code?'selected':''}>${esc(o.name)}</option>`).join('')}</select>`:''}</div><div class="pricebox"><div class="price">${shownPrice===null?'—':money(shownPrice)} / os.</div>${shownTotal===null?'':`<div class="meta">Łącznie: ${money(shownTotal)}</div>`}</div><div class="rowbuttons"><button class="history-btn small" onclick="event.stopPropagation();selectHotel(${h.id},'${shownDate||''}')">Historia</button><button class="offer-btn small" onclick="event.stopPropagation();window.open('${esc(h.url)}','_blank')">Otwórz ofertę</button><button class="delete-btn small" onclick="event.stopPropagation();removeHotel(${h.id})">Usuń</button><button class="terms-btn small" onclick="event.stopPropagation();toggleDates(${h.id})">Terminy i ceny (${h.offers.length})</button></div><div class="departures ${expandedHotels.has(h.id)?'show':''}">${h.offers.length?h.offers.map(o=>{let inactive=o.status!=='available';return `<button class="departure-choice ${inactive?'inactive ':''}${selected===h.id&&selectedDate===o.date?'active':''}" onclick="event.stopPropagation();selectHotel(${h.id},'${o.date}')"><span>${inactive?`<s>Wylot ${dateOnly(o.date)} · ${money(o.price)} / os.</s>`:`Wylot ${dateOnly(o.date)} · ${money(o.price)} / os.`}</span>${o.status==='expired'?'<span class="term-status">Termin minął</span>':o.status==='unavailable'?'<span class="term-status">Oferta niedostępna</span>':''}${!inactive&&o.price===cheapest?'<span class="best-badge">Najtaniej</span>':''}</button>`}).join(''):'<span class="meta">Kalendarz terminów będzie dostępny po odczycie ITAKA.</span>'}</div></article>`}).join('')}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function loadAirportOptions(id,departure){let h=hotels.find(x=>x.id===id);if(!h||h.provider==='rainbow'&&!departure)return;try{let url=`hotels/${id}/airports`;if(departure)url+=`?date=${encodeURIComponent(departure)}`;let d=await api(url);h.airport_options=d.airports||[];if(h.airport_code&&h.airport_code!=='DEFAULT'&&!h.airport_options.some(o=>o.code===h.airport_code))h.airport_options.unshift({code:h.airport_code,name:h.airport_name||'Obecne lotnisko'});drawHotels();if(d.warning)status(d.warning,true)}catch(e){status(e.message,true)}}
async function selectHotel(id,departure){selected=id;selectedDate=departure||hotels.find(h=>h.id===id)?.offers?.[0]?.date||null;drawHotels();await loadHistory();await loadAirportOptions(id,selectedDate)}
async function changeAirport(id,code){if(!code)return;try{let result=await api(`hotels/${id}/airport`,{method:'POST',body:JSON.stringify({airport_code:code,date:selectedDate})});status(result.message||'Zmieniono lotnisko');await load();if(selected===id)await loadHistory()}catch(e){status(e.message,true);await load()}}
function toggleDates(id){expandedHotels.has(id)?expandedHotels.delete(id):expandedHotels.add(id);drawHotels()}
async function loadHistory(){if(!selected){hist=[];insights=null;$('#history').innerHTML='';$('#nohistory').hidden=false;drawInsights();drawChart();return}let airport=hotels.find(h=>h.id===selected)?.airport_code||'DEFAULT';let params=new URLSearchParams();if(selectedDate)params.set('date',selectedDate);params.set('airport',airport);let d=await api(`hotels/${selected}/history?${params.toString()}`);hist=d.history;insights=d.analysis;drawInsights();$('#chartname').textContent=(hotels.find(h=>h.id===selected)?.name||'')+(selectedDate?' · wylot '+dateOnly(selectedDate):' · cena za osobę');$('#history').innerHTML=hist.map((x,i)=>`<tr><td>${date(x.checked_at)}</td><td>${money(x.price)} / os.</td><td>${x.change===null?'—':`${x.change<0?'−':'+'}${money(Math.abs(x.change))} (${x.change_percent===null?'—':`${x.change_percent<0?'−':'+'}${Math.abs(x.change_percent).toFixed(1)}%`})`}</td></tr>`).join('');$('#nohistory').hidden=hist.length>0;drawChart()}
function drawInsights(){let root=$('#insights');if(!root)return;if(!selected||!insights){root.innerHTML='<div class="insight muted">Wybierz hotel i termin, aby zobaczyć analizę.</div>';return}let a=insights;let current=a.current_price===null?'—':money(a.current_price);let change=a.change===null?'Brak poprzedniego odczytu':`${a.change<0?'−':a.change>0?'+':''}${money(Math.abs(a.change))} / os. (${a.change_percent===null?'—':`${a.change_percent<0?'−':a.change_percent>0?'+':''}${Math.abs(a.change_percent).toFixed(1)}%`})`;let comparison='';if(a.cheapest&&a.runner_up){let difference=a.runner_up.price-a.cheapest.price;let diffText=`${difference>0?'+':difference<0?'−':''}${money(Math.abs(difference))}`;comparison=`<div class="comparison-layout"><div class="comparison-terms"><div class="comparison-term"><span>${dateOnly(a.cheapest.date)}</span><strong>${money(a.cheapest.price)} / os.</strong></div><div class="comparison-term"><span>${dateOnly(a.runner_up.date)}</span><strong>${money(a.runner_up.price)} / os.</strong></div></div><div class="comparison-difference"><span>Różnica</span><strong>${diffText}</strong><small>za osobę</small></div></div>`}else{comparison='<div class="comparison-empty">Brak dwóch aktywnych terminów do porównania.</div>'}let min=a.minimum_price===null?'Brak danych':`${money(a.minimum_price)} · ${date(a.minimum_at)}`;root.innerHTML=`<div class="insight"><span>Zmiana od poprzedniego odczytu</span><strong>${change}</strong><small>Aktualnie ${current} / os.</small></div><div class="insight"><span>Minimum historyczne</span><strong>${a.minimum_price===null?'—':money(a.minimum_price)+' / os.'}</strong><small>${a.minimum_at?date(a.minimum_at):'Brak zapisanej historii'}</small></div><div class="insight comparison-insight"><span>Porównanie najtańszych dostępnych terminów</span>${comparison}</div>`}function drawChart(){let c=$('#chart'),dpr=devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;c.width=w*dpr;c.height=h*dpr;let x=c.getContext('2d');x.scale(dpr,dpr);x.clearRect(0,0,w,h);if(!hist.length){x.fillStyle='#819096';x.font='14px system-ui';x.textAlign='center';x.fillText('Wykres pojawi się po pierwszym odczycie',w/2,h/2);return}let a=[...hist].reverse(),v=a.map(p=>p.price),lo=Math.min(...v),hi=Math.max(...v),span=Math.max(hi-lo,1),pad={l:48,r:12,t:14,b:26};for(let i=0;i<4;i++){let y=pad.t+i*(h-pad.t-pad.b)/3;x.strokeStyle='#eaf0ef';x.beginPath();x.moveTo(pad.l,y);x.lineTo(w-pad.r,y);x.stroke();x.fillStyle='#829096';x.font='10px system-ui';x.textAlign='left';x.fillText(money(hi-i*span/3),2,y+3)}x.beginPath();a.forEach((p,i)=>{let px=pad.l+i*(w-pad.l-pad.r)/Math.max(a.length-1,1),py=pad.t+(hi-p.price)/span*(h-pad.t-pad.b);i?x.lineTo(px,py):x.moveTo(px,py)});x.strokeStyle='#148477';x.lineWidth=3;x.stroke();a.forEach((p,i)=>{let px=pad.l+i*(w-pad.l-pad.r)/Math.max(a.length-1,1),py=pad.t+(hi-p.price)/span*(h-pad.t-pad.b);x.fillStyle='#148477';x.beginPath();x.arc(px,py,3,0,7);x.fill()})}
function openAdd(){$('#modal').classList.add('show')}function closeAdd(){$('#modal').classList.remove('show')}
async function addHotel(e){e.preventDefault();let f=new FormData(e.target);try{await api('hotels',{method:'POST',body:JSON.stringify({name:f.get('name'),url:f.get('url'),people_count:Number(f.get('people_count'))})});closeAdd();e.target.reset();await load();status('Hotel dodany')}catch(x){status(x.message,true)}}
async function removeHotel(id){if(!confirm('Usunąć monitor razem z historią cen?'))return;try{await api(`hotels/${id}`,{method:'DELETE'});if(selected===id)selected=null;await load()}catch(e){status(e.message,true)}}
async function checkNow(){let b=$('#check');b.disabled=true;status('Zlecam sprawdzenie w GitHub Actions…');try{let d=await api('check',{method:'POST'});status(d.message);setTimeout(load,20000)}catch(e){status(e.message,true)}finally{b.disabled=false}}
async function loadAllAirportOptions(){const airportDate=h=>h.provider==='rainbow'?(h.offers?.find(o=>o.status==='available')?.date||h.departure_date):null;const rainbow=hotels.filter(h=>h.provider==='rainbow');await Promise.allSettled(rainbow.map(h=>loadAirportOptions(h.id,airportDate(h))));const itaka=hotels.filter(h=>h.provider!=='rainbow');for(let i=0;i<itaka.length;i+=2){await Promise.allSettled(itaka.slice(i,i+2).map(h=>loadAirportOptions(h.id,airportDate(h))))}}
window.addEventListener('resize',drawChart);load().then(loadAllAirportOptions);setInterval(load,60000);
</script></body></html>'''

@app.get("/assets/<path:filename>")
def asset(filename):
    if filename not in {"background.webp", "napis.png"}: return "", 404
    return send_from_directory(APP_DIR, filename)

@app.get("/")
@auth_required
def index(): return render_template_string(PAGE, auto_check_enabled=GITHUB_AUTOMATION_ENABLED)
@app.get("/api/hotels")
@auth_required
def api_hotels():
    try: sync_state_from_github()
    except Exception as e: app.logger.warning("Could not refresh GitHub state: %s",e)
    c=connect()
    rows=c.execute("SELECT id,name,url,people_count,image_url,last_checked_at,country,airport_code,airport_name,airport_options FROM hotels ORDER BY id").fetchall()
    latest=c.execute("SELECT p.* FROM prices p JOIN (SELECT hotel_id,airport_code,departure_date,MAX(id) id FROM prices WHERE departure_date IS NOT NULL GROUP BY hotel_id,airport_code,departure_date) q ON q.id=p.id ORDER BY p.hotel_id,p.airport_code,p.departure_date").fetchall()
    statuses={ (r['hotel_id'],r['airport_code'],r['departure_date']):r for r in c.execute("SELECT hotel_id,airport_code,departure_date,last_seen_at,missing_checks,status,current_price,previous_price FROM tracked_departures_by_airport") }
    c.close()
    today=datetime.utcnow().date().isoformat(); offers={}
    for o in latest:
        hotel=next((h for h in rows if h['id']==o['hotel_id']),None)
        if not hotel: continue
        record=statuses.get((o['hotel_id'],o['airport_code'],o['departure_date']))
        state='expired' if o['departure_date']<today else (record['status'] if record else 'available')
        price=o['price'] if o['per_person'] else o['price']/hotel['people_count']
        offers.setdefault((o['hotel_id'],o['airport_code']),[]).append(dict(date=o['departure_date'],price=price,checked=record['last_seen_at'] if record else o['checked_at'],status=state,missing_checks=record['missing_checks'] if record else 0))
    result=[]
    for h in rows:
        try: airport_options=json.loads(h['airport_options'] or "[]")
        except Exception: airport_options=[]
        if isinstance(airport_options,dict):
            airport_options=[dict(code=h['airport_code'],name=h['airport_name'] or "Lotnisko z oferty")]
        if is_itaka_url(h['url']) and {o.get('code') for o in airport_options}==set(AIRPORT_NAMES):
            # Ignore the legacy all-airports placeholder until ITAKA options are fetched for this offer.
            airport_options=[dict(code=h['airport_code'],name=h['airport_name'] or "Lotnisko z oferty")]
        if not airport_options:
            airport_options=[dict(code=h['airport_code'],name=h['airport_name'] or "Lotnisko z oferty")]
        hotel_offers=offers.get((h['id'],h['airport_code']),[])
        hotel_offers.sort(key=lambda o:(o['status']!='available',o['date'] if o['status']=='available' else o['date']))
        current=next((o for o in hotel_offers if o['status']=='available'),None)
        if current is None and hotel_offers: current=hotel_offers[-1]
        result.append(dict(id=h['id'],name=h['name'],url=h['url'],country=h['country'],airport_code=h['airport_code'],airport_name=h['airport_name'],airport_options=airport_options,provider=('rainbow' if is_rainbow_url(h['url']) else 'itaka'),people_count=h['people_count'],image_url=h['image_url'],price=current['price'] if current else None,total_price=current['price']*h['people_count'] if current else None,checked=h['last_checked_at'],departure_date=current['date'] if current else None,offers=hotel_offers))
    return jsonify(hotels=result)
@app.get("/api/hotels/<int:hid>/history")
@auth_required
def api_history(hid):
    try: sync_state_from_github()
    except Exception as e: app.logger.warning("Could not refresh GitHub state: %s",e)
    departure_date=request.args.get('date')
    airport_code=request.args.get('airport')
    c=connect(); hotel=c.execute("SELECT people_count,airport_code FROM hotels WHERE id=?",(hid,)).fetchone()
    if airport_code is None: airport_code=hotel["airport_code"] if hotel else "DEFAULT"
    rows=c.execute("SELECT price,checked_at,per_person FROM prices WHERE hotel_id=? AND airport_code=? AND (departure_date=? OR (? IS NULL AND departure_date IS NULL)) ORDER BY id DESC",(hid,airport_code,departure_date,departure_date)).fetchall()
    count=hotel['people_count'] if hotel else 1
    history=[dict(price=r['price'] if r['per_person'] else r['price']/count,total_price=r['price'],checked_at=r['checked_at']) for r in rows]
    tracker=c.execute("SELECT last_seen_at,status,current_price,previous_price FROM tracked_departures_by_airport WHERE hotel_id=? AND airport_code=? AND departure_date=?",(hid,airport_code,departure_date)).fetchone() if departure_date else None
    minimum=c.execute("SELECT price,checked_at,per_person FROM prices WHERE hotel_id=? AND airport_code=? AND departure_date=? ORDER BY CASE WHEN per_person=1 THEN price ELSE price/? END,id ASC LIMIT 1",(hid,airport_code,departure_date,count)).fetchone() if departure_date else None
    comparison=[]
    if departure_date:
        comparison=c.execute("SELECT p.departure_date,COALESCE(d.current_price,CASE WHEN p.per_person=1 THEN p.price ELSE p.price/? END) price FROM prices p JOIN (SELECT hotel_id,airport_code,departure_date,MAX(id) id FROM prices WHERE hotel_id=? AND airport_code=? AND departure_date IS NOT NULL GROUP BY hotel_id,airport_code,departure_date) q ON q.id=p.id LEFT JOIN tracked_departures_by_airport d ON d.hotel_id=p.hotel_id AND d.airport_code=p.airport_code AND d.departure_date=p.departure_date WHERE p.hotel_id=? AND p.airport_code=? AND p.departure_date>=date('now') AND COALESCE(d.status,'available')='available' ORDER BY price,p.departure_date LIMIT 2",(count,hid,airport_code,hid,airport_code)).fetchall()
    c.close()
    current_price=tracker['current_price'] if tracker and tracker['current_price'] is not None else (history[0]['price'] if history else None)
    previous_price=tracker['previous_price'] if tracker and tracker['previous_price'] is not None else None
    if tracker and tracker['last_seen_at'] and current_price is not None and (not history or tracker['last_seen_at']>history[0]['checked_at']):
        delta=round(current_price-previous_price,2) if previous_price is not None else None
        history.insert(0,dict(price=current_price,total_price=current_price*count,checked_at=tracker['last_seen_at'],change=delta,change_percent=round(delta/previous_price*100,2) if delta is not None and previous_price else None))
    else:
        delta=round(current_price-previous_price,2) if current_price is not None and previous_price is not None else None
        if history:
            history[0]['change']=delta
            history[0]['change_percent']=round(delta/previous_price*100,2) if delta is not None and previous_price else None
    for i in range(1,len(history)):
        prev=history[i+1]['price'] if i+1<len(history) else None
        change=round(history[i]['price']-prev,2) if prev is not None else None
        history[i]['change']=change
        history[i]['change_percent']=round(change/prev*100,2) if change is not None and prev else None
    minimum_price=(minimum['price'] if minimum['per_person'] else minimum['price']/count) if minimum else None
    minimum_at=minimum['checked_at'] if minimum else None
    best=[dict(date=r['departure_date'],price=r['price']) for r in comparison]
    return jsonify(history=history,analysis=dict(current_price=current_price,change=delta,change_percent=round(delta/previous_price*100,2) if delta is not None and previous_price else None,minimum_price=minimum_price,minimum_at=minimum_at,cheapest=best[0] if best else None,runner_up=best[1] if len(best)>1 else None))
@app.post("/api/hotels")
@auth_required
def api_add():
    d=request.get_json(force=True); name=str(d.get('name','')).strip(); url=str(d.get('url','')).strip()
    try: people_count=int(d.get('people_count',2))
    except (TypeError,ValueError): people_count=0
    if not name or not is_supported_offer_url(url) or not 1<=people_count<=20:return jsonify(error='Podaj nazwę, poprawny link ITAKA lub Rainbow i liczbę podróżnych od 1 do 20.'),400
    if is_rainbow_url(url): url=sanitize_rainbow_url(url)
    code,airport_name=airport_code_from_url(url)
    country=country_from_url(url)
    c=connect(); cur=c.execute("INSERT INTO hotels(name,url,people_count,country,airport_code,airport_name,airport_options) VALUES(?,?,?,?,?,?,?)",(name,url,people_count,country,code,airport_name,json.dumps([dict(code=code,name=airport_name)],ensure_ascii=False))); hotel_id=cur.lastrowid; c.commit(); c.close()
    try: save_state_to_github()
    except Exception as e: app.logger.exception("Could not save added hotel to GitHub") ; return jsonify(error=f"Hotel został dodany lokalnie, ale nie udało się zapisać go w GitHubie: {e}"),503
    return jsonify(id=hotel_id)
def _decode_airport_options(raw):
    try:
        return json.loads(raw or "[]") if isinstance(raw, (str, bytes)) else (raw or [])
    except Exception:
        return []


def refresh_airport_options():
    """Discover airport variants on the GitHub Actions runner and persist them with the shared state."""
    c=connect()
    hotels=c.execute("SELECT id,url,airport_code,airport_name,airport_options FROM hotels ORDER BY id").fetchall()
    horizon=c.execute("SELECT date('now','start of month','+3 months','-1 day')").fetchone()[0]
    c.close()
    now=datetime.utcnow().replace(microsecond=0)
    cutoff=now-timedelta(hours=12)
    checked=errors=0
    for hotel in hotels:
        cached=_decode_airport_options(hotel["airport_options"])
        if is_rainbow_url(hotel["url"]):
            by_date=cached.get("by_date",{}) if isinstance(cached,dict) else {}
            c=connect()
            dates=[row["departure_date"] for row in c.execute(
                "SELECT DISTINCT p.departure_date FROM prices p LEFT JOIN tracked_departures_by_airport d ON d.hotel_id=p.hotel_id AND d.airport_code=p.airport_code AND d.departure_date=p.departure_date WHERE p.hotel_id=? AND p.airport_code=? AND p.departure_date>=date('now') AND p.departure_date<=? AND COALESCE(d.status,'available')='available' ORDER BY p.departure_date",
                (hotel["id"],hotel["airport_code"],horizon)
            )]
            c.close()
            for departure_date in dates:
                entry=by_date.get(departure_date,{})
                updated=entry.get("updated_at","") if isinstance(entry,dict) else ""
                try: fresh=datetime.fromisoformat(updated)>cutoff
                except (TypeError,ValueError): fresh=False
                if fresh and entry.get("airports") and all(item.get("url") for item in entry["airports"]):
                    continue
                try:
                    found=rainbow_airport_variants(hotel["url"],departure_date)["airports"]
                    options=[]
                    for candidate in found:
                        selected=rainbow_airport_variants(hotel["url"],departure_date,candidate["code"])
                        variant_url=sanitize_rainbow_url(selected["url"])
                        if not is_rainbow_url(variant_url):
                            raise ValueError("Rainbow nie zwróciło poprawnego linku wybranego lotu.")
                        options.append(dict(code=candidate["code"],name=candidate["name"],url=variant_url))
                    if not options:
                        raise ValueError("Nie znaleziono lotnisk dla tego terminu.")
                    by_date[departure_date]=dict(updated_at=now.isoformat(),airports=options)
                    checked+=1
                except Exception as exc:
                    errors+=1
                    app.logger.warning("Could not refresh Rainbow airports for hotel %s and %s: %s",hotel["id"],departure_date,exc)
            payload=dict(version=1,by_date=by_date)
            c=connect()
            c.execute("UPDATE hotels SET airport_options=? WHERE id=?",(json.dumps(payload,ensure_ascii=False),hotel["id"]))
            c.commit(); c.close()
        elif is_itaka_url(hotel["url"]):
            updated=cached.get("updated_at","") if isinstance(cached,dict) else ""
            try: fresh=datetime.fromisoformat(updated)>cutoff
            except (TypeError,ValueError): fresh=False
            if fresh and cached.get("airports"):
                continue
            try:
                options=itaka_airport_options(hotel["url"])
                payload=dict(version=1,updated_at=now.isoformat(),airports=options)
                c=connect()
                c.execute("UPDATE hotels SET airport_options=? WHERE id=?",(json.dumps(payload,ensure_ascii=False),hotel["id"]))
                c.commit(); c.close()
                checked+=1
            except Exception as exc:
                errors+=1
                app.logger.warning("Could not refresh ITAKA airports for hotel %s: %s",hotel["id"],exc)
    return checked,errors


@app.get("/api/hotels/<int:hid>/airports")
@auth_required
def api_airports(hid):
    try: sync_state_from_github()
    except Exception as exc: app.logger.warning("Could not refresh GitHub state before airport lookup: %s",exc)
    c=connect()
    hotel=c.execute("SELECT url,airport_code,airport_name,airport_options FROM hotels WHERE id=?",(hid,)).fetchone()
    c.close()
    if not hotel: return jsonify(error="Nie znaleziono hotelu."),404
    departure_date=request.args.get("date")
    cached=_decode_airport_options(hotel["airport_options"])
    if is_rainbow_url(hotel["url"]):
        by_date=cached.get("by_date",{}) if isinstance(cached,dict) else {}
        entry=by_date.get(departure_date,{}) if departure_date else {}
        airports=entry.get("airports",[]) if isinstance(entry,dict) else []
        airports=[dict(code=item.get("code"),name=item.get("name")) for item in airports if item.get("code")]
        warning=None if airports else "GitHub Actions nie zapisał jeszcze lotnisk dla tego terminu. Spróbuj ponownie po zakończeniu sprawdzania."
    else:
        airports=cached.get("airports",[]) if isinstance(cached,dict) else cached
        airports=[dict(code=item.get("code"),name=item.get("name")) for item in airports if item.get("code")]
        warning=None if airports else "GitHub Actions nie zapisał jeszcze listy dostępnych lotnisk."
    current_code=hotel["airport_code"]
    if current_code and current_code!="DEFAULT" and not any(item["code"]==current_code for item in airports):
        airports.insert(0,dict(code=current_code,name=hotel["airport_name"] or "Obecne lotnisko"))
    if not airports:
        airports=[dict(code=current_code,name=hotel["airport_name"] or "Lotnisko z oferty")]
    if len(airports)<=1 and warning is None:
        warning="Dla tego terminu nie znaleziono innych dostępnych lotnisk."
    return jsonify(airports=airports,warning=warning)


@app.post("/api/hotels/<int:hid>/airport")
@auth_required
def api_set_airport(hid):
    try: sync_state_from_github()
    except Exception as exc: app.logger.warning("Could not refresh GitHub state before airport selection: %s",exc)
    data=request.get_json(force=True); code=str(data.get("airport_code","")).upper()
    departure_date=data.get("date")
    c=connect()
    hotel=c.execute("SELECT url,airport_options FROM hotels WHERE id=?",(hid,)).fetchone()
    if not hotel: c.close(); return jsonify(error="Nie znaleziono hotelu."),404
    cached=_decode_airport_options(hotel["airport_options"])
    if is_rainbow_url(hotel["url"]):
        if not departure_date: c.close(); return jsonify(error="Wybierz termin wylotu przed zmianą lotniska."),400
        by_date=cached.get("by_date",{}) if isinstance(cached,dict) else {}
        entry=by_date.get(departure_date,{})
        option=next((item for item in entry.get("airports",[]) if item.get("code")==code),None) if isinstance(entry,dict) else None
        if not option or not option.get("url"):
            c.close(); return jsonify(error="Brak zapisanej opcji tego lotniska. Poczekaj na GitHub Actions i odśwież listę."),409
        new_url=sanitize_rainbow_url(option["url"])
        if not is_rainbow_url(new_url):
            c.close(); return jsonify(error="Nieprawidłowy zapisany link Rainbow."),400
        airport_name=option["name"]
        options=cached
    else:
        available=cached.get("airports",[]) if isinstance(cached,dict) else cached
        option=next((item for item in available if item.get("code")==code),None) if isinstance(available,list) else None
        if not option or code not in AIRPORT_NAMES:
            c.close(); return jsonify(error="To lotnisko nie jest zapisane jako dostępne dla tej oferty."),400
        query=parse_qsl(urlparse(hotel["url"]).query,keep_blank_values=True)
        query=[(key,value) for key,value in query if key!="airports"]
        query.append(("airports",code))
        parsed=urlparse(hotel["url"]); new_url=urlunparse(parsed._replace(query=urlencode(query,doseq=True)))
        airport_name=option.get("name") or AIRPORT_NAMES[code]
        options=cached
    c.execute("UPDATE hotels SET url=?,airport_code=?,airport_name=?,airport_options=? WHERE id=?",
              (new_url,code,airport_name,json.dumps(options,ensure_ascii=False),hid))
    c.commit(); c.close()
    if not GITHUB_AUTOMATION_ENABLED:
        return jsonify(error="Brak GITHUB_TOKEN. Odczyt i wybór lotniska są obsługiwane przez GitHub Actions."),503
    try:
        save_state_to_github()
        trigger_price_check()
    except Exception as exc:
        app.logger.warning("Could not persist/check selected airport: %s",exc)
        return jsonify(error=f"Zapisano wybór lotniska, ale nie udało się zlecić odczytu GitHub Actions: {exc}"),503
    return jsonify(ok=True,message="Lotnisko zmienione. GitHub Actions pobiera teraz ceny.")


@app.delete("/api/hotels/<int:hid>")
@auth_required
def api_remove(hid):
    c=connect(); c.execute("DELETE FROM prices WHERE hotel_id=?",(hid,)); c.execute("DELETE FROM tracked_departures_by_airport WHERE hotel_id=?",(hid,)); c.execute("DELETE FROM tracked_departures WHERE hotel_id=?",(hid,)); c.execute("DELETE FROM hotels WHERE id=?",(hid,)); c.commit(); c.close()
    try: save_state_to_github()
    except Exception as e: app.logger.exception("Could not save hotel deletion to GitHub"); return jsonify(error=f"Nie udało się zapisać zmiany w GitHubie: {e}"),503
    return jsonify(ok=True)
def run_checks():
    c=connect(); hs=c.execute("SELECT id,url,people_count,airport_code,airport_name,country FROM hotels ORDER BY id").fetchall(); max_departure=c.execute("SELECT date('now','start of month','+3 months','-1 day')").fetchone()[0]; c.close(); errors=0
    for h in hs:
        try:
            result=fetch_offer(h['url'],h['people_count']);
            if len(result)==7: total,image_url,departure_date,calendar_prices,calendar_observed,airport_code,airport_name=result
            else: total,image_url,departure_date,calendar_prices,calendar_observed=result; airport_code,airport_name=airport_code_from_url(h['url'])
            stamp=datetime.utcnow().replace(microsecond=0).isoformat()
            calendar_prices=[(d,p) for d,p in dict(calendar_prices).items() if datetime.strptime(d,"%Y-%m-%d").date()<=datetime.strptime(max_departure,"%Y-%m-%d").date()]
            if departure_date and departure_date<=max_departure and total is not None:
                calendar_prices=[(d,total/h['people_count'] if d==departure_date else value) for d,value in calendar_prices]
                if not any(d==departure_date for d,_ in calendar_prices): calendar_prices.append((departure_date,total/h['people_count']))
            elif not calendar_prices and total is None and not calendar_observed:
                continue
            c=connect()
            if is_rainbow_url(h['url']) and h['airport_code']=="DEFAULT" and airport_code!="DEFAULT":
                c.execute("UPDATE prices SET airport_code=? WHERE hotel_id=? AND airport_code='DEFAULT'",(airport_code,h['id']))
                c.execute("INSERT OR IGNORE INTO tracked_departures_by_airport(hotel_id,airport_code,departure_date,last_seen_at,missing_checks,status,current_price,previous_price) SELECT hotel_id,?,departure_date,last_seen_at,missing_checks,status,current_price,previous_price FROM tracked_departures_by_airport WHERE hotel_id=? AND airport_code='DEFAULT'",(airport_code,h['id']))
                c.execute("DELETE FROM tracked_departures_by_airport WHERE hotel_id=? AND airport_code='DEFAULT'",(h['id'],))
            if calendar_prices:
                for date_value,person_price in calendar_prices:
                    previous=c.execute("SELECT price,per_person FROM prices WHERE hotel_id=? AND airport_code=? AND departure_date=? ORDER BY id DESC LIMIT 1",(h['id'],airport_code,date_value)).fetchone()
                    old_price=(previous['price'] if previous['per_person'] else previous['price']/h['people_count']) if previous else None
                    if old_price is None or abs(old_price-person_price)>=0.01:
                        c.execute("INSERT INTO prices(hotel_id,checked_at,price,departure_date,per_person,airport_code) VALUES(?,?,?,?,1,?)",(h['id'],stamp,person_price,date_value,airport_code))
            elif total is not None:
                previous=c.execute("SELECT price FROM prices WHERE hotel_id=? AND airport_code=? AND departure_date IS ? ORDER BY id DESC LIMIT 1",(h['id'],airport_code,departure_date)).fetchone()
                if previous is None or abs(previous['price']-total)>=0.01:
                    c.execute("INSERT INTO prices(hotel_id,checked_at,price,departure_date,per_person,airport_code) VALUES(?,?,?,?,0,?)",(h['id'],stamp,total,departure_date,airport_code))
            if calendar_observed:
                seen_dates={date_value for date_value,_ in calendar_prices}
                for date_value in seen_dates:
                    c.execute("INSERT INTO tracked_departures_by_airport(hotel_id,airport_code,departure_date,last_seen_at,missing_checks,status,current_price,previous_price) VALUES(?,?,?, ?,0,'available',?,NULL) ON CONFLICT(hotel_id,airport_code,departure_date) DO UPDATE SET last_seen_at=excluded.last_seen_at,missing_checks=0,status='available',previous_price=tracked_departures_by_airport.current_price,current_price=excluded.current_price",(h['id'],airport_code,date_value,stamp,dict(calendar_prices)[date_value]))
                known=c.execute("SELECT departure_date FROM tracked_departures_by_airport WHERE hotel_id=? AND airport_code=? AND departure_date>=date('now') AND departure_date<=?",(h['id'],airport_code,max_departure)).fetchall()
                for record in known:
                    if record['departure_date'] not in seen_dates:
                        c.execute("UPDATE tracked_departures_by_airport SET missing_checks=missing_checks+1,status=CASE WHEN missing_checks+1>=2 THEN 'unavailable' ELSE status END WHERE hotel_id=? AND airport_code=? AND departure_date=?",(h['id'],airport_code,record['departure_date']))
            c.execute("UPDATE hotels SET image_url=COALESCE(?,image_url),last_checked_at=?,country=?,airport_code=?,airport_name=? WHERE id=?",(image_url,stamp,country_from_url(h['url']),airport_code,airport_name,h['id']))
            c.commit(); c.close()
        except Exception as e:
            errors+=1; app.logger.warning("Price check failed for hotel %s: %s",h['id'],e)
    return len(hs),errors
@app.post("/api/check")
@auth_required
def api_check():
    if not GITHUB_AUTOMATION_ENABLED:
        return jsonify(error="Brak GITHUB_TOKEN. Sprawdzanie cen działa wyłącznie przez GitHub Actions."),503
    try: trigger_price_check()
    except Exception as e: return jsonify(error=f"Nie udało się uruchomić GitHub Actions: {e}"),503
    return jsonify(message="Sprawdzenie zlecone do GitHub Actions. Wyniki pojawią się po zakończeniu odczytu.")
@app.get("/health")
def health(): return "ok",200

def scheduler():
    time.sleep(15)
    while True:
        try: run_checks()
        except Exception: app.logger.exception("Scheduled check failed")
        time.sleep(INTERVAL_SECONDS)

if AUTO_SCHEDULER: threading.Thread(target=scheduler,daemon=True,name="price-scheduler").start()





