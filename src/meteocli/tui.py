from __future__ import annotations
"""
TUI interattiva "alla htop" con GRAFICI colore (curses)
- Fullscreen curses (q/ESC per uscire)
- Auto-refresh schermo e refetch dati separati
- Due grafici 'area' colorati nel pannello destro (temperatura + precipitazioni / umidità)
- Fallback a sparkline se lo spazio è troppo ridotto
"""

import curses, time, textwrap
from dataclasses import dataclass
from typing import List, Optional
EMOJI_VARIATION = "\ufe0f"   # Variation Selector-16
ZWJ = "\u200d"               # Zero Width Joiner
def strip_variants(s: str) -> str:
    # rimuove selettori/zwj che a volte rompono il rendering in curses
    return s.replace(ZWJ, "").replace(EMOJI_VARIATION, "")
# riuso funzioni dal modulo CLI
from .cli import ip_location, geocode_city, fetch_weather, describe_code

# ---------------------- utilità base ----------------------
SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
SPARK_ASCII  = " .:-=+*#%@"

def sparkline(values: List[float], width: int = 40, charset: str = "blocks") -> str:
    blocks = SPARK_BLOCKS if charset == "blocks" else SPARK_ASCII
    if not values:
        return ""
    n = len(values)
    if n > width:
        step = n / width
        values = [values[int(i * step)] for i in range(width)]
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        return blocks[len(blocks)//2] * len(values)
    rng = vmax - vmin
    out = []
    for v in values:
        idx = int(round((v - vmin) / rng * (len(blocks)-1)))
        idx = max(0, min(idx, len(blocks)-1))
        out.append(blocks[idx])
    return "".join(out)

def clean_floats(seq):
    return [float(v) for v in seq if v is not None]

def describe_for_tui(code: int | None, mode: str) -> str:
    """
    mode:
      - "color": mostra emoji a colori (se il terminale le supporta)
      - "bw":    prova con versione mono (senza variation selector)
      - "off":   rimuovi emoji, lascia solo testo
    """
    txt = describe_code(code)  # es: "🌧️ Pioggia"
    parts = txt.split(" ", 1)  # ["🌧️", "Pioggia"]
    if mode == "color":
        return txt
    if mode == "off":
        return parts[1] if len(parts) > 1 else txt
    # bw: tieni il simbolo ma senza varianti, così molti terminali lo rendono in b/n
    if len(parts) > 1:
        return strip_variants(parts[0]) + " " + parts[1]
    return strip_variants(txt)
# ---------------------- colori curses ----------------------
def init_colors():
    """Inizializza coppie di colori per linee e riempimenti."""
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    # linee
    curses.init_pair(1, curses.COLOR_YELLOW, -1)  # temperatura
    curses.init_pair(2, curses.COLOR_BLUE,   -1)  # precipitazioni
    curses.init_pair(3, curses.COLOR_CYAN,   -1)  # umidità
    # riempimenti (bg)
    curses.init_pair(11, -1, curses.COLOR_YELLOW)
    curses.init_pair(12, -1, curses.COLOR_BLUE)
    curses.init_pair(13, -1, curses.COLOR_CYAN)

LINE_TEMP, LINE_PREC, LINE_HUM = 1, 2, 3
FILL_TEMP, FILL_PREC, FILL_HUM = 11, 12, 13

# ---------------------- stato app ----------------------
@dataclass
class AppState:
    emoji_mode: str = "bw"       # "off" | "bw" | "color"
    layout: str = "bottom"       # "bottom" | "side"
    city_query: Optional[str] = None
    city_label: str = "—"
    temp_unit: str = "c"         # "c" | "f"
    wind_unit: str = "mph"       # "kmh" | "ms" | "mph" | "kn"
    hours: int = 48              # 6..168
    refresh_sec: int = 2
    refetch_sec: int = 300
    last_fetch_ts: float = 0.0
    data: Optional[dict] = None
    use_emoji: bool = True
    spark_charset: str = "blocks"
    graph_mode: str = "temp_prec"  # "temp_prec" | "prec_hum"

    def clamp_hours(self) -> None:
        self.hours = max(6, min(168, self.hours))

# ---------------------- input linea ----------------------
def prompt_input_line(stdscr, prompt: str) -> Optional[str]:
    max_y, max_x = stdscr.getmaxyx()
    y = max_y - 1
    stdscr.move(y, 0); stdscr.clrtoeol()
    msg = f"{prompt}: "
    stdscr.addstr(y, 0, msg); stdscr.refresh()
    buf: List[str] = []; x = len(msg); curses.curs_set(1)
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

# ---------------------- fetch dati ----------------------
def ensure_data(state: AppState) -> None:
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
        state.city_label = loc.name
        state.last_fetch_ts = now

# ---------------------- layout: header/pannelli ----------------------
def draw_header(stdscr, state: AppState) -> int:
    max_y, max_x = stdscr.getmaxyx()
    line = f" meteocli-tui  ·  {state.city_label}  ·  {time.strftime('%Y-%m-%d %H:%M:%S')} "
    stdscr.addstr(0, 0, line[:max_x].ljust(max_x), curses.A_REVERSE)
    help_line = "[q] quit  [c] città  [g] geo-IP  [u] C/F  [w] vento  [+/-] ore  [t] grafici  [r] refresh  [?] help"
    stdscr.addstr(1, 0, help_line[:max_x].ljust(max_x))
    status = f"emoji:{state.emoji_mode}  graph:{state.graph_mode}  layout:{state.layout}  hours:{state.hours}  wind:{state.wind_unit}"
    stdscr.addstr(2, 0, status[:max_x].ljust(max_x))
    return 3

def safe_get(arr, idx, default=None):
    return arr[idx] if isinstance(arr, list) and idx < len(arr) else default

def draw_current_panel(stdscr, y0: int, x0: int, w: int, state: AppState) -> int:
    stdscr.addstr(y0, x0, "[ Attuale ]"); y = y0 + 1
    if not state.data: stdscr.addstr(y, x0, "Nessun dato."); return y + 1
    H = state.data.get("hourly", {})
    t  = safe_get(H.get("temperature_2m", []), 0)
    rh = safe_get(H.get("relative_humidity_2m", []), 0)
    pp = safe_get(H.get("precipitation_probability", []), 0)
    pr = safe_get(H.get("pressure_msl", []), 0)
    ws = safe_get(H.get("wind_speed_10m", []), 0)
    wc = safe_get(H.get("weather_code", []), 0)
    unit_t = "°C" if state.temp_unit == "c" else "°F"
    rows = [
        ("Meteo",        describe_for_tui(wc, state.emoji_mode)),
        ("Temperatura",  f"{t:.1f} {unit_t}" if t is not None else "—"),
        ("Umidità",      f"{int(rh)}%" if rh is not None else "—"),
        ("Prec. prob.",  f"{int(pp)}%" if pp is not None else "—"),
        ("Vento",        f"{ws:.1f} {state.wind_unit}" if ws is not None else "—"),
        ("Pressione",    f"{int(pr)} hPa" if pr is not None else "—"),
    ]
    for k, v in rows:
        stdscr.addstr(y, x0, f"{k:<15} {v}"[:w]); y += 1
    return y

def draw_forecast_table(stdscr, y0: int, x0: int, w: int, state: AppState) -> int:
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
        tval = safe_get(temps, i); pval = safe_get(probs, i); code = safe_get(codes, i)
        ora = tstr[11:16] if isinstance(tstr, str) and len(tstr) >= 16 else "—"
        line = f"{ora:<5} {(f'{tval:.0f}{unit_t}' if tval is not None else '—'):<6} {(f'{int(pval)}%' if pval is not None else '—'):<6} {describe_for_tui(code, state.emoji_mode)}"
        stdscr.addstr(y, x0, line[:w]); y += 1
    return y

# ---------------------- grafici area colorati ----------------------
def _sample_to_width(values: List[float], width: int) -> List[float]:
    if not values:
        return []
    if len(values) == width:
        return values
    if width <= 1:
        return [values[0]]
    out = []
    for c in range(width):
        idx = round(c * (len(values) - 1) / (width - 1))
        out.append(values[int(idx)])
    return out

def draw_area_chart(
    stdscr, *, x0: int, y0: int, w: int, h: int,
    values: List[float], xlabels: List[str],
    title: str, line_pair: int, fill_pair: int,
    label_every: int = 8
) -> None:
    if w < 10 or h < 6 or not values:
        stdscr.addstr(y0, x0, "(spazio insufficiente)")
        return
    stdscr.addstr(y0, x0, title[:w])
    top = y0 + 1
    height = h - 3
    base_y = top + height - 1
    vs = values[:]
    vmin, vmax = min(vs), max(vs)
    if vmax == vmin:
        vmax = vmin + 1.0
    scale = (height - 1) / (vmax - vmin)
    xs = list(range(w))
    ys = []
    sampled = _sample_to_width(vs, w)
    for v in sampled:
        y = base_y - int(round((v - vmin) * scale))
        ys.append(y)
    # riempimento
    for x, y_line in zip(xs, ys):
        for y in range(y_line + 1, base_y + 1):
            stdscr.addstr(y, x0 + x, " ", curses.color_pair(fill_pair) | curses.A_DIM)
    # linea
    for i, (x, y) in enumerate(zip(xs, ys)):
        stdscr.addstr(y, x0 + x, "─", curses.color_pair(line_pair) | curses.A_BOLD)
        if i > 0:
            y_prev = ys[i - 1]
            step = 1 if y > y_prev else -1
            for yy in range(y_prev, y, step):
                stdscr.addstr(yy, x0 + x, "─", curses.color_pair(line_pair))
    # etichette
    for i, (x, y) in enumerate(zip(xs, ys)):
        if i % max(1, label_every) == 0:
            lab = str(int(round(sampled[i])))
            yy = max(top, y - 1)
            xx = max(x0, min(x0 + w - len(lab), x0 + x - len(lab)//2))
            stdscr.addstr(yy, xx, lab, curses.color_pair(line_pair) | curses.A_BOLD)
    # tick orari
    stdscr.addstr(base_y + 1, x0, " " * w)
    if xlabels:
        step = max(1, len(xlabels) // 8)
        for i in range(0, len(xlabels), step):
            col = int(round(i * (w - 1) / (len(xlabels) - 1))) if len(xlabels) > 1 else 0
            label = xlabels[i][:5]
            xx = max(x0, min(x0 + w - len(label), x0 + col - len(label)//2))
            stdscr.addstr(base_y + 1, xx, label, curses.A_DIM)

def draw_graphs(stdscr, y0: int, x0: int, w: int, hgt: int, state: AppState) -> None:
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
    if w < 24 or hgt < 14:
        stdscr.addstr(y, x0, "(spazio ridotto → grafici compatti)")
        y += 1
        stdscr.addstr(y, x0, "Temp"); y += 1
        stdscr.addstr(y, x0, sparkline(temps, width=max(10, w-2), charset=state.spark_charset)); y += 2
        stdscr.addstr(y, x0, "Prec %"); y += 1
        stdscr.addstr(y, x0, sparkline(probs, width=max(10, w-2), charset=state.spark_charset)); y += 1
        return
    half_h = (hgt - 1) // 2
    unit_t = "°C" if state.temp_unit == "c" else "°F"
    if state.graph_mode == "temp_prec":
        draw_area_chart(
            stdscr, x0=x0, y0=y, w=w, h=half_h,
            values=temps, xlabels=xlabels,
            title=f"Temperatura oraria ({unit_t})",
            line_pair=LINE_TEMP, fill_pair=FILL_TEMP, label_every=8
        )
        y += half_h
        draw_area_chart(
            stdscr, x0=x0, y0=y, w=w, h=hgt - (y - (y0 + 1)),
            values=probs, xlabels=xlabels,
            title="Probabilità precipitazioni (%)",
            line_pair=LINE_PREC, fill_pair=FILL_PREC, label_every=8
        )
    else:
        draw_area_chart(
            stdscr, x0=x0, y0=y, w=w, h=half_h,
            values=probs, xlabels=xlabels,
            title="Probabilità precipitazioni (%)",
            line_pair=LINE_PREC, fill_pair=FILL_PREC, label_every=8
        )
        y += half_h
        draw_area_chart(
            stdscr, x0=x0, y0=y, w=w, h=hgt - (y - (y0 + 1)),
            values=hums, xlabels=xlabels,
            title="Umidità (%)",
            line_pair=LINE_HUM, fill_pair=FILL_HUM, label_every=8
        )

# ---------------------- help overlay ----------------------
def draw_help_overlay(stdscr) -> None:
    max_y, max_x = stdscr.getmaxyx()
    help_text = """
    Tasti:
      q / ESC — Esci
      c       — Cambia città (Enter=OK, ESC=annulla)
      g       — Geo-IP (annulla override città)
      u       — °C/°F
      w       — Vento: mph → kmh → ms → kn
      + / -   — Aumenta / diminuisci ore (6..168)
      t       — Cambia grafici (Temp+Prec ↔ Prec+Umidità)
      r       — Ridisegna subito
      ? / h   — Mostra/Nascondi aiuto
    """.strip("\n")
    box_w = min(86, max_x - 4)
    lines = []
    for para in help_text.split("\n"):
        lines.extend(textwrap.wrap(para, width=box_w - 4) or [""])
    box_h = min(len(lines) + 4, max_y - 4)
    y0 = (max_y - box_h)//2; x0 = (max_x - box_w)//2
    for i in range(box_h):
        stdscr.addstr(y0 + i, x0, " " * box_w, curses.A_REVERSE if i in (0, box_h-1) else 0)
    stdscr.addstr(y0, x0 + 2, " Aiuto ")
    y = y0 + 2
    for line in lines[: box_h - 3]:
        stdscr.addstr(y, x0 + 2, line.ljust(box_w - 4)); y += 1

# ---------------------- main loop curses ----------------------
def run(stdscr, state: AppState) -> int:
    curses.curs_set(0); stdscr.nodelay(True)
    stdscr.timeout(state.refresh_sec * 1000); curses.use_default_colors()
    init_colors()
    show_help = False
    while True:
        ensure_data(state)
        stdscr.erase()
        y = draw_header(stdscr, state)
        max_y, max_x = stdscr.getmaxyx()
        body_h = max_y - y - 1

        if state.layout == "bottom":
           # fascia alta: due pannelli affiancati (attuale + tabella)
           top_h  = max(10, body_h // 2)              # metà schermo circa
           left_w = min(40, max(28, max_x // 3))      # pannello attuale stretto
           right_w = max_x - left_w - 2

           draw_current_panel(stdscr, y,            0,        left_w - 1, state)
           draw_forecast_table(stdscr, y, left_w + 2,        right_w - 1, state)

           # fascia bassa: GRAFICI a tutta larghezza
           draw_graphs(stdscr, y + top_h, 0, max_x - 1, body_h - top_h, state)
        else:
           # layout "side" (quello vecchio): grafici a destra
            col = max(24, max_x // 3)
            left_x, mid_x, right_x = 0, col + 1, (col * 2) + 2
            right_w = max_x - right_x - 1

            draw_current_panel(stdscr, y, left_x, col - 2, state)
            draw_forecast_table(stdscr, y, mid_x,  col - 2, state)
            draw_graphs(stdscr, y, right_x, right_w, body_h, state)

        if show_help: draw_help_overlay(stdscr)
        stdscr.refresh()
        try:
            ch = stdscr.get_wch()
        except curses.error:
            ch = None
        if ch is None:
            continue
        key = ch.lower() if isinstance(ch, str) else ch
        if key in ("q", "\x1b"): return 0
        elif key == "r": pass
        elif key == "u":
            state.temp_unit = "f" if state.temp_unit == "c" else "c"; state.last_fetch_ts = 0.0
        elif key == "w":
            order = ["mph", "kmh", "ms", "kn"]
            state.wind_unit = order[(order.index(state.wind_unit)+1) % len(order)]; state.last_fetch_ts = 0.0
        elif key == "+":
            state.hours += 6; state.clamp_hours(); state.last_fetch_ts = 0.0
        elif key == "-":
            state.hours -= 6; state.clamp_hours(); state.last_fetch_ts = 0.0
        elif key == "t":
            state.graph_mode = "prec_hum" if state.graph_mode == "temp_prec" else "temp_prec"
        elif key == "g":
            state.city_query = None; state.last_fetch_ts = 0.0
        elif key == "c":
            new_city = prompt_input_line(stdscr, "Nuova città (Enter=OK, ESC=annulla)")
            if new_city is not None:
                state.city_query = new_city.strip() or None; state.last_fetch_ts = 0.0
        elif key == "e":
            order = ["off", "bw", "color"]
            state.emoji_mode = order[(order.index(state.emoji_mode) + 1) % len(order)]
        elif key == "o":
            state.layout = "side" if state.layout == "bottom" else "bottom"

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
