#!/usr/bin/env python3
"""
cinema_watcher.py
------------------
Sleduje premietania IMAX 70mm v Cinema City Flora (Praha) cez verejné JSON API
cinemacity.cz (rovnaké API, cez ktoré funguje aj samotná webová rezervácia).

Čo robí:
  1. Zistí, na ktoré dni sú naplánované 70mm/IMAX premietania.
  2. Pre každý deň si stiahne zoznam premietaní (film, čas, sála, obsadenosť).
  3. Porovná to s uloženým stavom z minulého behu (state.json) a nahlási:
       - NOVÝ TERMÍN  -> premietanie, ktoré tam predtým nebolo
       - VOĽNÉ MIESTA -> premietanie, ktoré bolo vypredané a teraz už nie je
         (typicky keď kino pridá kapacitu alebo niekto vráti lístky)
       - PRIBUDLI VOĽNÉ MIESTA -> stúpol počet voľných miest (vrátené lístky)
       - ZRUŠENÝ TERMÍN -> premietanie, ktoré zmizlo z rozpisu

Počet voľných miest:
  API nevracia počet miest priamo, len `availabilityRatio`. Napriek názvu je to
  podiel VOĽNÝCH miest (overené: premietanie bez jediného predaného lístka má 1.0).
  Hodnoty sú vždy presné násobky 1/kapacita sály, takže sa z nich dá spätne
  dopočítať počet voľných miest — pozri DEFAULT_CAPACITY / CINEMA_CAPACITY.

  Konkrétne RADY A SEDADLÁ verejné API nevracia. Sú dostupné až v rezervačnom
  systéme (tickets.rel.cinemacity.cz), ktorý je za Cloudflare ochranou a vracia
  403 na požiadavky mimo prehliadača — bez ovládania reálneho prehliadača sa
  k mape sedadiel dostať nedá.

  Z rovnakého dôvodu sa "Prostor pro invalidní vozík" nedá rozpoznať automaticky.
  Počet týchto miest v sále sa zadáva ručne cez CINEMA_WHEELCHAIR a odpočíta sa
  od voľných miest, takže hlásenia chodia len na reálne rezervovateľné sedadlá:
      CINEMA_WHEELCHAIR='{"IMAX VOLVO": 4}'

Ako spustiť jednorazovo:
    python3 cinema_watcher.py

Ako sledovať priebežne (napr. každých 10 minút):
    python3 cinema_watcher.py --watch --interval 600

E-mail pri zmene:
  Nastav SMTP premenné (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO) a
  e-mail sa pošle len vtedy, keď sa dá reálne niečo kúpiť — nový termín alebo
  uvoľnené miesta. Zrušené termíny samé o sebe e-mail nespustia (--mail-on all
  to zmení, --mail-on never e-maily vypne). Prvý beh e-mail neposiela, len si
  založí stav (--mail-first-run to prepíše). Overenie nastavenia:
      python3 cinema_watcher.py --test-email

Výstup pre webovú stránku:
    python3 cinema_watcher.py --json-out web/data/status.json
  Uloží aktuálny rozpis, súčty a históriu hlásení do JSONu, z ktorého číta
  stránka vo `web/` (nasadenie na Netlify — pozri README.md).

Beh v GitHub Actions:
  Workflow .github/workflows/cinema-watcher.yml spúšťa kontrolu podľa cronu,
  posiela e-maily a stav aj JSON pre web commitne späť do repa (stav musí
  prežiť medzi behmi, inak by každý beh vyzeral ako prvý). Čas spustenia sa
  mení v tom súbore, prihlasovacie údaje k e-mailu idú do GitHub Secrets.

Logovanie:
  - Všetko ide naraz do konzoly aj do log súboru (default cinema_watcher.log
    vedľa skriptu, rotuje po 1 MB, drží 5 starých kópií).
  - `--log-level DEBUG` pridá detaily: každý stiahnutý event, obsadenosť,
    orezaný náhľad odpovede z API.
  - `--dump-raw ADRESÁR` uloží surové JSON odpovede z API do súborov —
    keď potrebuješ vidieť presne to, čo server vrátil.

Cez systémový cron (namiesto --watch):
    */10 * * * * /usr/bin/python3 /cesta/k/cinema_watcher.py

Konfigurácia nižšie (CINEMA_ID, ATTR, FILM_NAME_FILTER) sa dá zmeniť aj bez
zásahu do kódu cez premenné prostredia (pozri sekciu CONFIG).
"""

import json
import os
import re
import sys
import time
import smtplib
import logging
import argparse
import urllib.request
import urllib.error
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
CINEMA_ID = os.environ.get("CINEMA_ID", "1052")          # 1052 = Praha Flora
ATTR = os.environ.get("CINEMA_ATTR", "70-mm")             # filter atribútu (70mm/IMAX film)
LANG = os.environ.get("CINEMA_LANG", "cs_CZ")
DAYS_AHEAD = int(os.environ.get("CINEMA_DAYS_AHEAD", "60"))
# Voliteľný filter podľa názvu filmu (case-insensitive substring).
# Predvolene sledujeme len Odyseu — pozor, API vracia český názov ("Odyssea"),
# preto "odys", ktoré sadne na Odyssea aj Odyssey.
# Zmeníš cez premennú prostredia CINEMA_FILM_FILTER, alebo daj ""
# pre sledovanie všetkých 70mm/IMAX premietaní.
FILM_NAME_FILTER = os.environ.get("CINEMA_FILM_FILTER", "odys").strip().lower()

BASE = f"https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/10101"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get(
    "CINEMA_STATE_FILE",
    os.path.join(SCRIPT_DIR, "cinema_watcher_state.json"),
)
LOG_FILE = os.environ.get("CINEMA_LOG_FILE", os.path.join(SCRIPT_DIR, "cinema_watcher.log"))
LOG_LEVEL = os.environ.get("CINEMA_LOG_LEVEL", "INFO")
DUMP_DIR = os.environ.get("CINEMA_DUMP_DIR", "")

HTTP_TIMEOUT = int(os.environ.get("CINEMA_HTTP_TIMEOUT", "15"))
HTTP_ATTEMPTS = int(os.environ.get("CINEMA_HTTP_ATTEMPTS", "3"))

# Kam uložiť JSON pre webovú stránku (prázdne = neukladať).
JSON_OUT = os.environ.get("CINEMA_JSON_OUT", "")
# Koľko posledných hlásení si držať v histórii v tom JSONe.
JSON_HISTORY = int(os.environ.get("CINEMA_JSON_HISTORY", "50"))

# E-mail (SMTP). Bez SMTP_HOST/MAIL_TO sa e-maily jednoducho neposielajú.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
# "starttls" (port 587), "ssl" (port 465) alebo "none" (bez šifrovania).
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
MAIL_FROM = os.environ.get("MAIL_FROM", "") or SMTP_USER
MAIL_TO = os.environ.get("MAIL_TO", "")  # viac adries oddeľ čiarkou
MAIL_SUBJECT_PREFIX = os.environ.get("MAIL_SUBJECT_PREFIX", "[Cinema City]")
# Kedy posielať e-mail:
#   tickets = len keď sa dá niečo kúpiť (nový termín / uvoľnené miesta) — default
#   all     = pri akejkoľvek zmene (aj zrušený termín)
#   never   = nikdy
MAIL_ON = os.environ.get("CINEMA_MAIL_ON", "tickets").strip().lower()

# Typy zmien, na ktoré sa dá reálne kúpiť lístok.
TICKET_KINDS = ("new", "freed", "more")

# Kapacity sál — potrebné na prepočet availabilityRatio na počet voľných miest.
# API kapacitu priamo nevracia, ale availabilityRatio je vždy presný násobok
# 1/kapacita (orezaný na 4 desatinné miesta), takže sa dá odvodiť:
# pre IMAX VOLVO sedí 384 (odchýlka < 0,04 miesta na všetkých pozorovaných hodnotách).
# Prepísať/doplniť sa dá cez CINEMA_CAPACITY, napr.: {"IMAX VOLVO": 384, "Sál 5": 120}
DEFAULT_CAPACITY = {"IMAX VOLVO": 384}
CAPACITIES = dict(DEFAULT_CAPACITY)

# Počet miest pre invalidný vozík ("Prostor pro invalidní vozík") v sále.
# Tieto miesta sú síce v kapacite, ale bežne sa nepredávajú a pre normálnu
# rezerváciu sú nepoužiteľné — preto sa odpočítavajú od počtu voľných miest.
# API zloženie sedadiel nevracia (mapa sedadiel je len v rezervačnom systéme
# za Cloudflare), takže sa to musí nastaviť ručne cez CINEMA_WHEELCHAIR,
# napr.: CINEMA_WHEELCHAIR='{"IMAX VOLVO": 4}'
DEFAULT_WHEELCHAIR = {"IMAX VOLVO": 6}
WHEELCHAIR = dict(DEFAULT_WHEELCHAIR)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; cinema-watcher/1.0)",
    "Accept": "application/json",
}

log = logging.getLogger("cinema_watcher")


# ---------------------------------------------------------------------------
# Logovanie
# ---------------------------------------------------------------------------
def setup_logging(log_file: str, level: str):
    """Nastaví logovanie do konzoly aj do rotujúceho súboru."""
    # Emoji v hláškach potrebujú UTF-8; Windows konzola má default cp125x.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    log.setLevel(numeric_level)
    log.handlers.clear()
    log.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    if log_file:
        try:
            file_handler = RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            log.addHandler(file_handler)
        except OSError as e:
            log.warning("Nepodarilo sa otvoriť log súbor %s: %s", log_file, e)


def _load_int_map(env_name: str, target: dict, defaults: dict):
    """Načíta mapu {sála: číslo} z premennej prostredia (JSON) navrch predvolených hodnôt."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return
    try:
        target.update({str(k): int(v) for k, v in json.loads(raw).items()})
        log.debug("%s načítané, výsledok: %s", env_name, target)
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as e:
        target.clear()
        target.update(defaults)
        log.warning("%s sa nedá rozparsovať (%s), používam predvolené: %s", env_name, e, target)


def init_config():
    """Načíta kapacity sál a počty miest pre vozík z premenných prostredia."""
    _load_int_map("CINEMA_CAPACITY", CAPACITIES, DEFAULT_CAPACITY)
    _load_int_map("CINEMA_WHEELCHAIR", WHEELCHAIR, DEFAULT_WHEELCHAIR)


def free_seats_for(auditorium: str, ratio):
    """Prepočíta availabilityRatio na počet voľných miest. None = kapacita sály neznáma."""
    if ratio is None:
        return None
    capacity = CAPACITIES.get(auditorium)
    if not capacity:
        return None
    return round(ratio * capacity)


def bookable_seats(ev: dict):
    """Voľné miesta bez miest pre vozík. Fallback na hrubý počet (staršie state.json)."""
    value = ev.get("free_bookable")
    return ev.get("free_seats") if value is None else value


def dump_raw(url: str, raw: bytes, dump_dir: str):
    """Uloží surovú odpoveď z API do súboru (kvôli neskoršej kontrole)."""
    if not dump_dir:
        return
    try:
        os.makedirs(dump_dir, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("/quickbook/10101/")[-1])[:80]
        path = os.path.join(dump_dir, f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.json")
        with open(path, "wb") as f:
            f.write(raw)
        log.debug("Surová odpoveď uložená do %s", path)
    except OSError as e:
        log.warning("Nepodarilo sa uložiť surovú odpoveď: %s", e)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
class FetchError(Exception):
    """Nepodarilo sa stiahnuť/rozparsovať odpoveď z API."""


def fetch_json(url: str, dump_dir: str = ""):
    """Stiahne JSON z URL. Pri neúspechu skúsi znova, potom vyhodí FetchError."""
    last_error = None

    for attempt in range(1, HTTP_ATTEMPTS + 1):
        started = time.monotonic()
        log.debug("GET %s (pokus %d/%d)", url, attempt, HTTP_ATTEMPTS)
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
                status = resp.getcode()

            elapsed_ms = (time.monotonic() - started) * 1000
            log.info("GET %s -> HTTP %s, %d B, %.0f ms", url, status, len(raw), elapsed_ms)
            dump_raw(url, raw, dump_dir)

            data = json.loads(raw.decode("utf-8"))
            if log.isEnabledFor(logging.DEBUG):
                preview = raw.decode("utf-8", "replace")
                if len(preview) > 1500:
                    preview = preview[:1500] + f"... (orezané, celkovo {len(raw)} B)"
                log.debug("Odpoveď: %s", preview)
            return data

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:  # telo odpovede je len bonus, chybu neprepisuj
                pass
            last_error = f"HTTP {e.code} {e.reason}"
            log.warning("GET %s zlyhalo: %s %s", url, last_error, body)

        except urllib.error.URLError as e:
            last_error = f"sieťová chyba: {e.reason}"
            log.warning("GET %s zlyhalo: %s", url, last_error)

        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            last_error = f"odpoveď sa nedá rozparsovať ako JSON: {e}"
            log.warning("GET %s zlyhalo: %s", url, last_error)

        except OSError as e:  # timeout a spol.
            last_error = f"{type(e).__name__}: {e}"
            log.warning("GET %s zlyhalo: %s", url, last_error)

        if attempt < HTTP_ATTEMPTS:
            backoff = 2 ** (attempt - 1)
            log.debug("Skúšam znova o %ds...", backoff)
            time.sleep(backoff)

    raise FetchError(f"{url}: {last_error}")


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------
def get_dates_with_screenings(dump_dir: str = ""):
    """Vráti zoznam dátumov (YYYY-MM-DD), kedy sa hrá aspoň jedno 70mm/IMAX premietanie."""
    until = (datetime.now() + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")
    url = f"{BASE}/dates/in-cinema/{CINEMA_ID}/until/{until}?attr={ATTR}&lang={LANG}"
    data = fetch_json(url, dump_dir)
    dates = data.get("body", {}).get("dates", [])
    log.info("API vrátilo %d dní s premietaním (do %s): %s", len(dates), until, dates or "—")
    return dates


def get_events_for_date(date_str: str, dump_dir: str = ""):
    """Vráti zoznam premietaní pre daný deň (filtrovaných podľa ATTR)."""
    url = f"{BASE}/film-events/in-cinema/{CINEMA_ID}/at-date/{date_str}?attr={ATTR}&lang={LANG}"
    data = fetch_json(url, dump_dir)
    body = data.get("body", {})
    events = body.get("events", [])
    films = {f["id"]: f for f in body.get("films", [])}
    log.info(
        "%s: %d premietaní, %d filmov (%s)",
        date_str,
        len(events),
        len(films),
        ", ".join(sorted(f.get("name", "?") for f in films.values())) or "—",
    )
    return events, films


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        log.info("Stavový súbor %s zatiaľ neexistuje — beriem to ako prvý beh.", STATE_FILE)
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        log.info("Načítaný predošlý stav z %s (%d premietaní).", STATE_FILE, len(state))
        return state
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Stavový súbor %s sa nedá načítať (%s) — začínam odznova.", STATE_FILE, e)
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log.info("Stav uložený do %s (%d premietaní).", STATE_FILE, len(state))
    except OSError as e:
        log.error("Stav sa nepodarilo uložiť do %s: %s", STATE_FILE, e)


# ---------------------------------------------------------------------------
# E-mail
# ---------------------------------------------------------------------------
def mail_recipients():
    return [a.strip() for a in MAIL_TO.split(",") if a.strip()]


def mail_configured():
    """True, ak sú nastavené aspoň server a adresát."""
    return bool(SMTP_HOST and mail_recipients())


def changes_to_notify(changes, mail_on: str = None):
    """Vyberie zmeny, kvôli ktorým sa má poslať e-mail."""
    mode = (mail_on or MAIL_ON).strip().lower()
    if mode == "never" or not changes:
        return []
    if mode == "all":
        return list(changes)
    # default "tickets": len to, na čo sa dá kúpiť lístok
    return [c for c in changes if c["kind"] in TICKET_KINDS]


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Slovenské tvary: 1 zmena / 2–4 zmeny / 5+ zmien."""
    if n == 1:
        return one
    return few if 2 <= n <= 4 else many


def build_email(relevant, all_changes):
    """Zloží subject + textovú a HTML verziu tela e-mailu."""
    from html import escape

    films = sorted({c["film"] for c in relevant if c.get("film")})
    kinds = {c["kind"] for c in relevant}
    count = len(relevant)
    if kinds == {"new"}:
        what = (
            "nový termín" if count == 1
            else f"{count} {_plural(count, '', 'nové termíny', 'nových termínov')}"
        )
    elif not kinds - {"freed", "more"}:
        what = "uvoľnené miesta"
    else:
        what = f"{count} {_plural(count, 'zmena', 'zmeny', 'zmien')}"

    emoji = "🎟️" if kinds & set(TICKET_KINDS) else "🔔"
    subject = " ".join(filter(None, [
        MAIL_SUBJECT_PREFIX,
        f"{emoji} {what}" + (f" — {', '.join(films)}" if films else ""),
    ]))

    # Zrušené termíny nie sú dôvod na e-mail, ale keď už mail ide, patria doň.
    extra = [c for c in all_changes if c not in relevant]

    text_lines = [c["text"] for c in relevant]
    if extra:
        text_lines += ["", "Ostatné zmeny:"] + [c["text"] for c in extra]
    text_lines += ["", f"Kontrola: {datetime.now():%Y-%m-%d %H:%M:%S} (lokálny čas bežiaceho stroja)"]

    def as_html(items):
        rows = []
        for c in items:
            label = escape(c["text"].split(":", 1)[0])
            body = escape(f"{c['film']} — {c['date']} {c['time']} · sála {c['auditorium']}")
            seats = escape(c.get("seats") or "")
            link = c.get("booking_link")
            button = (
                f'<a href="{escape(link)}" style="display:inline-block;margin-top:6px;'
                f'padding:8px 14px;background:#c8102e;color:#fff;text-decoration:none;'
                f'border-radius:6px;font-weight:600">Rezervovať</a>'
                if link else ""
            )
            rows.append(
                '<li style="margin:0 0 18px">'
                f'<div style="font-weight:700">{label}</div>'
                f'<div>{body}</div>'
                f'<div style="color:#555">{seats}</div>'
                f"{button}</li>"
            )
        return "\n".join(rows)

    html_body = (
        '<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:15px;'
        'line-height:1.5;color:#111">'
        f"<h2 style=\"margin:0 0 16px\">{escape(what.capitalize())}</h2>"
        f'<ul style="list-style:none;padding:0;margin:0">{as_html(relevant)}</ul>'
        + (
            '<h3 style="margin:24px 0 8px;font-size:15px;color:#555">Ostatné zmeny</h3>'
            f'<ul style="list-style:none;padding:0;margin:0">{as_html(extra)}</ul>'
            if extra else ""
        )
        + f'<p style="color:#888;font-size:12px;margin-top:24px">cinema_watcher · '
          f'{escape(f"{datetime.now():%Y-%m-%d %H:%M:%S}")}</p></div>'
    )

    return subject, "\n".join(text_lines), html_body


def send_email(subject: str, text_body: str, html_body: str = ""):
    """Pošle e-mail cez SMTP. Vráti True pri úspechu."""
    recipients = mail_recipients()
    if not SMTP_HOST or not recipients:
        log.info("E-mail nie je nakonfigurovaný (SMTP_HOST / MAIL_TO) — neposielam.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM or SMTP_USER or recipients[0]
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        if SMTP_SECURITY == "ssl":
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT)
        with server:
            if SMTP_SECURITY == "starttls":
                server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log.error("E-mail sa nepodarilo poslať (%s): %s", type(e).__name__, e)
        return False

    log.info("E-mail odoslaný na %s: %s", ", ".join(recipients), subject)
    return True


def notify_by_email(changes, mail_on: str = None):
    """Pošle e-mail, ak medzi zmenami je niečo, na čo sa dá kúpiť lístok."""
    relevant = changes_to_notify(changes, mail_on)
    if not relevant:
        if changes:
            log.info("Zmeny sú, ale žiadne kúpiteľné miesta — e-mail neposielam.")
        return False
    subject, text_body, html_body = build_email(relevant, changes)
    return send_email(subject, text_body, html_body)


# ---------------------------------------------------------------------------
# JSON výstup pre webovú stránku
# ---------------------------------------------------------------------------
def write_json_out(path: str, current: dict, changes: list, ok: bool = True, error: str = ""):
    """Uloží aktuálny stav + históriu hlásení do JSONu, z ktorého číta web.

    `current=None` znamená, že kontrola zlyhala: zoznam premietaní sa zachová
    z predošlého behu (inak by web ukázal prázdno, akoby sa nič nehralo)
    a do stránky sa dostane len príznak chyby.
    """
    if not path:
        return

    now = datetime.now(timezone.utc).replace(microsecond=0)

    previous_doc = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                previous_doc = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Predošlý %s sa nedá načítať (%s) — začínam s prázdnou históriou.", path, e)

    if current is None:
        screenings = previous_doc.get("screenings") or []
        totals = previous_doc.get("totals") or {}
        total_free = totals.get("free_bookable", 0)
        sold_out = totals.get("sold_out", 0)
    else:
        screenings = []
        total_free = 0
        sold_out = 0
        for event_id, ev in sorted(current.items(), key=lambda kv: (kv[1]["date"], kv[1]["time"])):
            free_bookable = bookable_seats(ev)
            if free_bookable:
                total_free += free_bookable
            if ev.get("sold_out"):
                sold_out += 1
            screenings.append({**ev, "event_id": event_id, "seats": describe_seats(ev)})

    # História: najnovšie hlásenia prvé, staršie orezané.
    history = previous_doc.get("history") or []
    if changes:
        history = [{"at": now.isoformat(), "changes": changes}] + history
    history = history[:JSON_HISTORY]

    doc = {
        "generated_at": now.isoformat(),
        "ok": ok,
        "error": error or None,
        "last_ok_at": now.isoformat() if ok else previous_doc.get("last_ok_at"),
        "config": {
            "cinema_id": CINEMA_ID,
            "attr": ATTR,
            "film_filter": FILM_NAME_FILTER,
            "days_ahead": DAYS_AHEAD,
        },
        "totals": {
            "screenings": len(screenings),
            "free_bookable": total_free,
            "sold_out": sold_out,
        },
        "screenings": screenings,
        "changes": changes,
        "last_change_at": (history[0]["at"] if history else None),
        "history": history,
    }

    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        log.info("JSON pre web uložený do %s (%d premietaní).", path, len(screenings))
    except OSError as e:
        log.error("JSON pre web sa nepodarilo uložiť do %s: %s", path, e)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def collect_current_events(dump_dir: str = ""):
    """Stiahne aktuálne premietania zo všetkých relevantných dní."""
    current = {}
    skipped_by_filter = 0
    skipped_no_id = 0
    seen_films = set()
    unknown_halls = set()

    for date_str in get_dates_with_screenings(dump_dir):
        events, films = get_events_for_date(date_str, dump_dir)
        for ev in events:
            film = films.get(ev.get("filmId"), {})
            film_name = film.get("name", "Neznámy film")
            seen_films.add(film_name)

            if FILM_NAME_FILTER and FILM_NAME_FILTER not in film_name.lower():
                skipped_by_filter += 1
                continue

            event_id = ev.get("id")
            if not event_id:
                skipped_no_id += 1
                log.warning("Premietanie bez ID, preskakujem: %s", ev)
                continue

            auditorium = ev.get("auditorium", "?")
            # POZOR: availabilityRatio = podiel VOĽNÝCH miest, nie obsadených.
            # (Overené: premietanie, kde nie je predaný ani jeden lístok, má 1.0.)
            ratio = ev.get("availabilityRatio")

            free_total = free_seats_for(auditorium, ratio)
            wheelchair = WHEELCHAIR.get(auditorium, 0)
            # Predpoklad: miesta pre vozík sú vždy voľné (bežne sa nepredávajú).
            # Ak by predsa boli obsadené, počet bežných miest tu vyjde nižší
            # než v skutočnosti — radšej podhodnotiť ako hlásiť falošné voľné miesto.
            free_bookable = None if free_total is None else max(0, free_total - wheelchair)

            entry = {
                "film": film_name,
                "date": date_str,
                "time": ev.get("eventDateTime", "")[11:16],  # HH:MM z ISO stringu
                "auditorium": auditorium,
                "sold_out": bool(ev.get("soldOut", False)),
                "availability_ratio": ratio,
                "free_seats": free_total,          # vrátane miest pre vozík
                "free_bookable": free_bookable,    # bežné miesta, na tie sa hlási
                "wheelchair_seats": wheelchair,
                "capacity": CAPACITIES.get(auditorium),
                "booking_link": (
                    f"https://www.cinemacity.cz/cz/booking-router/launch/{event_id}?lang=cs"
                ),
            }
            current[event_id] = entry
            log.debug(
                "  %s %s | %s | sála %s | vypredané=%s | ratio=%s | voľných=%s "
                "(z toho vozík %s) | bežných=%s | id=%s",
                entry["date"], entry["time"], entry["film"], entry["auditorium"],
                entry["sold_out"], ratio, free_total, wheelchair, free_bookable, event_id,
            )

            if entry["capacity"] is None and auditorium not in unknown_halls:
                unknown_halls.add(auditorium)
                log.warning(
                    "Neznáma kapacita sály '%s' — počet voľných miest neviem prepočítať. "
                    "Doplň ju cez CINEMA_CAPACITY, napr. CINEMA_CAPACITY='{\"%s\": 384}'",
                    auditorium, auditorium,
                )

    log.info(
        "Spolu %d sledovaných premietaní (filter filmu: %s; odfiltrované: %d; bez ID: %d).",
        len(current),
        f"'{FILM_NAME_FILTER}'" if FILM_NAME_FILTER else "žiadny",
        skipped_by_filter,
        skipped_no_id,
    )

    # Typická pasca: API vracia český názov (napr. "Odyssea"), filter je anglický.
    if not current and seen_films and FILM_NAME_FILTER:
        log.warning(
            "Filter '%s' nesadol na žiadny film. API ponúklo: %s. "
            "Uprav CINEMA_FILM_FILTER (alebo daj prázdny reťazec pre všetky filmy).",
            FILM_NAME_FILTER,
            ", ".join(sorted(seen_films)),
        )

    return current


def describe_seats(ev: dict) -> str:
    """Textový popis obsadenosti jedného premietania."""
    if ev.get("sold_out"):
        return "VYPREDANÉ"

    free = ev.get("free_seats")
    if free is not None:
        wheelchair = ev.get("wheelchair_seats") or 0
        if not wheelchair:
            return f"{free} voľných z {ev.get('capacity')}"
        bookable = bookable_seats(ev)
        if bookable == 0:
            return f"0 bežných voľných (ostali len miesta pre vozík: {free})"
        return f"{bookable} bežných voľných z {ev.get('capacity')} (+{free - bookable} pre vozík)"

    ratio = ev.get("availability_ratio")
    if ratio is not None:
        return f"{ratio:.1%} voľných (kapacita sály neznáma)"
    return "obsadenosť neznáma"


def log_snapshot(current: dict):
    """Vypíše do logu aktuálny stav všetkých sledovaných premietaní."""
    if not current:
        return

    log.info("--- Aktuálny stav (%d premietaní) ---", len(current))
    total_free = 0
    known = 0

    for ev in sorted(current.values(), key=lambda e: (e["date"], e["time"])):
        log.info(
            "  %s %s  %-24s sála %-12s %s",
            ev["date"], ev["time"], ev["film"], ev["auditorium"], describe_seats(ev),
        )
        if bookable_seats(ev) is not None:
            total_free += bookable_seats(ev)
            known += 1

    if known:
        log.info("Spolu voľných bežných miest: %d (v %d premietaniach)", total_free, known)


def change(kind: str, event_id, ev: dict, text: str, **extra):
    """Jedna zmena v štruktúrovanej podobe — kvôli e-mailu a JSONu pre web."""
    item = {
        "kind": kind,                  # new | freed | more | removed
        "text": text,
        "event_id": str(event_id),
        "film": ev.get("film"),
        "date": ev.get("date"),
        "time": ev.get("time"),
        "auditorium": ev.get("auditorium"),
        "seats": describe_seats(ev),
        "free_bookable": bookable_seats(ev),
        "booking_link": ev.get("booking_link"),
    }
    item.update(extra)
    return item


def diff_and_report(previous: dict, current: dict, alert_free: int = 1):
    """Porovná predošlý a aktuálny stav. Vráti zoznam zmien (pozri `change`)."""
    new_ids = set(current) - set(previous)
    removed_ids = set(previous) - set(current)
    common_ids = set(current) & set(previous)

    log.debug(
        "Porovnanie: %d nových, %d zrušených, %d spoločných.",
        len(new_ids), len(removed_ids), len(common_ids),
    )

    changes = []

    for eid in sorted(new_ids, key=lambda i: (current[i]["date"], current[i]["time"])):
        ev = current[eid]
        changes.append(change(
            "new", eid, ev,
            f"🆕 NOVÝ TERMÍN: {ev['film']} — {ev['date']} {ev['time']} "
            f"(sála {ev['auditorium']}, {describe_seats(ev)}) -> {ev['booking_link']}",
        ))

    for eid in sorted(removed_ids, key=lambda i: (previous[i]["date"], previous[i]["time"])):
        ev = previous[eid]
        changes.append(change(
            "removed", eid, ev,
            f"❌ ZRUŠENÝ TERMÍN: {ev['film']} — {ev['date']} {ev['time']} "
            f"(sála {ev['auditorium']})",
        ))

    for eid in sorted(common_ids, key=lambda i: (current[i]["date"], current[i]["time"])):
        old, new = previous[eid], current[eid]
        was_soldout = old["sold_out"]
        is_soldout = new["sold_out"]
        # Hlásime len bežné miesta — miesta pre vozík sa ako "voľné" nerátajú.
        old_free = bookable_seats(old)
        new_free = bookable_seats(new)

        if was_soldout and not is_soldout:
            if new_free == 0:
                # Odblokovali sa len miesta pre vozík — pre bežnú rezerváciu nič.
                log.info(
                    "Už nie je vypredané, ale voľné sú len miesta pre vozík: %s — %s %s (sála %s)",
                    new["film"], new["date"], new["time"], new["auditorium"],
                )
            else:
                changes.append(change(
                    "freed", eid, new,
                    f"🎟️ UVOĽNILI SA MIESTA: {new['film']} — {new['date']} {new['time']} "
                    f"(sála {new['auditorium']}, {describe_seats(new)}) -> {new['booking_link']}",
                ))
            continue

        if not was_soldout and is_soldout:
            log.info(
                "Vypredalo sa: %s — %s %s (sála %s)",
                new["film"], new["date"], new["time"], new["auditorium"],
            )
            continue

        # Pribudli voľné miesta (niekto vrátil lístky / kino uvoľnilo kapacitu).
        if old_free is not None and new_free is not None:
            delta = new_free - old_free
            if delta >= alert_free:
                changes.append(change(
                    "more", eid, new,
                    f"🎟️ PRIBUDLI VOĽNÉ MIESTA (+{delta}): {new['film']} — "
                    f"{new['date']} {new['time']} (sála {new['auditorium']}, "
                    f"{old_free} -> {new_free} voľných) -> {new['booking_link']}",
                    delta=delta,
                    free_before=old_free,
                ))
            elif delta:
                log.info(
                    "Ubudli voľné miesta: %s — %s %s (sála %s): %d -> %d",
                    new["film"], new["date"], new["time"], new["auditorium"],
                    old_free, new_free,
                )
        elif old.get("availability_ratio") != new.get("availability_ratio"):
            log.info(
                "Zmena dostupnosti: %s — %s %s: %s -> %s",
                new["film"], new["date"], new["time"],
                old.get("availability_ratio"), new.get("availability_ratio"),
            )

    return changes


def run_once(
    verbose_no_change=True,
    dump_dir="",
    list_all=True,
    alert_free=1,
    json_out="",
    mail_on=None,
    mail_first_run=False,
):
    """Jeden cyklus kontroly. Vráti True, ak prebehol úspešne."""
    started = time.monotonic()
    log.info("=== Štart kontroly (kino %s, atribút %s) ===", CINEMA_ID, ATTR)

    previous = load_state()

    try:
        current = collect_current_events(dump_dir)
    except FetchError as e:
        # Dôležité: pri chybe API stav NEUKLADÁME. Inak by prázdny výsledok
        # vyzeral ako "všetko zrušené" a po obnovení spojenia zas ako "všetko nové".
        log.error("Kontrola zlyhala, stav ostáva nezmenený: %s", e)
        write_json_out(json_out, None, [], ok=False, error=str(e))
        return False

    if not current and not previous:
        log.warning(
            "API nevrátilo žiadne premietania. Skontroluj CINEMA_ID / ATTR / filter filmu."
        )
        write_json_out(json_out, current, [])
        return True

    if list_all:
        log_snapshot(current)

    changes = diff_and_report(previous, current, alert_free=alert_free)

    if changes:
        log.info("--- ZMENY (%d) ---", len(changes))
        for item in changes:
            log.info(item["text"])
        if previous or mail_first_run:
            notify_by_email(changes, mail_on)
        else:
            # Prvý beh (žiadny predošlý stav) = všetko je "nové". E-mail so
            # všetkými termínmi nikomu nepomôže, tak ho preskočíme a berieme
            # tento beh len ako založenie stavu. Prepínač: --mail-first-run.
            log.info("Prvý beh — e-mail neposielam, len si ukladám stav.")
    elif verbose_no_change:
        log.info("Žiadne zmeny (%d sledovaných premietaní).", len(current))

    write_json_out(json_out, current, changes)
    save_state(current)
    log.info("=== Kontrola hotová za %.1fs ===", time.monotonic() - started)
    return True


def main():
    parser = argparse.ArgumentParser(description="Sledovanie termínov a voľných miest v Cinema City")
    parser.add_argument("--watch", action="store_true", help="Beh v slučke namiesto jedného behu")
    parser.add_argument("--interval", type=int, default=600, help="Interval v sekundách pri --watch (default 600)")
    parser.add_argument("--quiet", action="store_true", help="Nevypisovať hlášku, keď nie sú žiadne zmeny")
    parser.add_argument("--log-file", default=LOG_FILE, help=f"Kam zapisovať log (default {LOG_FILE}); prázdne = len konzola")
    parser.add_argument("--log-level", default=LOG_LEVEL, choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Úroveň logovania (default INFO)")
    parser.add_argument("--dump-raw", default=DUMP_DIR, metavar="ADRESÁR", help="Ukladať surové JSON odpovede z API do tohto adresára")
    parser.add_argument("--no-list", dest="list_all", action="store_false", help="Nevypisovať zoznam všetkých premietaní s voľnými miestami")
    parser.add_argument("--alert-free", type=int, default=1, metavar="N", help="Hlásiť, keď pribudne aspoň N voľných miest (default 1)")
    parser.add_argument("--json-out", default=JSON_OUT, metavar="SÚBOR", help="Uložiť stav + históriu hlásení do JSONu pre webovú stránku")
    parser.add_argument("--mail-on", default=MAIL_ON, choices=["tickets", "all", "never"], help="Kedy poslať e-mail: tickets = len keď sa dá kúpiť lístok (default), all = pri každej zmene, never = nikdy")
    parser.add_argument("--mail-first-run", action="store_true", help="Poslať e-mail aj pri prvom behu (inak sa prvý beh len uloží ako výchozí stav)")
    parser.add_argument("--test-email", action="store_true", help="Poslať skúšobný e-mail a skončiť (na overenie SMTP nastavenia)")
    args = parser.parse_args()

    setup_logging(args.log_file, args.log_level)
    init_config()
    log.debug(
        "Konfigurácia: CINEMA_ID=%s ATTR=%s LANG=%s DAYS_AHEAD=%s FILM_FILTER=%r STATE_FILE=%s LOG_FILE=%s",
        CINEMA_ID, ATTR, LANG, DAYS_AHEAD, FILM_NAME_FILTER, STATE_FILE, args.log_file,
    )
    log.info(
        "E-mail: %s (adresáti: %s, režim: %s)",
        "nastavený" if mail_configured() else "NENASTAVENÝ (SMTP_HOST / MAIL_TO)",
        ", ".join(mail_recipients()) or "—",
        args.mail_on,
    )

    if args.test_email:
        ok = send_email(
            f"{MAIL_SUBJECT_PREFIX} skúšobný e-mail",
            "Toto je skúšobný e-mail z cinema_watcher.py — SMTP nastavenie funguje.",
            "<p>Toto je skúšobný e-mail z <code>cinema_watcher.py</code> — "
            "SMTP nastavenie funguje.</p>",
        )
        sys.exit(0 if ok else 1)

    if args.watch:
        log.info("Spúšťam sledovanie, interval %ss. Ukonči cez Ctrl+C.", args.interval)
        while True:
            try:
                run_once(
                    verbose_no_change=not args.quiet,
                    dump_dir=args.dump_raw,
                    list_all=args.list_all,
                    alert_free=args.alert_free,
                    json_out=args.json_out,
                    mail_on=args.mail_on,
                    mail_first_run=args.mail_first_run,
                )
            except KeyboardInterrupt:
                raise
            except Exception:
                # Slučka musí prežiť aj neočakávanú chybu — inak sledovanie ticho umrie.
                log.exception("Neočakávaná chyba počas kontroly, pokračujem ďalším cyklom.")
            time.sleep(args.interval)
    else:
        ok = run_once(
            verbose_no_change=not args.quiet,
            dump_dir=args.dump_raw,
            list_all=args.list_all,
            alert_free=args.alert_free,
            json_out=args.json_out,
            mail_on=args.mail_on,
            mail_first_run=args.mail_first_run,
        )
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Ukončené používateľom.")
        sys.exit(130)
