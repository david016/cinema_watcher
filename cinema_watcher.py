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

Ako spustiť jednorazovo:
    python3 cinema_watcher.py

Ako sledovať priebežne (napr. každých 10 minút):
    python3 cinema_watcher.py --watch --interval 600

Logovanie:
  - Všetko ide naraz do konzoly aj do log súboru (default cinema_watcher.log
    vedľa skriptu, rotuje po 1 MB, drží 5 starých kópií).
  - `--log-level DEBUG` pridá detaily: každý stiahnutý event, obsadenosť,
    orezaný náhľad odpovede z API.
  - `--dump-raw ADRESÁR` uloží surové JSON odpovede z API do súborov —
    keď potrebuješ vidieť presne to, čo server vrátil.

Ako sledovať priebežne (napr. každých 10 minút):
    */10 * * * * /usr/bin/python3 /cesta/k/cinema_watcher.py

Konfigurácia nižšie (CINEMA_ID, ATTR, FILM_NAME_FILTER) sa dá zmeniť aj bez
zásahu do kódu cez premenné prostredia (pozri sekciu CONFIG).
"""

import json
import os
import re
import sys
import time
import logging
import argparse
import urllib.request
import urllib.error
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta

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

# Kapacity sál — potrebné na prepočet availabilityRatio na počet voľných miest.
# API kapacitu priamo nevracia, ale availabilityRatio je vždy presný násobok
# 1/kapacita (orezaný na 4 desatinné miesta), takže sa dá odvodiť:
# pre IMAX VOLVO sedí 384 (odchýlka < 0,04 miesta na všetkých pozorovaných hodnotách).
# Prepísať/doplniť sa dá cez CINEMA_CAPACITY, napr.: {"IMAX VOLVO": 384, "Sál 5": 120}
DEFAULT_CAPACITY = {"IMAX VOLVO": 384}
CAPACITIES = dict(DEFAULT_CAPACITY)

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


def init_capacities():
    """Načíta kapacity sál z CINEMA_CAPACITY (JSON) navrch predvolených hodnôt."""
    raw = os.environ.get("CINEMA_CAPACITY", "").strip()
    if not raw:
        return
    try:
        CAPACITIES.update({str(k): int(v) for k, v in json.loads(raw).items()})
        log.debug("Kapacity sál po načítaní CINEMA_CAPACITY: %s", CAPACITIES)
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as e:
        log.warning(
            "CINEMA_CAPACITY sa nedá rozparsovať (%s), používam predvolené: %s", e, CAPACITIES
        )


def free_seats_for(auditorium: str, ratio):
    """Prepočíta availabilityRatio na počet voľných miest. None = kapacita sály neznáma."""
    if ratio is None:
        return None
    capacity = CAPACITIES.get(auditorium)
    if not capacity:
        return None
    return round(ratio * capacity)


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

            entry = {
                "film": film_name,
                "date": date_str,
                "time": ev.get("eventDateTime", "")[11:16],  # HH:MM z ISO stringu
                "auditorium": auditorium,
                "sold_out": bool(ev.get("soldOut", False)),
                "availability_ratio": ratio,
                "free_seats": free_seats_for(auditorium, ratio),
                "capacity": CAPACITIES.get(auditorium),
                "booking_link": (
                    f"https://www.cinemacity.cz/cz/booking-router/launch/{event_id}?lang=cs"
                ),
            }
            current[event_id] = entry
            log.debug(
                "  %s %s | %s | sála %s | vypredané=%s | ratio=%s | voľných=%s | id=%s",
                entry["date"], entry["time"], entry["film"], entry["auditorium"],
                entry["sold_out"], ratio, entry["free_seats"], event_id,
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
        return f"{free} voľných z {ev.get('capacity')}"
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
        if ev.get("free_seats") is not None:
            total_free += ev["free_seats"]
            known += 1

    if known:
        log.info("Spolu voľných miest: %d (v %d premietaniach)", total_free, known)


def diff_and_report(previous: dict, current: dict, alert_free: int = 1):
    new_ids = set(current) - set(previous)
    removed_ids = set(previous) - set(current)
    common_ids = set(current) & set(previous)

    log.debug(
        "Porovnanie: %d nových, %d zrušených, %d spoločných.",
        len(new_ids), len(removed_ids), len(common_ids),
    )

    report_lines = []

    for eid in sorted(new_ids, key=lambda i: (current[i]["date"], current[i]["time"])):
        ev = current[eid]
        report_lines.append(
            f"🆕 NOVÝ TERMÍN: {ev['film']} — {ev['date']} {ev['time']} "
            f"(sála {ev['auditorium']}, {describe_seats(ev)}) -> {ev['booking_link']}"
        )

    for eid in sorted(removed_ids, key=lambda i: (previous[i]["date"], previous[i]["time"])):
        ev = previous[eid]
        report_lines.append(
            f"❌ ZRUŠENÝ TERMÍN: {ev['film']} — {ev['date']} {ev['time']} (sála {ev['auditorium']})"
        )

    for eid in sorted(common_ids, key=lambda i: (current[i]["date"], current[i]["time"])):
        old, new = previous[eid], current[eid]
        was_soldout = old["sold_out"]
        is_soldout = new["sold_out"]
        old_free = old.get("free_seats")
        new_free = new.get("free_seats")

        if was_soldout and not is_soldout:
            report_lines.append(
                f"🎟️ UVOĽNILI SA MIESTA: {new['film']} — {new['date']} {new['time']} "
                f"(sála {new['auditorium']}, {describe_seats(new)}) -> {new['booking_link']}"
            )
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
                report_lines.append(
                    f"🎟️ PRIBUDLI VOĽNÉ MIESTA (+{delta}): {new['film']} — "
                    f"{new['date']} {new['time']} (sála {new['auditorium']}, "
                    f"{old_free} -> {new_free} voľných) -> {new['booking_link']}"
                )
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

    return report_lines


def run_once(verbose_no_change=True, dump_dir="", list_all=True, alert_free=1):
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
        return False

    if not current and not previous:
        log.warning(
            "API nevrátilo žiadne premietania. Skontroluj CINEMA_ID / ATTR / filter filmu."
        )
        return True

    if list_all:
        log_snapshot(current)

    changes = diff_and_report(previous, current, alert_free=alert_free)

    if changes:
        log.info("--- ZMENY (%d) ---", len(changes))
        for line in changes:
            log.info(line)
    elif verbose_no_change:
        log.info("Žiadne zmeny (%d sledovaných premietaní).", len(current))

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
    args = parser.parse_args()

    setup_logging(args.log_file, args.log_level)
    init_capacities()
    log.debug(
        "Konfigurácia: CINEMA_ID=%s ATTR=%s LANG=%s DAYS_AHEAD=%s FILM_FILTER=%r STATE_FILE=%s LOG_FILE=%s",
        CINEMA_ID, ATTR, LANG, DAYS_AHEAD, FILM_NAME_FILTER, STATE_FILE, args.log_file,
    )

    if args.watch:
        log.info("Spúšťam sledovanie, interval %ss. Ukonči cez Ctrl+C.", args.interval)
        while True:
            try:
                run_once(
                    verbose_no_change=not args.quiet,
                    dump_dir=args.dump_raw,
                    list_all=args.list_all,
                    alert_free=args.alert_free,
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
        )
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Ukončené používateľom.")
        sys.exit(130)
