from __future__ import annotations
import time
import argparse
from typing import List, Optional

from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from .cli import (
    ip_location,
    geocode_city,
    fetch_weather,
    describe_code,
)
import meteocli.cli as _cli_mod

console = Console()

def sparkline(values: List[float], width: int = 40) -> str:
    """Mini-grafico orizzontale tipo '▁▂▃▄▅▆▇█'."""
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    n = len(values)
    if n > width:
        step = n / width
        values = [values[int(i * step)] for i in range(width)]
    vmin = min(values)
    vmax = max(values)
    if vmax == vmin:
        return blocks[len(blocks) // 2] * len(values)
    rng = vmax - vmin
    out = []
    for v in values:
        idx = int(round((v - vmin) / rng * (len(blocks) - 1)))
        idx = max(0, min(idx, len(blocks) - 1))
        out.append(blocks[idx])
    return "".join(out)

def build_layout(city_label: str, data: dict, hours: int, temp_unit: str, wind_unit: str) -> Layout:
    h = data.get("hourly", {})
    times = h.get("time", [])[:hours]
    temps = h.get("temperature_2m", [])[:hours]
    probs = h.get("precipitation_probability", [])[:hours]
    hums  = h.get("relative_humidity_2m", [])[:hours]
    press = h.get("pressure_msl", [])[:hours]
    wind  = h.get("wind_speed_10m", [])[:hours]
    codes = h.get("weather_code", [])[:hours]

    def safe(arr, i, default=None):
        return arr[i] if isinstance(arr, list) and i < len(arr) else default

    unit_t = "°C" if temp_unit == "c" else "°F"
    now_t  = safe(temps, 0)
    now_p  = safe(probs, 0)
    now_h  = safe(hums,  0)
    now_pr = safe(press, 0)
    now_w  = safe(wind,  0)
    now_wc = safe(codes, 0)

    header = Panel.fit(
        Text.assemble(
            (" meteocli-top ", "reverse bold"),
            (f"  ·  {city_label}  ·  ", "bold"),
            (time.strftime("%Y-%m-%d %H:%M:%S"), "dim"),
        ),
        box=box.SQUARE, padding=(0, 1)
    )

    t_now = Table.grid(padding=(0, 2))
    t_now.add_column(justify="left")
    t_now.add_column(justify="right")
    t_now.add_row("Meteo", describe_code(now_wc))
    t_now.add_row("Temperatura", f"{now_t:.1f} {unit_t}" if now_t is not None else "—")
    t_now.add_row("Umidità",     f"{int(now_h)}%" if now_h is not None else "—")
    t_now.add_row("Prec. prob.", f"{int(now_p)}%" if now_p is not None else "—")
    t_now.add_row("Vento",       f"{now_w:.1f} {wind_unit}" if now_w is not None else "—")
    t_now.add_row("Pressione",   f"{int(now_pr)} hPa" if now_pr is not None else "—")
    panel_now = Panel(t_now, title="Attuale", box=box.SIMPLE_HEAVY)

    table_fc = Table(title="Prossime 12 ore (step 3h)", box=box.SIMPLE)
    table_fc.add_column("Ora")
    table_fc.add_column("Temp")
    table_fc.add_column("Prec %", justify="right")
    table_fc.add_column("Meteo")
    for i in range(0, min(len(times), 12), 3):
        tstr = times[i][11:16] if i < len(times) else "—"
        tval = safe(temps, i)
        pval = safe(probs, i)
        code = safe(codes, i)
        table_fc.add_row(
            tstr,
            f"{tval:.0f}{unit_t}" if tval is not None else "—",
            f"{int(pval)}%" if pval is not None else "—",
            describe_code(code),
        )
    panel_fc = Panel(table_fc, box=box.SIMPLE)

    s_temp = sparkline([v for v in temps if v is not None], width=50)
    s_prec = sparkline([v for v in probs if v is not None], width=50)
    grid_graph = Table.grid(padding=1)
    grid_graph.add_column()
    grid_graph.add_row(Text(f"Temperatura ({unit_t})", style="bold"))
    grid_graph.add_row(Text(s_temp))
    grid_graph.add_row(Text("Probabilità precipitazioni (%)", style="bold"))
    grid_graph.add_row(Text(s_prec))
    panel_graph = Panel(grid_graph, title=f"Grafici (ultime {min(hours, len(times))} ore)", box=box.SIMPLE)

    layout = Layout()
    layout.split(
        Layout(header, name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(Text("Ctrl+C per uscire  •  Aggiornamento automatico", style="dim"), name="footer", size=1),
    )
    layout["body"].split_row(
        Layout(panel_now,   name="left",  ratio=1),
        Layout(panel_fc,    name="mid",   ratio=2),
        Layout(panel_graph, name="right", ratio=2),
    )
    return layout


def parse_args():
    p = argparse.ArgumentParser(
        description=(
            "Meteo in terminale. Default: stampa snapshot e torna al prompt.\n"
            "Usa --watch per la modalità fullscreen con auto-refresh (stile htop)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--city", "-c", help="Città (se omesso, rilevamento IP)")
    p.add_argument("--hours", "-H", type=int, default=48, help="Ore di previsione (max 168)")
    p.add_argument("--temp-unit", choices=["c", "f"], default="c", help="Unità temperatura")
    p.add_argument("--wind-unit", choices=["kmh", "ms", "mph", "kn"], default="mph", help="Unità vento")
    p.add_argument("--no-emoji", action="store_true",
                   help="Sostituisce emoji con simboli ASCII (consigliato via SSH)")
    p.add_argument("--watch", "-w", action="store_true",
                   help="Modalità continua fullscreen (Ctrl+C per uscire)")
    p.add_argument("--refresh", "-r", type=int, default=30,
                   help="(--watch) Secondi tra refresh schermo")
    p.add_argument("--refetch", type=int, default=300,
                   help="(--watch) Secondi tra nuovo fetch meteo da API")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    hours = max(6, min(168, args.hours))

    if args.no_emoji:
        _cli_mod._USE_EMOJI = False

    loc = geocode_city(args.city) if args.city else ip_location()
    if not loc:
        loc = geocode_city("Roma")
    if not loc:
        console.print("[red]Impossibile determinare la posizione. Usa --city[/red]")
        return 2

    if not args.watch:
        # ── SNAPSHOT: stampa e torna al prompt ────────────────────────
        try:
            data = fetch_weather(loc, hours, args.temp_unit, args.wind_unit)
        except Exception as e:
            console.print(f"[red]Errore rete:[/red] {e}")
            return 3
        layout = build_layout(loc.name, data, hours, args.temp_unit, args.wind_unit)
        console.print(layout)
        console.print("[dim]Dati: Open‑Meteo • IP‑API  –  usa --watch per aggiornamento continuo[/dim]")
        return 0

    # ── WATCH: fullscreen con auto-refresh ────────────────────────────
    data = None
    last_fetch = 0.0
    with Live(refresh_per_second=max(1, 60 // max(1, args.refresh)), console=console, screen=True):
        while True:
            now = time.time()
            if data is None or now - last_fetch >= args.refetch:
                try:
                    data = fetch_weather(loc, hours, args.temp_unit, args.wind_unit)
                    last_fetch = now
                except Exception as e:
                    console.print(f"[red]Errore rete:[/red] {e}")
                    time.sleep(args.refresh)
                    continue
            layout = build_layout(loc.name, data, hours, args.temp_unit, args.wind_unit)
            console.print(layout, end="")
            time.sleep(args.refresh)
            console.clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
