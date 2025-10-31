from __future__ import annotations
import sys
import argparse
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List
import requests
from rich.console import Console
from rich.table import Table
from rich import box
import plotext as plt

console = Console()

WEATHER_CODE: Dict[int, Tuple[str, str]] = {
0: ("Cielo sereno", "☀️"),
1: ("Prevalenza sereno", "🌤️"),
2: ("Parzialmente nuvoloso", "⛅"),
3: ("Coperto", "☁️"),
45: ("Nebbia", "🌫️"), 48: ("Nebbia gelante", "🌫️"),
51: ("Pioviggine leggera", "🌦️"), 53: ("Pioviggine", "🌦️"), 55: ("Pioviggine intensa", "🌧️"),
56: ("Pioggerella gelata leggera", "🌧️"), 57: ("Pioggerella gelata", "🌧️"),
61: ("Pioggia debole", "🌦️"), 63: ("Pioggia", "🌧️"), 65: ("Pioggia forte", "⛈️"),
66: ("Pioggia gelata debole", "🌧️"), 67: ("Pioggia gelata", "🌧️"),
71: ("Neve debole", "🌨️"), 73: ("Neve", "❄️"), 75: ("Neve forte", "❄️"),
77: ("Granuli di neve", "🌨️"),
80: ("Rovesci deboli", "🌦️"), 81: ("Rovesci", "🌧️"), 82: ("Rovesci forti", "⛈️"),
85: ("Rovesci di neve deboli", "🌨️"), 86: ("Rovesci di neve", "❄️"),
95: ("Temporale", "⛈️"), 96: ("Temporale con grandine leggera", "⛈️"), 99: ("Temporale con grandine", "⛈️"),
}

@dataclass
class Location:
    name: str
    lat: float
    lon: float




def ip_location() -> Optional[Location]:
    try:
        r = requests.get("http://ip-api.com/json", timeout=6)
        r.raise_for_status()
        j = r.json()
        if j.get("status") == "success":
            city = j.get("city") or "Località"
            return Location(city, float(j["lat"]), float(j["lon"]))
    except Exception:
        return None
    return None




def geocode_city(name: str) -> Optional[Location]:
    try:
        url = "https://geocoding-api.open-meteo.com/v1/search"
        params = {"name": name, "count": 1, "language": "it", "format": "json"}
        r = requests.get(url, params=params, timeout=6)
        r.raise_for_status()
        j = r.json()
        results = j.get("results") or []
        if results:
            top = results[0]
            label = ", ".join(
                [p for p in [top.get("name"), top.get("admin1"), top.get("country")] if p]
            )
            return Location(label, float(top["latitude"]), float(top["longitude"]))
    except Exception:
        return None
    return None

def fetch_weather(loc: Location, hours: int, temp_unit: str, wind_unit: str) -> dict:
    forecast_days = min(7, max(1, (hours + 23) // 24))
    url = "https://api.open-meteo.com/v1/forecast"
    hourly_vars = [
        "temperature_2m",
        "relative_humidity_2m",
        "precipitation_probability",
        "pressure_msl",
        "wind_speed_10m",
        "weather_code",
    ]
    params = {
        "latitude": loc.lat,
        "longitude": loc.lon,
        "timezone": "auto",
        "hourly": ",".join(hourly_vars),
        "temperature_unit": "celsius" if temp_unit == "c" else "fahrenheit",
        "windspeed_unit": wind_unit,
        "forecast_days": forecast_days,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()




def _nearest_hour_index(iso_times: List[str]) -> int:
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%dT%H:00")
    try:
        return iso_times.index(now)
    except ValueError:
        fmt = "%Y-%m-%dT%H:00"
        try:
            now_dt = datetime.strptime(now, fmt)
            dts = [datetime.strptime(t, fmt) for t in iso_times]
            diffs = [abs((dt - now_dt).total_seconds()) for dt in dts]
            return int(diffs.index(min(diffs)))
        except Exception:
            return 0




def describe_code(code: int | None) -> str:
    if code is None:
        return "—"
    desc, emoji = WEATHER_CODE.get(int(code), ("N/D", ""))
    return f"{emoji} {desc}".strip()

def render_current(loc: Location, data: dict, temp_unit: str, wind_unit: str) -> None:
    h = data.get("hourly", {})
    times = h.get("time", [])
    idx = _nearest_hour_index(times)


    def get(key: str):
        arr = h.get(key)
        return arr[idx] if isinstance(arr, list) and idx < len(arr) else None


    t = get("temperature_2m")
    rh = get("relative_humidity_2m")
    pp = get("precipitation_probability")
    pr = get("pressure_msl")
    ws = get("wind_speed_10m")
    wc = get("weather_code")


    unit_t = "°C" if temp_unit == "c" else "°F"


    table = Table(title=f"Meteo attuale — {loc.name}", box=box.SIMPLE_HEAVY)
    table.add_column("Parametro")
    table.add_column("Valore", justify="right")
    table.add_row("Meteo", describe_code(wc))
    table.add_row("Temperatura", f"{t:.1f} {unit_t}" if t is not None else "—")
    table.add_row("Umidità", f"{int(rh)}%" if rh is not None else "—")
    table.add_row("Prob. precipitazioni", f"{int(pp)}%" if pp is not None else "—")
    table.add_row("Vento", f"{ws:.1f} {wind_unit}" if ws is not None else "—")
    table.add_row("Pressione", f"{int(pr)} hPa" if pr is not None else "—")
    console.print(table)




def render_forecast(data: dict, temp_unit: str, hours: int) -> None:
    h = data.get("hourly", {})
    times = h.get("time", [])[:hours]
    temps = h.get("temperature_2m", [])[:hours]
    probs = h.get("precipitation_probability", [])[:hours]


    from rich.table import Table
    from rich import box
    table = Table(title="Prossime ore", box=box.SIMPLE)
    table.add_column("Ora")
    table.add_column("Temp")
    table.add_column("Prec %", justify="right")
    table.add_column("Meteo")


    unit_t = "°C" if temp_unit == "c" else "°F"


    for i in range(0, min(len(times), 12), 3):
        tstr = times[i][11:16]
        tval = temps[i] if i < len(temps) else None
        pval = probs[i] if i < len(probs) else None
        code = h.get("weather_code", [None]*len(times))[i]
        table.add_row(
            tstr,
            f"{tval:.0f}{unit_t}" if tval is not None else "—",
            f"{int(pval)}%" if pval is not None else "—",
            describe_code(code),
        )
        console.print(table)


        if len(times) >= 4 and len(temps) == len(times):
            xticks_idx = list(range(0, len(times), max(1, len(times)//10)))
            xticks_lbl = [times[i][11:16] for i in xticks_idx]


            plt.clear_figure();
            plt.title("Temperatura oraria")
            plt.xlabel("Ora"); plt.ylabel(f"Temp ({unit_t})")
            plt.plot(temps); plt.xticks(xticks_idx, xticks_lbl); plt.show()


            if probs and len(probs) == len(times):
                plt.clear_figure();
                plt.title("Probabilità precipitazioni")
                plt.xlabel("Ora"); plt.ylabel("%")
                plt.plot(probs); plt.xticks(xticks_idx, xticks_lbl); plt.show()




def parse_args(argv: List[str]):
    p = argparse.ArgumentParser(
    description="CLI meteo (Open‑Meteo) con grafici ASCII",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--city", "-c", help="Città (se omesso, rilevamento IP)")
    p.add_argument("--hours", "-H", type=int, default=48, help="Ore di previsione (max 168)")
    p.add_argument("--temp-unit", choices=["c", "f"], default="c", help="Unità temperatura")
    p.add_argument("--wind-unit", choices=["kmh", "ms", "mph", "kn"], default="mph", help="Unità vento")
    return p.parse_args(argv)




def main(argv: List[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    hours = max(1, min(168, args.hours))


    loc = geocode_city(args.city) if args.city else ip_location()
    if not loc:
        loc = geocode_city("Roma")
    if not loc:
        console.print("[red]Impossibile determinare la posizione. Usa --city[/red]")
        return 2


    try:
        data = fetch_weather(loc, hours, args.temp_unit, args.wind_unit)
    except requests.RequestException as e:
        console.print(f"[red]Errore rete:[/red] {e}")
        return 3


    render_current(loc, data, args.temp_unit, args.wind_unit)
    console.print()
    render_forecast(data, args.temp_unit, hours)
    console.print("[dim]Dati: Open‑Meteo • IP‑API[/dim]")
    return 0




if __name__ == "__main__":
    raise SystemExit(main())   