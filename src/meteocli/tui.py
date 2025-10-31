from __future__ import annotations
"""
Implementazione terminale interattivo
----------------------------------------

Obiettivo:
- Funzionamento ad htop attraverso curses (la vista in htop è carina, nonostante l'assenza di una GUI fatta e finita).
- Input da tastiera per cambiare città, unità, orizzonte ore, ecc.
- Uscita con 'q', help con '?', geolocalizzazione da IP con 'g'.

Perché curses?
- Gestisce schermo intero, input non-bloccante, colori e posizioni x/y.

Architettura:
- AppState: tiene stato (città, unità, ore, tempi di refresh).
- ciclo principale: ogni 'refresh' secondi ridisegna; ogni 'refetch' secondi rifà la chiamata HTTP.
- layout "manuale": header (riga 0-1), pannello attuale (sinistra), tabella forecast (centro),
  sparklines semplici (destra). Il tutto adattato alla dimensione del terminale.

"""

import curses
import time
import textwrap
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .cli import (
    ip_location,
    geocode_city,
    fetch_weather,
    describe_code,
)

# --------------------------
# Utilità "sparkline" ASCII
# --------------------------
def sparkline(values: List[float], width: int = 40) -> str:
    """
    Converte una lista di valori numerici in blocchi più impattanti visivamente ▁▂▃▄▅▆▇█.
    - width: larghezza target; se values è più lunga, viene sotto-campionata.
    - Nota: se i valori sono costanti, mostra una linea orizzontale intermedia.
    """
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    n = len(values)
    # Sottocampionamento semplice: prendi circa 'width' punti equidistanti
    if n > width:
        step = n / width
        values = [values[int(i * step)] for i in range(width)]

    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        return blocks[len(blocks) // 2] * len(values)

    rng = vmax - vmin
    out = []
    for v in values:
        # Normalizza [vmin,vmax] -> [0,len(blocks)-1]
        idx = int(round((v - vmin) / rng * (len(blocks) - 1)))
        idx = max(0, min(idx, len(blocks) - 1))
        out.append(blocks[idx])
    return "".join(out)


# --------------------------
# Stato applicazione
# --------------------------
@dataclass
class AppState:
    city_query: Optional[str] = None   # testo cercato dall'utente (es. "Bari"); None = usa geoloc
    city_label: str = "—"              # etichetta visibile (es. "Bari, Puglia, Italy")
    temp_unit: str = "c"               # "c" = celsius o "f" = farhenheit
    wind_unit: str = "mph"             # "kmh", "ms", "mph", "kn" a seconda della preferenza
    hours: int = 48                    # orizzonte previsioni
    refresh_sec: int = 2               # ogni quanti secondi ridisegnare lo schermo
    refetch_sec: int = 300             # ogni quanti secondi rifare la chiamata HTTP
    last_fetch_ts: float = 0.0         # monotonic() dell’ultimo fetch
    data: Optional[dict] = None        # dati meteo correnti (struttura Open-Meteo)
    graph_mode: str = "temp"           # "temp" o "prec" (toggle con 't')

    def cycle_temp_unit(self) -> None:
        self.temp_unit = "f" if self.temp_unit == "c" else "c"

    def cycle_wind_unit(self) -> None:
        order = ["mph", "kmh", "ms", "kn"]
        self.wind_unit = order[(order.index(self.wind_unit) + 1) % len(order)]

    def clamp_hours(self) -> None:
        self.hours = max(6, min(168, self.hours))  # >=6 utile per qualche sparkline non vuota


# --------------------------
# Input "linea" in curses
# --------------------------
def prompt_input_line(stdscr, prompt: str) -> Optional[str]:
    """
    Prompt a fondo schermo per acquisire una stringa (es: nuova città).
    - Gestisce backspace, ESC per annullare, Enter per confermare.
    - Ritorna None se annullato, altrimenti la stringa (anche vuota).
    """
    max_y, max_x = stdscr.getmaxyx()
    y = max_y - 1
    # Barra prompt: cancelliamo la riga e scriviamo il messaggio
    stdscr.move(y, 0)
    stdscr.clrtoeol()
    msg = f"{prompt}: "
    stdscr.addstr(y, 0, msg)
    stdscr.refresh()

    buf: List[str] = []
    x = len(msg)
    curses.curs_set(1)  # cursore visibile

    while True:
        ch = stdscr.get_wch()
        if isinstance(ch, str) and ch == "\n":
            curses.curs_set(0)
            return "".join(buf)
        if ch == 27:  # ESC
            curses.curs_set(0)
            return None
        if ch in ("\b", "\x7f", curses.KEY_BACKSPACE):
            if buf:
                buf.pop()
                x -= 1
                stdscr.move(y, x)
                stdscr.delch()
        elif isinstance(ch, str) and ch.isprintable():
            if x < max_x - 1:
                buf.append(ch)
                stdscr.addstr(y, x, ch)
                x += 1
        stdscr.refresh()


# --------------------------
# Disegno schermo
# --------------------------
def draw_header(stdscr, state: AppState) -> int:
    """
    Disegna l'header con titolo, città e hint comandi.
    Ritorna la prossima riga libera (y) da cui proseguire a disegnare.
    """
    max_y, max_x = stdscr.getmaxyx()
    title = " meteocli-tui "
    city = f" · {state.city_label} · "
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    line = (title + city + now).center(max_x)
    stdscr.addstr(0, 0, line[:max_x], curses.A_REVERSE)

    help_line = "[q] quit  [c] città  [g] geo-IP  [t] grafico  [u] C/F  [w] vento  [+/-] ore  [r] refresh  [?] help"
    stdscr.addstr(1, 0, help_line[:max_x])
    return 2  # prossima riga


def safe_get(arr, idx, default=None):
    return arr[idx] if isinstance(arr, list) and idx < len(arr) else default


def draw_current_panel(stdscr, y0: int, x0: int, w: int, state: AppState) -> int:
    """
    Pannello "Attuale": meteo, temp, umidità, prob. precipitazioni, vento, pressione.
    Ritorna la riga successiva rispetto a y0 da cui proseguire (utile se vuoi stack verticale).
    """
    # Titolo pannello
    stdscr.addstr(y0, x0, "[ Attuale ]")
    y = y0 + 1

    if not state.data:
        stdscr.addstr(y, x0, "Nessun dato (ancora).")
        return y + 1

    h = state.data.get("hourly", {})
    times = h.get("time", [])
    # index 0 ~ prima ora della serie (Open-Meteo ordina in avanti)
    t = safe_get(h.get("temperature_2m", []), 0)
    rh = safe_get(h.get("relative_humidity_2m", []), 0)
    pp = safe_get(h.get("precipitation_probability", []), 0)
    pr = safe_get(h.get("pressure_msl", []), 0)
    ws = safe_get(h.get("wind_speed_10m", []), 0)
    wc = safe_get(h.get("weather_code", []), 0)

    unit_t = "°C" if state.temp_unit == "c" else "°F"
    rows = [
        ("Meteo", describe_code(wc)),
        ("Temperatura", f"{t:.1f} {unit_t}" if t is not None else "—"),
        ("Umidità", f"{int(rh)}%" if rh is not None else "—"),
        ("Prec. prob.", f"{int(pp)}%" if pp is not None else "—"),
        ("Vento", f"{ws:.1f} {state.wind_unit}" if ws is not None else "—"),
        ("Pressione", f"{int(pr)} hPa" if pr is not None else "—"),
    ]

    for k, v in rows:
        stdscr.addstr(y, x0, f"{k:<15} {v}")
        y += 1

    return y


def draw_forecast_table(stdscr, y0: int, x0: int, w: int, state: AppState) -> int:
    """
    Tabella forecast compatta: prime 12 ore (ogni 3h).
    """
    stdscr.addstr(y0, x0, "[ Prossime 12 ore (3h) ]")
    y = y0 + 1

    if not state.data:
        stdscr.addstr(y, x0, "Nessun dato (ancora).")
        return y + 1

    h = state.data.get("hourly", {})
    times = h.get("time", [])[:state.hours]
    temps = h.get("temperature_2m", [])[:state.hours]
    probs = h.get("precipitation_probability", [])[:state.hours]
    codes = h.get("weather_code", [])[:state.hours]

    unit_t = "°C" if state.temp_unit == "c" else "°F"
    stdscr.addstr(y, x0, "Ora   Temp   Prec%   Meteo")
    y += 1

    for i in range(0, min(len(times), 12), 3):
        tstr = safe_get(times, i, "—")
        tval = safe_get(temps, i)
        pval = safe_get(probs, i)
        code = safe_get(codes, i)
        ora = tstr[11:16] if isinstance(tstr, str) and len(tstr) >= 16 else "—"
        temp_str = f"{tval:.0f}{unit_t}" if tval is not None else "—"
        prec_str = f"{int(pval)}%" if pval is not None else "—"
        meteo_str = describe_code(code)
        # Troncamento per non sporcare layout
        line = f"{ora:<5} {temp_str:<6} {prec_str:<6} {meteo_str}"
        stdscr.addstr(y, x0, line[:w])
        y += 1

    return y


def draw_graphs(stdscr, y0: int, x0: int, w: int, hgt: int, state: AppState) -> None:
    """
    Sezione grafici testuali (sparkline).
    - Modalità 'temp': mostra temperatura e poi probabilità precipitazioni.
    - Modalità 'prec': mostra probabilità precipitazioni e poi umidità.
    """
    stdscr.addstr(y0, x0, "[ Grafici ]  (t: cambia modalità)")
    y = y0 + 1

    if not state.data:
        stdscr.addstr(y, x0, "Nessun dato (ancora).")
        return

    H = state.data.get("hourly", {})
    temps = H.get("temperature_2m", [])[:state.hours]
    probs = H.get("precipitation_probability", [])[:state.hours]
    hums  = H.get("relative_humidity_2m", [])[:state.hours]

    # helper per sparkline: rimuove None
    def clean(nums):
        return [float(v) for v in nums if v is not None]

    width = max(10, w - 2)  # lascia un paio di colonne di margine

    if state.graph_mode == "temp":
        stdscr.addstr(y, x0, "Temperatura")
        y += 1
        stdscr.addstr(y, x0, sparkline(clean(temps), width=width))
        y += 2
        stdscr.addstr(y, x0, "Probabilità precipitazioni (%)")
        y += 1
        stdscr.addstr(y, x0, sparkline(clean(probs), width=width))
        y += 1
    else:
        stdscr.addstr(y, x0, "Probabilità precipitazioni (%)")
        y += 1
        stdscr.addstr(y, x0, sparkline(clean(probs), width=width))
        y += 2
        stdscr.addstr(y, x0, "Umidità (%)")
        y += 1
        stdscr.addstr(y, x0, sparkline(clean(hums), width=width))
        y += 1


def draw_help_overlay(stdscr) -> None:
    """
    Disegna un overlay semi-trasparente con i tasti. Premi di nuovo '?' per chiudere.
    """
    max_y, max_x = stdscr.getmaxyx()
    help_text = """
    Tasti:
      q    — Esci
      c    — Cambia città (prompt in basso). Enter = applica, ESC = annulla
      g    — Geolocalizza via IP (override eventuale city_query)
      u    — Cambia unità temperatura (°C/°F)
      w    — Cambia unità vento (mph → kmh → ms → kn)
      + / - — Aumenta / diminuisce ore previsione (min 6, max 168)
      t    — Cambia il pannello grafici (temp/prec vs prec/umidità)
      r    — Ridisegna subito (senza refetch)
      ?    — Mostra/nasconde questo aiuto

    Note:
    - Lo schermo si aggiorna ogni 'refresh' secondi, ma i dati meteo si refetchano
      solo ogni 'refetch' secondi, per non stressare l'API (default 300s).
    - Eseguito su Raspberry via SSH non richiede altro; su Windows "nativo" serve windows-curses.
    """.strip("\n")

    box_w = min(80, max_x - 4)
    lines = []
    for para in help_text.split("\n"):
        lines.extend(textwrap.wrap(para, width=box_w - 4) or [""])

    box_h = min(len(lines) + 4, max_y - 4)
    y0 = (max_y - box_h) // 2
    x0 = (max_x - box_w) // 2

    # Cornice semplice
    for i in range(box_h):
        stdscr.addstr(y0 + i, x0, " " * box_w, curses.A_REVERSE if i in (0, box_h-1) else 0)
    stdscr.addstr(y0, x0 + 2, " Aiuto ")

    y = y0 + 2
    for line in lines[: box_h - 3]:
        stdscr.addstr(y, x0 + 2, line.ljust(box_w - 4))
        y += 1


# --------------------------
# Fetch dati + init città
# --------------------------
def ensure_data(state: AppState) -> None:
    """
    Se serve, fa il fetch dei dati (in base a refetch_sec).
    Aggiorna city_label se cambia città.
    """
    now = time.monotonic()
    need_fetch = state.data is None or (now - state.last_fetch_ts) >= state.refetch_sec

    if not need_fetch:
        return

    # 1) Risolvi posizione
    if state.city_query:
        loc = geocode_city(state.city_query)
    else:
        loc = ip_location()

    if not loc:
        # fallback: Roma
        loc = geocode_city("Roma")

    if not loc:
        # Nessuna posizione ricavata: lascia i dati a None, ci penserà il ciclo a ridisegnare un errore
        state.city_label = "Posizione non trovata"
        state.data = None
        state.last_fetch_ts = now
        return

    # 2) Fetch meteo
    try:
        data = fetch_weather(loc, state.hours, state.temp_unit, state.wind_unit)
        state.city_label = loc.name
        state.data = data
        state.last_fetch_ts = now
    except Exception:
        # In caso di errore di rete manteniamo i vecchi dati (se presenti)
        state.city_label = loc.name
        state.last_fetch_ts = now


# --------------------------
# Loop principale curses
# --------------------------
def run(stdscr, state: AppState) -> int:
    """
    Loop principale:
    - inizializza curses
    - ogni 'refresh_sec' ridisegna lo schermo
    - gestisce input non-bloccante con timeout
    - esce con 'q'
    """
    curses.curs_set(0)           # cursore invisibile
    stdscr.nodelay(True)         # getch non blocca
    stdscr.timeout(state.refresh_sec * 1000)  # millis di attesa massima tra un ciclo e l'altro
    curses.use_default_colors()  # usa palette terminale

    show_help = False

    while True:
        # 1) eventualmente refetch (se passato abbastanza tempo)
        ensure_data(state)

        # 2) Pulisci schermo e ridisegna layout
        stdscr.erase()
        y = draw_header(stdscr, state)

        # Layout orizzontale a 3 colonne:
        max_y, max_x = stdscr.getmaxyx()
        col_w = max(20, max_x // 3)
        left_x = 0
        mid_x = col_w + 1
        right_x = (col_w * 2) + 2
        right_w = max_x - right_x - 1

        # Pannelli
        y_left_end = draw_current_panel(stdscr, y, left_x, col_w - 2, state)
        y_mid_end  = draw_forecast_table(stdscr, y, mid_x, col_w - 2, state)
        draw_graphs(stdscr, y, right_x, right_w, max_y - y - 1, state)

        # Overlay help
        if show_help:
            draw_help_overlay(stdscr)

        # 3) Mostra sullo schermo
        stdscr.refresh()

        # 4) Leggi input (non blocca oltre il timeout impostato)
        try:
            ch = stdscr.get_wch()
        except curses.error:
            ch = None

        if ch is None:
            # nessun input: ricomincia il ciclo (causerà il sleep implicito del timeout)
            continue

        # Normalizza in minuscolo le lettere
        if isinstance(ch, str):
            key = ch.lower()
        else:
            key = ch

        # 5) Gestione tasti
        if key in ("q", "\x1b"):  # 'q' o ESC
            return 0
        elif key == "r":
            # Ridisegna subito: basta continuare il loop; ensure_data() deciderà se refetchare
            pass
        elif key == "u":
            state.cycle_temp_unit()
            state.last_fetch_ts = 0.0  # forziamo refetch (per unità diverse)
        elif key == "w":
            state.cycle_wind_unit()
            state.last_fetch_ts = 0.0
        elif key == "+":
            state.hours += 6
            state.clamp_hours()
            state.last_fetch_ts = 0.0
        elif key == "-":
            state.hours -= 6
            state.clamp_hours()
            state.last_fetch_ts = 0.0
        elif key == "t":
            state.graph_mode = "prec" if state.graph_mode == "temp" else "temp"
        elif key == "g":
            # Geo-IP: cancella city_query per tornare alla geolocalizzazione
            state.city_query = None
            state.last_fetch_ts = 0.0
        elif key == "c":
            # Prompt per nuova città
            new_city = prompt_input_line(stdscr, "Nuova città (Enter=OK, ESC=annulla)")
            if new_city is not None:
                new_city = new_city.strip()
                # Vuota = torna a geolocalizzazione; stringa = geocoding
                state.city_query = new_city or None
                state.last_fetch_ts = 0.0
        elif key in ("?", "h"):
            show_help = not show_help

        # Nota: non serve uno sleep esplicito: stdscr.timeout() ha già atteso fino a refresh_ms


# --------------------------
# Entry-point
# --------------------------
def main() -> int:
    """
    Punto d'ingresso CLI: crea uno stato di default e avvia curses.wrapper().
    Se vuoi parametri via riga di comando (es. refresh_sec), puoi aggiungere argparse qui.
    """
    # Valori ragionevoli: refresh 2s, refetch 300s (5 min)
    state = AppState(refresh_sec=2, refetch_sec=300)
    try:
        return curses.wrapper(run, state)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
