"""Static HTML dashboard: equity and drawdown charts, stat tiles, positions, fills, events.

One self-contained file, no external assets, light and dark mode, hover crosshair.
"""

import html
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from tbot.backtest.engine import BacktestResult
from tbot.core.models import Fill
from tbot.data.store import BarStore
from tbot.live.config import SessionConfig
from tbot.live.ledger import Event, Ledger
from tbot.live.runner import load_guard, restore_portfolio, stream_keys

MAX_POINTS = 1500  # downsample longer series; the tooltip reads the drawn points
WIDTH, HEIGHT = 880, 260
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 64, 16, 12, 28


@dataclass(frozen=True)
class DashboardData:
    title: str
    subtitle: str
    times: list[datetime]
    equity: list[float]
    initial: float
    positions: list[tuple[str, float, float | None]]  # symbol, quantity, mark price
    fills: list[Fill] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def drawdowns(equity: Sequence[float], initial: float) -> list[float]:
    """Fraction below the running peak (including the initial equity), as negatives."""
    peak = initial
    out = []
    for value in equity:
        peak = max(peak, value)
        out.append(value / peak - 1 if peak > 0 else 0.0)
    return out


def downsample[T](values: Sequence[T], limit: int = MAX_POINTS) -> list[T]:
    """Every k-th point plus the last one, so the end of the series is always drawn."""
    if len(values) <= limit:
        return list(values)
    step = math.ceil(len(values) / limit)
    kept = list(values[::step])
    if (len(values) - 1) % step:
        kept.append(values[-1])
    return kept


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Round tick values (1, 2, 5 times a power of ten) covering [low, high]."""
    if not (math.isfinite(low) and math.isfinite(high)):
        return [0.0, 1.0]
    if high <= low:
        high = low + 1
    raw = (high - low) / max(count - 1, 1)
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 5, 10) if m * power >= raw)
    ticks = []
    value = math.floor(low / step) * step
    while value < high - 1e-12:
        ticks.append(round(value, 10))
        value += step
    ticks.append(round(value, 10))  # the first tick at or above high, so the top is covered
    return ticks


def _fmt_money(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:,.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value:+.1%}"


def _line_chart(
    chart_id: str, times: Sequence[datetime], values: Sequence[float], *, percent: bool
) -> str:
    """SVG line with a 10% area wash, recessive gridlines, and clean ticks."""
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return '<p class="muted">no data</p>'
    low, high = min(finite), max(finite)
    if percent:
        low, high = min(low, 0.0), max(high, 0.0)
        if high - low < 0.05:  # no drawdown yet: a small span, not 0% to 100%
            low = high - 0.05
    ticks = nice_ticks(low, high)
    y_min, y_max = ticks[0], ticks[-1]
    x0, x1 = PAD_LEFT, WIDTH - PAD_RIGHT
    y0, y1 = HEIGHT - PAD_BOTTOM, PAD_TOP
    n = len(values)

    def sx(i: int) -> float:
        return x0 + (x1 - x0) * (i / (n - 1) if n > 1 else 0.5)

    def sy(v: float) -> float:
        return y0 - (y0 - y1) * (v - y_min) / (y_max - y_min)

    points = [(sx(i), sy(v)) for i, v in enumerate(values)]
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points)
    baseline = sy(0.0) if percent else y0
    area = f"{path} L{points[-1][0]:.1f},{baseline:.1f} L{points[0][0]:.1f},{baseline:.1f} Z"
    grid = "".join(
        f'<line class="grid" x1="{x0}" x2="{x1}" y1="{sy(t):.1f}" y2="{sy(t):.1f}"/>'
        f'<text class="tick" x="{x0 - 8}" y="{sy(t) + 4:.1f}" text-anchor="end">'
        f"{(f'{t:.0%}' if percent else f'{t:,.0f}')}</text>"
        for t in ticks
    )
    label_count = min(6, n)
    x_labels = "".join(
        f'<text class="tick" x="{sx(i):.1f}" y="{HEIGHT - 8}" text-anchor="middle">'
        f"{times[i]:%Y-%m-%d}</text>"
        for i in sorted({round(k * (n - 1) / max(label_count - 1, 1)) for k in range(label_count)})
    )
    end_x, end_y = points[-1]
    return (
        f'<svg class="chart" id="{chart_id}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-label="{chart_id} over time">'
        f"{grid}{x_labels}"
        f'<line class="axis" x1="{x0}" x2="{x1}" y1="{baseline:.1f}" y2="{baseline:.1f}"/>'
        f'<path class="area" d="{area}"/><path class="line" d="{path}"/>'
        f'<circle class="end" cx="{end_x:.1f}" cy="{end_y:.1f}" r="4"/>'
        f'<line class="crosshair" x1="0" x2="0" y1="{y1}" y2="{y0}" visibility="hidden"/>'
        f'<circle class="focus" r="4" visibility="hidden"/>'
        f'<rect class="hit" x="{x0}" y="{y1}" width="{x1 - x0}" height="{y0 - y1}" '
        f'fill="transparent"/>'
        "</svg>"
    )


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{html.escape(empty)}</p>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _tile(label: str, value: str, delta_class: str = "") -> str:
    return (
        f'<div class="tile"><div class="label">{html.escape(label)}</div>'
        f'<div class="value {delta_class}">{html.escape(value)}</div></div>'
    )


def render(data: DashboardData) -> str:
    times = downsample(data.times)
    equity = downsample(data.equity)
    dd = downsample(drawdowns(data.equity, data.initial))  # peaks between kept points count
    final = data.equity[-1] if data.equity else data.initial
    total_return = final / data.initial - 1 if data.initial else 0.0
    max_dd = min(drawdowns(data.equity, data.initial), default=0.0)
    exposure_value = sum(q * (m or 0.0) for _, q, m in data.positions)
    exposure = exposure_value / final if final > 0 else 0.0

    labels = [t.strftime("%Y-%m-%d %H:%M") for t in times]
    equity_points = [round(v, 2) for v in equity]
    drawdown_points = [round(v, 4) for v in dd]
    series = {"times": labels, "equity": equity_points, "drawdown": drawdown_points}
    tiles = "".join(
        [
            _tile("Equity", _fmt_money(final)),
            _tile("Total return", _fmt_pct(total_return), "up" if total_return >= 0 else "down"),
            _tile("Max drawdown", _fmt_pct(max_dd), "down" if max_dd < 0 else ""),
            _tile("Exposure", f"{exposure:.0%}"),
            _tile("Fills", f"{len(data.fills):,}"),
        ]
    )
    positions = _table(
        ["Symbol", "Quantity", "Mark", "Value"],
        [
            (s, f"{q:.6f}", _fmt_money(m) if m else "-", _fmt_money(q * m) if m else "-")
            for s, q, m in data.positions
        ],
        "no open positions",
    )
    fills = _table(
        ["Time (UTC)", "Side", "Symbol", "Quantity", "Price", "Fee"],
        [
            (
                f"{f.time:%Y-%m-%d %H:%M}",
                "BUY" if f.quantity > 0 else "SELL",
                f.symbol,
                f"{abs(f.quantity):.6f}",
                _fmt_money(f.price),
                f"{f.fee:.2f}",
            )
            for f in data.fills[-20:][::-1]
        ],
        "no fills yet",
    )
    events = _table(
        ["Time (UTC)", "Level", "Message"],
        [(f"{e.time:%Y-%m-%d %H:%M}", e.level, e.message) for e in data.events[-20:][::-1]],
        "no events",
    )
    equity_rows = [
        (t, f"{v:,.2f}", f"{d:.2%}")
        for t, v, d in zip(labels, equity_points, drawdown_points, strict=True)
    ]
    notes = "".join(f'<p class="note">{html.escape(n)}</p>' for n in data.notes)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(data.title)}</title>
<style>
:root {{
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #e34948; --up: #006300; --down: #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #e66767; --up: #0ca30c; --down: #e66767;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #e66767; --up: #0ca30c; --down: #e66767;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 24px 16px; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 960px; margin: 0 auto; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
h2 {{ font-size: 15px; margin: 0 0 8px; }}
.subtitle, .muted, .note {{ color: var(--ink-2); margin: 0 0 16px; }}
.note {{ font-weight: 600; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px;
  margin: 16px 0; }}
.tile {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 14px; }}
.tile .label {{ color: var(--ink-2); font-size: 13px; }}
.tile .value {{ font-size: 24px; font-weight: 600; margin-top: 2px; }}
.tile .value.up {{ color: var(--up); }}
.tile .value.down {{ color: var(--down); }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px; margin: 0 0 16px; position: relative; }}
.chart {{ width: 100%; height: auto; display: block; }}
.chart .grid {{ stroke: var(--grid); stroke-width: 1; }}
.chart .axis {{ stroke: var(--axis); stroke-width: 1; }}
.chart .tick {{ fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }}
.chart .line {{ fill: none; stroke: var(--series-1); stroke-width: 2; stroke-linejoin: round;
  stroke-linecap: round; }}
.chart .area {{ fill: var(--series-1); opacity: 0.1; }}
.chart .end, .chart .focus {{ fill: var(--series-1); stroke: var(--surface); stroke-width: 2; }}
.chart .crosshair {{ stroke: var(--axis); stroke-width: 1; }}
#drawdown .line, #drawdown .end, #drawdown .focus {{ stroke: var(--series-2); }}
#drawdown .line {{ fill: none; }}
#drawdown .area, #drawdown .end, #drawdown .focus {{ fill: var(--series-2); }}
.tooltip {{ position: absolute; pointer-events: none; background: var(--surface);
  border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 13px;
  visibility: hidden; white-space: nowrap; box-shadow: 0 2px 8px rgba(0,0,0,0.12); }}
.tooltip strong {{ font-size: 15px; }}
.tooltip span {{ color: var(--ink-2); margin-left: 6px; }}
table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); }}
th {{ color: var(--ink-2); font-weight: 600; font-size: 13px; }}
td:nth-child(n+2), th:nth-child(n+2) {{ text-align: right; }}
#events td, #events th {{ text-align: left; }}
details summary {{ cursor: pointer; color: var(--ink-2); }}
</style>
</head>
<body>
<main>
<h1>{html.escape(data.title)}</h1>
<p class="subtitle">{html.escape(data.subtitle)}</p>
{notes}
<div class="tiles">{tiles}</div>
<div class="card"><h2>Equity</h2>{_line_chart("equity", times, equity, percent=False)}
<div class="tooltip" id="equity-tip"></div></div>
<div class="card"><h2>Drawdown from peak</h2>{_line_chart("drawdown", times, dd, percent=True)}
<div class="tooltip" id="drawdown-tip"></div></div>
<div class="card"><h2>Open positions</h2>{positions}</div>
<div class="card"><h2>Recent fills</h2>{fills}</div>
<div class="card" id="events"><h2>Recent events</h2>{events}</div>
<div class="card"><details><summary>Equity table ({len(equity_rows)} points)</summary>
{_table(["Time (UTC)", "Equity", "Drawdown"], equity_rows, "no data")}</details></div>
</main>
<script id="series" type="application/json">{json.dumps(series)}</script>
<script>
(function () {{
  var series = JSON.parse(document.getElementById("series").textContent);
  var n = series.times.length;
  function wire(id, key, format) {{
    var svg = document.getElementById(id);
    var tip = document.getElementById(id + "-tip");
    if (!svg || !tip || n === 0) return;
    var hit = svg.querySelector(".hit"), cross = svg.querySelector(".crosshair");
    var focus = svg.querySelector(".focus"), line = svg.querySelector(".line");
    var x0 = {PAD_LEFT}, x1 = {WIDTH - PAD_RIGHT};
    var value = document.createElement("strong"), label = document.createElement("span");
    tip.appendChild(value); tip.appendChild(label);
    function pointAt(i) {{
      var d = line.getAttribute("d").slice(1).split(" L")[i].split(",");
      return [parseFloat(d[0]), parseFloat(d[1])];
    }}
    function show(event) {{
      var rect = svg.getBoundingClientRect();
      var x = (event.clientX - rect.left) * {WIDTH} / rect.width;
      var i = Math.round((x - x0) / (x1 - x0) * (n - 1));
      i = Math.max(0, Math.min(n - 1, i));
      var p = pointAt(i);
      cross.setAttribute("x1", p[0]); cross.setAttribute("x2", p[0]);
      cross.setAttribute("visibility", "visible");
      focus.setAttribute("cx", p[0]); focus.setAttribute("cy", p[1]);
      focus.setAttribute("visibility", "visible");
      value.textContent = format(series[key][i]);
      label.textContent = series.times[i];
      tip.style.visibility = "visible";
      var left = p[0] * rect.width / {WIDTH};
      tip.style.left = Math.min(left + 12, rect.width - tip.offsetWidth - 8) + "px";
      tip.style.top = (p[1] * rect.height / {HEIGHT} - 40) + "px";
    }}
    function hide() {{
      cross.setAttribute("visibility", "hidden"); focus.setAttribute("visibility", "hidden");
      tip.style.visibility = "hidden";
    }}
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", hide);
  }}
  wire("equity", "equity", function (v) {{
    return v.toLocaleString(undefined, {{maximumFractionDigits: 2}});
  }});
  wire("drawdown", "drawdown", function (v) {{ return (v * 100).toFixed(1) + "%"; }});
}})();
</script>
</body>
</html>
"""


def from_ledger(config: SessionConfig, store: BarStore, title: str) -> DashboardData:
    ledger = Ledger(config.ledger)
    try:
        portfolio = restore_portfolio(config, ledger)
        guard = load_guard(config, ledger)
        rows = ledger.conn.execute("SELECT time, equity FROM equity ORDER BY id").fetchall()
        times = [datetime.fromisoformat(t) for t, _ in rows]
        equity = [float(v) for _, v in rows]
        marks = {}
        for symbol, timeframe in stream_keys(config):
            bars = store.read(symbol, timeframe)
            marks[symbol] = float(bars["close"][-1]) if not bars.is_empty() else None
        positions = [
            (symbol, qty, marks.get(symbol)) for symbol, qty in sorted(portfolio.positions.items())
        ]
        notes = []
        if guard.state.halted:
            notes.append(f"HALTED: {guard.state.halt_reason} (run `tbot resume`)")
        created = ledger.get_meta("created_at") or "-"
        return DashboardData(
            title=title,
            subtitle=f"ledger {config.ledger}, created {created[:19]}, "
            f"{len(portfolio.fills)} fills, {len(ledger.adjustments())} adjustments",
            times=times,
            equity=equity,
            # Adjustments move money in or out of the book; then the first snapshot is the base.
            initial=equity[0] if equity and ledger.adjustments() else config.initial_cash,
            positions=positions,
            fills=ledger.fills(),
            events=ledger.recent_events(20),
            notes=notes,
        )
    finally:
        ledger.close()


def from_backtest(result: BacktestResult, title: str, subtitle: str) -> DashboardData:
    frame = result.equity
    times = list(frame["time"])
    fills = [
        Fill(t, s, q, p, f)
        for t, s, q, p, f in result.fills.select(
            "time", "symbol", "quantity", "price", "fee"
        ).rows()
    ]
    last_fill: dict[str, float] = {}
    for fill in reversed(fills):
        last_fill.setdefault(fill.symbol, fill.price)
    positions = [
        (s, q, result.marks.get(s, last_fill.get(s))) for s, q in sorted(result.positions.items())
    ]
    return DashboardData(
        title=title,
        subtitle=subtitle,
        times=times,
        equity=[float(v) for v in frame["equity"]],
        initial=result.initial_cash,
        positions=positions,
        fills=fills,
    )
