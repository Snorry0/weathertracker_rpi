from __future__ import annotations
"""
TUI interattiva "alla htop" con GRAFICI completi (plotext)
----------------------------------------------------------
- Fullscreen curses (Ctrl+C / q per uscire)
- Auto-refresh schermo e refetch dati separati (per non stressare l'API)
- Grafici 'veri' in ASCII via plotext, integrati nel pannello di destra
- Tasti per cambiare città, unità, ore, modalità grafici, ecc.
- Fallback automatico a sparkline se lo spazio non basta

Architettura (alto livello):
- AppState  : stato dell'app (città, unità, ore, timers, dati)
- ensure_data(): decide quando refetchare e aggiorna `state.data`
- run()     : main loop curses → disegna layout + gestisce input
- draw_*()  : funzioni di rendering per header, pannelli, grafici
"""

import curses
import time
import textwrap
import io
import contextlib
from dataclasses import dataclass
from typing import List, Optional

# Riuso di funzioni dal modulo CLI (nessun dup)
from .cli import (
    ip_location,
    geocode_city,
    fetch_weather,
    describe_code,
)

# --- Utilità locali ----------------------------------------------------------

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
SPARK_ASCII  = " .:-=+*#%@"

def sparkline(values: List[float], width: int = 40, charset: str = "blocks") -> str:
    """Mini grafico orizzontale con blocchi (default) o ASCII puro."""
    blocks = SPARK_BLOCKS if charset == "blocks" else SPARK_ASCII
    if not values:
        return ""
    n = len(values)
    if n > width:
        step = n / width
        values = [values[int(i * step)] for i in range(width)]
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        return blocks[len(blocks) // 2] * len(values)
    rng = vmax - vmin
    out = []
    for v in values:
        idx = int(round((v - vmin) / rng * (len(blocks) - 1)))
        idx = max(0, min(idx, len(blocks) - 1))
        out.append(blocks[idx])
    return "".join(out)

def clean_floats(seq):
    """Converte in float e rimuove None."""
    return [float(v) for v in seq if v is not None]

def describe_for_tui(code: int | None, use_emoji: bool) -> str:
    """Mostra '🌧️ Pioggia' se use_emoji True, altrimenti solo 'Pioggia'."""
    txt = describe_code(code)
    if use_emoji:
        return txt
    return txt.split(" ", 1)[1] if " " in txt else txt

# --- Stato -------------------------------------------------------------------

@dataclass
class AppState:
    city_query: Optional[str] = None        # stringa cercata (None = geo-IP)
    city_label: str = "—"                   # etichetta visibile
    temp_unit: str = "c"                    # "c" | "f"
    wind_unit: str = "mph"                  # "kmh" | "ms" | "mph" | "kn"
    hours: int = 48                         # orizzonte ore (6..168)
    refresh_sec: int = 2                    # ridisegno schermo
    refetch_sec: int = 300                  # nuovo fetch dati
    last_fetch_ts: float = 0.0              # monotonic() ultimo fetch
    data: Optional[dict] = None             # dati meteo
    use_emoji: bool = False                 # se mostrare emoji (richiede UTF-8 + font)
    spark_charset: str = "blocks"           # per fallback grafici
    graph_mode: str = "temp_prec"           # "temp_prec" | "prec_hum" (t = toggle)

    def clamp_hours(self) -> None:
        self.hours = max(6, min(168, self.hours))

# --- Input riga in curses (per cambiare città) --------------------------------

def prompt_input_line(stdscr, prompt: str) -> Optional[str]:
    """Prompt in basso, Enter conferma, ESC annulla."""
    max_y, max_x = stdscr.getmaxyx()
    y = max_y - 1
    stdscr.move(y, 0); stdscr.clrtoeol()
    msg = f"{prompt}: "
    stdscr.addstr(y, 0, msg); stdscr.refresh()

    buf: List[str] = []
    x = len(msg); curses.curs_set(1)
    while True:
        ch = stdscr.get_wch()
        if isinstance(ch, str) and ch == "\n":
            curses.curs_set(0); return "".join(buf)
        if ch == 27:  # ESC
            curses.curs_set(0); return None
        if ch in ("\b", "\x7f", curses.KEY_BACKSPACE):
            if buf:
                buf.pop(); x -= 1
                stdscr.move(y, x); stdscr.delch()
        elif isinstance(ch, str) and ch.isprintable():
            if x < max_x - 1:
                buf.append(ch); stdscr.addstr(y, x, ch); x += 1
        stdscr.refresh()

# --- Fetch dati ---------------------------------------------------------------

def ensure_data(state: AppState) -> None:
    """Decide se refetchare in base a `refetch_sec` o variazioni di stato."""
    now = time.monotonic()
    if state.data is not None and (now - state.last_fetch_ts) < state.refetch_sec:
        return

    loc = geocode_city(state.city_query) if state.city_query else ip_location()
    if not loc: loc = geocode_city("Roma")
    if not loc:
        state.city_label = "Posizione non trovata"
        state.data = None; state.last_fetch_ts = now; return

    try:
        data = fetch_weather(loc, state.hours, state.temp_unit, state.wind_unit)
        state.city_label = loc.name
        state.data = data
        state.last_fetch_ts = now
    except Exception:
        # mantieni i dati vecchi se c’erano
        state.city_label = loc.name
        state.last_fetch_ts = now

# --- Rendering layout ---------------------------------------------------------

def draw_header(stdscr, state: AppState) -> int:
    """Riga titolo + riga help + riga status (ritorna y di partenza body)."""
    max_y, max_x = stdscr.getmaxyx()
    line = f" meteocli-tui  ·  {state.city_label}  ·  {time.strftime('%Y-%m-%d %H:%M:%S')} "
    stdscr.addstr(0, 0, line[:max_x].ljust(max_x), curses.A_REVERSE)

    help_line = "[q] quit  [c] città  [g] geo-IP  [u] C/F  [w] vento  [+/-] ore  [t] grafici  [r] refresh  [?] help"
    stdscr.addstr(1, 0, help_line[:max_x].ljust(max_x))

    status = f"emoji:{'on' if state.use_emoji else 'off'}  graph:{state.graph_mode}  hours:{state.hours}  wind:{state.wind_unit}"
    stdscr.addstr(2, 0, status[:max_x].ljust(max_x))
    return 3

def safe_get(arr, idx, default=None):
    return arr[idx] if isinstance(arr, list) and idx < len(arr) else default

def draw_current_panel(stdscr, y0: int, x0: int, w: int, state: AppState) -> int:
    """Pannello sinistro con meteo attuale."""
    stdscr.addstr(y0, x0, "[ Attuale ]")
    y = y0 + 1
    if not state.data:
        stdscr.addstr(y, x0, "Nessun dato."); return y + 1
    h = state.data.get("hourly", {})
    t = safe_get(h.get("temperature_2m", []), 0)
    rh = safe_get(h.get("relative_humidity_2m", []), 0)
    pp = safe_get(h.get("precipitation_probability", []), 0)
    pr = safe_get(h.get("pressure_msl", []), 0)
    ws = safe_get(h.get("wind_speed_10m", []), 0)
    wc = safe_get(h.get("weather_code", []), 0)
    unit_t = "°C" if state.temp_unit == "c" else "°F"
    rows = [
        ("Meteo", describe_for_tui(wc, state.use_emoji)),
        ("Temperatura", f"{t:.1f} {unit_t}" if t is not None else "—"),
        ("Umidità", f"{int(rh)}%" if rh is not None else "—"),
        ("Prec. prob.", f"{int(pp)}%" if pp is not None else "—"),
        ("Vento", f"{ws:.1f} {state.wind_unit}" if ws is not None else "—"),
        ("Pressione", f"{int(pr)} hPa" if pr is not None else "—"),
    ]
    for k, v in rows:
        stdscr.addstr(y, x0, f"{k:<15} {v}"[:w]); y += 1
    return y

def draw_forecast_table(stdscr, y0: int, x0: int, w: int, state: AppState) -> int:
    """Tabella compatta (prime 12 ore ogni 3h)."""
    stdscr.addstr(y0, x0, "[ Prossime 12 ore (3h) ]"); y = y0 + 1
    if not state.data: stdscr.addstr(y, x0, "Nessun dato."); return y + 1
    H = state.data.get("hourly", {})
    times = H.get("time", [])[:state.hours]
    temps = H.get("temperature_2m", [])[:state.hours]
    probs = H.get("precipitation_probability", [])[:state.hours]
    codes = H.get("weather_code", [])[:state.hours]
    unit_t = "°C" if state.temp_unit == "c" else "°F"
    stdscr.addstr(y, x0, "Ora   Temp   Prec%   Meteo"[:w]); y += 1
    for i in range(0, min(len(times), 12), 3):
        tstr = safe_get(times, i, "—")
        tval = safe_get(temps, i)
        pval = safe_get(probs, i)
        code = safe_get(codes, i)
        ora = tstr[11:16] if isinstance(tstr, str) and len(tstr) >= 16 else "—"
        line = f"{ora:<5} { (f'{tval:.0f}{unit_t}' if tval is not None else '—') :<6} { (f'{int(pval)}%' if pval is not None else '—') :<6} {describe_for_tui(code, state.use_emoji)}"
        stdscr.addstr(y, x0, line[:w]); y += 1
    return y

# --- Grafici plotext integrati -----------------------------------------------

def render_plotext_chart(width: int, height: int, title: str, xlabels: List[str], values: List[float], ylabel: str) -> List[str]:
    """
    Costruisce un grafico plotext e NE RESTITUISCE LE RIGHE come lista di stringhe.
    - width/height: dimensioni massime disponibili nel pannello.
    - xlabels: etichette (testo breve); se troppo lunghe, plotext le gestirà sparsen.
    """
    import plotext as plt  # import locale per evitare impatto su curses globalmente
    # plotext disegna su stdout; lo catturiamo in una StringIO
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            plt.clear_figure()
            # Un po' di margine per bordo/padding
            w = max(20, width - 2)
            h = max(10, height // 2 - 1)  # verrà usato due volte (due grafici)
            plt.plotsize(w, h)
            plt.title(title)
            plt.xlabel("Ora")
            plt.ylabel(ylabel)
            plt.plot(values)
            # xticks: riduciamo numero per non affollare
            if xlabels:
                step = max(1, len(xlabels) // 10)
                xs = list(range(0, len(xlabels), step))
                xl = [xlabels[i] for i in xs]
                plt.xticks(xs, xl)
            plt.show()
        except Exception as e:
            # in caso di errori, restituiamo un messaggio visibile nel pannello
            return [f"[plotext error] {e}"]
    out = buf.getvalue().splitlines()
    # plotext in genere produce trailing righe vuote: puliamo/ridimensioniamo
    return [line[:width] for line in out if line.strip("\n") != ""]

def draw_graphs(stdscr, y0: int, x0: int, w: int, hgt: int, state: AppState) -> None:
    """
    Pannello destro con DUE grafici plotext (temp + prec OPPURE prec + umidità).
    - Se lo spazio è troppo stretto, fallback a sparkline (ASCII).
    """
    stdscr.addstr(y0, x0, "[ Grafici ]  (t: cambia modalità)")
    y = y0 + 1
    if not state.data:
        stdscr.addstr(y, x0, "Nessun dato."); return

    H = state.data.get("hourly", {})
    times = H.get("time", [])[:state.hours]
    xlabels = [t[11:16] if isinstance(t, str) and len(t) >= 16 else "" for t in times]
    temps = clean_floats(H.get("temperature_2m", [])[:state.hours])
    probs = clean_floats(H.get("precipitation_probability", [])[:state.hours])
    hums  = clean_floats(H.get("relative_humidity_2m", [])[:state.hours])

    # Se lo spazio è scarso → sparkline
    if w < 40 or hgt < 20 or len(temps) < 4:
        stdscr.addstr(y, x0, "(spazio ridotto → grafici compatti)")
        y += 1
        if state.graph_mode == "temp_prec":
            stdscr.addstr(y, x0, "Temperatura"); y += 1
            stdscr.addstr(y, x0, sparkline(temps, width=max(10, w - 2), charset=state.spark_charset)); y += 2
            stdscr.addstr(y, x0, "Probabilità precipitazioni (%)"); y += 1
            stdscr.addstr(y, x0, sparkline(probs, width=max(10, w - 2), charset=state.spark_charset)); y += 1
        else:
            stdscr.addstr(y, x0, "Probabilità precipitazioni (%)"); y += 1
            stdscr.addstr(y, x0, sparkline(probs, width=max(10, w - 2), charset=state.spark_charset)); y += 2
            stdscr.addstr(y, x0, "Umidità (%)"); y += 1
            stdscr.addstr(y, x0, sparkline(hums, width=max(10, w - 2), charset=state.spark_charset)); y += 1
        return

    unit_t = "°C" if state.temp_unit == "c" else "°F"

    # Primo grafico
    if state.graph_mode == "temp_prec":
        g1_lines = render_plotext_chart(w, hgt, "Temperatura oraria", xlabels, temps, f"Temp ({unit_t})")
        g2_lines = render_plotext_chart(w, hgt, "Probabilità precipitazioni", xlabels, probs, "%")
    else:
        g1_lines = render_plotext_chart(w, hgt, "Probabilità precipitazioni", xlabels, probs, "%")
        g2_lines = render_plotext_chart(w, hgt, "Umidità", xlabels, hums, "%")

    # Disegna i due blocchi (uno sotto l'altro)
    for line in g1_lines[: hgt // 2 - 1]:
        if y >= y0 + hgt - 1: break
        stdscr.addstr(y, x0, line[:w]); y += 1
    # separatore
    if y < y0 + hgt - 1:
        stdscr.addstr(y, x0, "-" * min(w, 60)); y += 1
    for line in g2_lines[: (y0 + hgt - 1) - y]:
        stdscr.addstr(y, x0, line[:w]); y += 1

# --- Help overlay -------------------------------------------------------------

def draw_help_overlay(stdscr) -> None:
    max_y, max_x = stdscr.getmaxyx()
    help_text = """
    Tasti:
      q / ESC — Esci
      c       — Cambia città (prompt in basso). Enter = OK, ESC = annulla
      g       — Geo-localizza via IP (annulla override città)
      u       — Cambia unità temperatura (°C/°F)
      w       — Cambia unità vento (mph → kmh → ms → kn)
      + / -   — Aumenta / diminuisci ore previsione (min 6, max 168)
      t       — Cambia i grafici (Temp+Prec  <→  Prec+Umidità)
      r       — Ridisegna subito (senza refetch)
      ? / h   — Mostra/Nascondi questa guida

    Note:
    - I grafici usano plotext; se lo spazio è troppo ridotto, passo a sparkline compatte.
    - Refetch dati ogni 'refetch_sec' (default 300s); lo schermo si aggiorna ogni 'refresh_sec' (default 2s).
    - Per vedere emoji serve locale UTF-8 + font compatibile.
    """.strip("\n")

    box_w = min(86, max_x - 4)
    lines = []
    for para in help_text.split("\n"):
        lines.extend(textwrap.wrap(para, width=box_w - 4) or [""])

    box_h = min(len(lines) + 4, max_y - 4)
    y0 = (max_y - box_h) // 2; x0 = (max_x - box_w) // 2

    for i in range(box_h):
        stdscr.addstr(y0 + i, x0, " " * box_w, curses.A_REVERSE if i in (0, box_h-1) else 0)
    stdscr.addstr(y0, x0 + 2, " Aiuto ")

    y = y0 + 2
    for line in lines[: box_h - 3]:
        stdscr.addstr(y, x0 + 2, line.ljust(box_w - 4)); y += 1

# --- Main loop curses ---------------------------------------------------------

def run(stdscr, state: AppState) -> int:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(state.refresh_sec * 1000)
    curses.use_default_colors()

    show_help = False

    while True:
        ensure_data(state)        # refetch se serve
        stdscr.erase()            # pulisci frame
        y = draw_header(stdscr, state)

        # 3 colonne: sinistra (attuale) | centro (tabella) | destra (grafici)
        max_y, max_x = stdscr.getmaxyx()
        col = max(24, max_x // 3)
        left_x, mid_x, right_x = 0, col + 1, (col * 2) + 2
        right_w = max_x - right_x - 1
        body_h = max_y - y - 1

        draw_current_panel(stdscr, y, left_x, col - 2, state)
        draw_forecast_table(stdscr, y, mid_x, col - 2, state)
        draw_graphs(stdscr, y, right_x, right_w, body_h, state)

        if show_help: draw_help_overlay(stdscr)
        stdscr.refresh()

        # Input non bloccante (attende max refresh_sec)
        try:
            ch = stdscr.get_wch()
        except curses.error:
            ch = None
        if ch is None:
            continue
        key = ch.lower() if isinstance(ch, str) else ch

        if key in ("q", "\x1b"):              # ESC o q
            return 0
        elif key == "r":                       # redraw
            pass
        elif key == "u":                       # °C/°F
            state.temp_unit = "f" if state.temp_unit == "c" else "c"
            state.last_fetch_ts = 0.0
        elif key == "w":                       # unità vento
            order = ["mph", "kmh", "ms", "kn"]
            state.wind_unit = order[(order.index(state.wind_unit) + 1) % len(order)]
            state.last_fetch_ts = 0.0
        elif key == "+":                       # più ore
            state.hours += 6; state.clamp_hours(); state.last_fetch_ts = 0.0
        elif key == "-":                       # meno ore
            state.hours -= 6; state.clamp_hours(); state.last_fetch_ts = 0.0
        elif key == "t":                       # cambia combinazione grafici
            state.graph_mode = "prec_hum" if state.graph_mode == "temp_prec" else "temp_prec"
        elif key == "g":                       # geo-IP
            state.city_query = None; state.last_fetch_ts = 0.0
        elif key == "c":                       # prompt città
            new_city = prompt_input_line(stdscr, "Nuova città (Enter=OK, ESC=annulla)")
            if new_city is not None:
                new_city = new_city.strip()
                state.city_query = new_city or None
                state.last_fetch_ts = 0.0
        elif key in ("?", "h"):
            show_help = not show_help

def main() -> int:
    state = AppState(refresh_sec=2, refetch_sec=300)
    try:
        return curses.wrapper(run, state)
    except KeyboardInterrupt:
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
