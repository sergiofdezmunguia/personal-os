"""Panel «Pulso del mes»: una página HTML autocontenida con el análisis de gasto.

- Se genera en el PC tras cada `pos sync` y con `pos panel` (que además la abre en el
  navegador de Windows). Se escribe en `<buzón de Scriptable>/panel/index.html`: el iPhone la
  abre con el script de Scriptable «Personal OS Panel» (visor con JavaScript) o desde Archivos.
- **Legible sin JavaScript** (la vista previa de Archivos de iOS no lo ejecuta): los gráficos
  son SVG generados aquí, las cifras van escritas y el desglose usa `<details>`. El
  JavaScript solo añade mejoras (tooltips al tocar, buscar movimientos).
- Sin recursos externos: ni fuentes, ni scripts, ni imágenes de terceros. Los datos no salen
  del PC ni de iCloud Drive.
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from personal_os.finance import insights
from personal_os.finance.insights import CUT_SHARE, Panel
from personal_os.finance.models import fmt_eur

MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
MONTHS_LONG = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
    "octubre", "noviembre", "diciembre",
]  # fmt: skip
INDEX = "index.html"


# --------------------------------------------------------------------------- formato


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def eur(cents: int, *, sign: bool = False) -> str:
    """12345 → '123 €' (sin céntimos, separador de miles español)."""
    value = round(cents / 100)
    text = f"{abs(value):,}".replace(",", ".") + " €"
    if value < 0:
        return "−" + text
    return ("+" + text) if sign and value > 0 else text


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f} %"


def month_label(month: str, *, long: bool = False) -> str:
    y, m = month.split("-")
    return (MONTHS_LONG if long else MONTHS)[int(m) - 1] + ("" if long else f" {y[2:]}")


def nice_ticks(top: float, n: int = 4) -> list[float]:
    if top <= 0:
        return [0]
    raw = top / n
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    return [i * step for i in range(int(top // step) + 2)]


def k(cents: float) -> str:
    euros = cents / 100
    return (
        f"{euros / 1000:.1f} k".replace(".", ",").replace(",0 k", " k")
        if euros >= 1000
        else f"{euros:.0f}"
    )


# --------------------------------------------------------------------------- gráficos (SVG)


def chart_cumulative(p: Panel) -> str:
    pace = p.pace
    w, h, ml, mr, mt, mb = 440, 250, 46, 14, 14, 28
    top = max([*pace.cumulative_habit, *pace.cumulative, 1]) * 1.08
    ticks = nice_ticks(top)
    top = ticks[-1]

    def x(d: int) -> float:
        return ml + (d - 1) / max(pace.days - 1, 1) * (w - ml - mr)

    def y(v: float) -> float:
        return h - mb - v / top * (h - mt - mb)

    out = [
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Gasto acumulado del mes frente al mes habitual">'
    ]
    for t in ticks:
        stroke = "var(--axis)" if t == 0 else "var(--grid)"
        out.append(
            f'<line x1="{ml}" x2="{w - mr}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{stroke}"/>'
        )
        out.append(f'<text x="{ml - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{k(t)}</text>')
    for d in (1, 8, 15, 22, 29):
        if d <= pace.days:
            out.append(f'<text x="{x(d):.1f}" y="{h - 8}" text-anchor="middle">{d}</text>')

    def path(values: list[int]) -> str:
        return "".join(
            f"{'L' if i else 'M'}{x(i + 1):.1f},{y(v):.1f}" for i, v in enumerate(values)
        )

    out.append(
        f'<path d="{path(pace.cumulative_habit)}" fill="none" stroke="var(--ref)" stroke-width="2" stroke-dasharray="5 4"/>'
    )
    if pace.cumulative:
        out.append(
            f'<path d="{path(pace.cumulative)}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round"/>'
        )
        cx, cy = x(pace.day), y(pace.cumulative[-1])
        out.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="var(--s1)" stroke="var(--surface)" stroke-width="2"/>'
        )
        anchor = "end" if pace.day > pace.days * 0.6 else "start"
        dx = -8 if anchor == "end" else 8
        out.append(
            f'<text class="lbl-strong" x="{cx + dx:.1f}" y="{cy - 9:.1f}" text-anchor="{anchor}">Hoy {eur(pace.cumulative[-1])}</text>'
        )
    end = pace.cumulative_habit[-1] if pace.cumulative_habit else 0
    out.append(
        f'<text class="lbl" x="{x(pace.days):.1f}" y="{y(end) - 8:.1f}" text-anchor="end">Habitual {eur(end)}</text>'
    )
    # zonas de toque por día (tooltips con JS; <title> como respaldo)
    step = (w - ml - mr) / max(pace.days - 1, 1)
    for d in range(1, pace.days + 1):
        rows = [f"Habitual: {eur(pace.cumulative_habit[d - 1])}"]
        if d <= len(pace.cumulative):
            rows.insert(0, f"Este mes: {eur(pace.cumulative[d - 1])}")
        tip = f"Día {d}|" + "|".join(rows)
        out.append(
            f'<rect class="hit" x="{x(d) - step / 2:.1f}" y="{mt}" width="{step:.1f}" height="{h - mt - mb}" '
            f'data-tip="{esc(tip)}"><title>{esc(tip.replace("|", " · "))}</title></rect>'
        )
    out.append("</svg>")
    return "".join(out)


def chart_flows(p: Panel) -> str:
    flows = p.trend
    w, h, ml, mr, mt, mb = 640, 250, 46, 8, 14, 30
    top = max([max(f.income_cents, f.expense_cents) for f in flows] + [1]) * 1.08
    ticks = nice_ticks(top)
    top = ticks[-1]

    def y(v: float) -> float:
        return h - mb - max(v, 0) / top * (h - mt - mb)

    out = [
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Ingresos y gasto por mes">'
    ]
    for t in ticks:
        stroke = "var(--axis)" if t == 0 else "var(--grid)"
        out.append(
            f'<line x1="{ml}" x2="{w - mr}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{stroke}"/>'
        )
        out.append(f'<text x="{ml - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{k(t)}</text>')
    gw = (w - ml - mr) / len(flows)
    bw = min(16, gw / 2 - 3)
    for i, f in enumerate(flows):
        cx = ml + gw * i + gw / 2
        for v, color, side in (
            (f.income_cents, "var(--s1)", -1),
            (f.expense_cents, "var(--s2)", 1),
        ):
            bx = cx - bw - 1 if side < 0 else cx + 1
            top_y, base = y(v), y(0)
            if base - top_y < 0.5:
                continue
            r = min(3, (base - top_y) / 2)
            opacity = ' opacity="0.5"' if f.in_progress else ""
            out.append(
                f'<path d="M{bx:.1f},{base:.1f}V{top_y + r:.1f}Q{bx:.1f},{top_y:.1f} {bx + r:.1f},{top_y:.1f}'
                f'H{bx + bw - r:.1f}Q{bx + bw:.1f},{top_y:.1f} {bx + bw:.1f},{top_y + r:.1f}V{base:.1f}Z" fill="{color}"{opacity}/>'
            )
        label = month_label(f.month)
        out.append(
            f'<text x="{cx:.1f}" y="{h - 10}" text-anchor="middle">{esc(label.split()[0])}</text>'
        )
        tip = (
            f"{month_label(f.month, long=True).capitalize()}{' (en curso)' if f.in_progress else ''}"
            f"|Ingresos: {eur(f.income_cents)}|Gasto: {eur(f.expense_cents)}"
            f"|Invertido: {eur(f.invested_cents)}|Ahorro en efectivo: {eur(f.saving_cents)}"
        )
        out.append(
            f'<rect class="hit" x="{cx - gw / 2:.1f}" y="{mt}" width="{gw:.1f}" height="{h - mt - mb}" '
            f'data-tip="{esc(tip)}"><title>{esc(tip.replace("|", " · "))}</title></rect>'
        )
    out.append("</svg>")
    return "".join(out)


def chart_net_worth(p: Panel) -> str:
    pts = [n for n in p.net_worth if not n.missing]
    if len(pts) < 2:
        return '<p class="muted">Aún no hay meses suficientes con datos de todas las cuentas.</p>'
    w, h, ml, mr, mt, mb = 440, 240, 50, 26, 22, 30
    lo, hi = min(n.total_cents for n in pts), max(n.total_cents for n in pts)
    pad = max((hi - lo) * 0.15, hi * 0.02, 1)
    lo, hi = max(lo - pad, 0), hi + pad

    def x(i: int) -> float:
        return ml + i / (len(pts) - 1) * (w - ml - mr)

    def y(v: float) -> float:
        return h - mb - (v - lo) / (hi - lo) * (h - mt - mb)

    out = [
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Patrimonio neto a fin de mes">'
    ]
    for t in nice_ticks(hi - lo, 3):
        v = lo + t
        if v > hi:
            break
        out.append(
            f'<line x1="{ml}" x2="{w - mr}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--grid)"/>'
        )
        out.append(f'<text x="{ml - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{k(v)}</text>')
    line = "".join(
        f"{'L' if i else 'M'}{x(i):.1f},{y(n.total_cents):.1f}" for i, n in enumerate(pts)
    )
    area = (
        f"M{x(0):.1f},{y(lo):.1f}"
        + line.replace("M", "L", 1)
        + f"L{x(len(pts) - 1):.1f},{y(lo):.1f}Z"
    )
    out.append(f'<path d="{area}" fill="var(--s1)" opacity="0.10"/>')
    out.append(f'<path d="{line}" fill="none" stroke="var(--s1)" stroke-width="2"/>')
    every = max(1, len(pts) // 6)
    for i, n in enumerate(pts):
        market = n.investments_basis == "mercado"
        fill = "var(--s1)" if market else "var(--surface)"
        out.append(
            f'<circle cx="{x(i):.1f}" cy="{y(n.total_cents):.1f}" r="4.5" fill="{fill}" stroke="var(--s1)" stroke-width="2"/>'
        )
        if i % every == 0 or i == len(pts) - 1:
            out.append(
                f'<text x="{x(i):.1f}" y="{h - 10}" text-anchor="middle">{esc(month_label(n.month).split()[0])}</text>'
            )
        tip = (
            f"{month_label(n.month, long=True).capitalize()}|Patrimonio: {eur(n.total_cents)}"
            f"|Efectivo: {eur(n.cash_cents)}|Inversiones: {eur(n.investments_cents)} "
            f"({'a mercado' if market else 'a coste'})"
        )
        out.append(
            f'<circle class="hit" cx="{x(i):.1f}" cy="{y(n.total_cents):.1f}" r="16" data-tip="{esc(tip)}">'
            f"<title>{esc(tip.replace('|', ' · '))}</title></circle>"
        )
    last = pts[-1]
    out.append(
        f'<text class="lbl-strong" x="{x(len(pts) - 1):.1f}" y="{y(last.total_cents) - 11:.1f}" text-anchor="end">{eur(last.total_cents)}</text>'
    )
    out.append("</svg>")
    return "".join(out)


def stack(parts: list[tuple[str, int, str, str]], base: int, label: str) -> str:
    """Barra apilada + leyenda con cifras: (etiqueta, céntimos, color, nota)."""
    shown = [pp for pp in parts if pp[1] > 0]
    segs = "".join(
        f'<div style="flex:{c};background:{color}" data-tip="{esc(f"{name}|{eur(c)} · {pct(c / base if base else None)}")}"></div>'
        for name, c, color, _ in shown
    )
    rows = "".join(
        f'<div class="split-row"><span class="dot" style="background:{color}"></span>'
        f"<span>{esc(name)}{f'<small>{esc(note)}</small>' if note else ''}</span>"
        f'<span class="amt">{eur(c)}<small>{pct(c / base if base else None)}</small></span></div>'
        for name, c, color, note in parts
    )
    return f'<div class="stack" role="img" aria-label="{esc(label)}">{segs}</div><div class="split">{rows}</div>'


# --------------------------------------------------------------------------- secciones


def section_pace(p: Panel) -> str:
    pace = p.pace
    delta = pace.delta_cents
    if pace.variable_habit_cents == 0:
        pill = '<span class="pill">Sin referencia todavía</span>'
    elif delta > max(2000, pace.variable_habit_to_day_cents * 0.05):
        pill = f'<span class="pill warn">{ICON_WARN}{eur(delta)} por encima del ritmo</span>'
    elif delta < -max(2000, pace.variable_habit_to_day_cents * 0.05):
        pill = f'<span class="pill good">{ICON_OK}{eur(-delta)} por debajo del ritmo</span>'
    else:
        pill = f'<span class="pill good">{ICON_OK}Al ritmo habitual</span>'
    full = max(pace.variable_habit_cents, pace.variable_cents, 1)
    fill = min(pace.variable_cents / full, 1) * 100
    mark = min(pace.variable_habit_to_day_cents / full, 1) * 100
    proj = pace.projected_cents
    proj_delta = proj - pace.expense_habit_cents
    saving = pace.projected_saving_cents
    habit_saving = p.habit.income_cents - p.habit.expense_cents
    rate = saving / pace.income_habit_cents if pace.income_habit_cents else None
    habit_rate = habit_saving / p.habit.income_cents if p.habit.income_cents else None
    early = (
        '<p class="muted small">Primeros días del mes: la proyección aún es poco fiable.</p>'
        if pace.day < 7
        else ""
    )
    table = "".join(
        f"<tr><td>{d}</td><td>{eur(pace.cumulative[d - 1]) if d <= len(pace.cumulative) else '—'}</td>"
        f"<td>{eur(pace.cumulative_habit[d - 1])}</td></tr>"
        for d in sorted({1, 5, 10, 15, 20, 25, pace.day, pace.days})
        if d <= pace.days
    )
    return f"""
<section aria-labelledby="h-pace">
  <h2 id="h-pace">¿Cómo voy este mes?</h2>
  <div class="pace">
    <div class="panel">
      <div class="row-between"><span class="eyebrow">Gasto variable a día {pace.day}</span>{pill}</div>
      <div class="hero-num">{eur(pace.variable_cents)}</div>
      <div class="hero-of">de {eur(pace.variable_habit_cents)} que sueles gastar en variable al mes · a estas alturas sueles llevar {eur(pace.variable_habit_to_day_cents)}</div>
      <div class="bar-track" role="img" aria-label="{eur(pace.variable_cents)} de {eur(pace.variable_habit_cents)}; ritmo habitual hoy {eur(pace.variable_habit_to_day_cents)}">
        <div class="bar-fill" style="width:{fill:.1f}%"></div><div class="bar-mark" style="left:{mark:.1f}%"></div>
      </div>
      <div class="bar-legend"><span>0 €</span><span>▏ritmo habitual hoy: {eur(pace.variable_habit_to_day_cents)}</span><span>{eur(full)}</span></div>
      <div class="facts">
        <div class="fact"><div class="k">Fijos pagados</div><div class="v">{eur(pace.fixed_cents)}</div><div class="n">sueles pagar {eur(pace.fixed_habit_cents)}</div></div>
        <div class="fact"><div class="k">Gasto previsto a fin de mes</div><div class="v">{eur(proj)}</div><div class="n">{eur(abs(proj_delta))} {"más" if proj_delta > 0 else "menos"} que tu media</div></div>
        <div class="fact"><div class="k">Nómina neta media</div><div class="v">{eur(pace.salary_habit_cents)}</div><div class="n">{esc(", ".join(month_label(m, long=True) for m in p.habit_months))}</div></div>
        <div class="fact"><div class="k">Ahorro previsto (incl. inversión)</div><div class="v">{eur(saving)} · {pct(rate)}</div><div class="n">media: {eur(habit_saving)} · {pct(habit_rate)}</div></div>
      </div>
      {early}
    </div>
    <div class="panel">
      <div class="chart-head"><span class="chart-title">Gasto acumulado</span>
        <span class="legend"><span class="key"><span class="sw" style="background:var(--s1)"></span>{esc(month_label(pace.month, long=True).capitalize())}</span><span class="key"><span class="sw dash"></span>Mes habitual</span></span></div>
      {chart_cumulative(p)}
      <details><summary>Ver como tabla</summary><div class="scroll"><table><tr><th>Día</th><th>Este mes</th><th>Habitual</th></tr>{table}</table></div></details>
    </div>
  </div>
</section>"""


def section_categories_now(p: Panel) -> str:
    cats = sorted(
        [c for c in p.categories if not c.fixed and max(c.to_day_cents, c.habit_to_day_cents) > 0],
        key=lambda c: -max(c.to_day_cents, c.habit_to_day_cents),
    )[:9]
    if not cats:
        return ""
    top = max(max(c.to_day_cents, c.habit_to_day_cents) for c in cats) * 1.08 or 1
    rows = []
    for c in cats:
        d = c.to_day_cents - c.habit_to_day_cents
        cls = "up" if d > 1000 else "down" if d < -1000 else ""
        tip = f"{c.name}|Este mes: {eur(c.to_day_cents)}|Habitual a día {p.pace.day}: {eur(c.habit_to_day_cents)}"
        rows.append(
            f'<div class="cat" data-tip="{esc(tip)}"><span class="name">{esc(c.name)}{" <small>· fijo</small>" if c.fixed else ""}</span>'
            f'<div class="track" role="img" aria-label="{esc(tip.replace("|", ", "))}">'
            f'<div class="fill" style="width:{max(c.to_day_cents, 0) / top * 100:.1f}%"></div>'
            f'<div class="avg" style="left:{max(c.habit_to_day_cents, 0) / top * 100:.1f}%"></div></div>'
            f'<span class="vals">{eur(c.to_day_cents)}<span class="delta {cls}">{eur(d, sign=True) if d else "±0 €"}</span></span></div>'
        )
    return f"""
<section aria-labelledby="h-now">
  <h2 id="h-now">¿Dónde se está yendo este mes?</h2>
  <div class="panel">
    <div class="chart-head"><span class="chart-title">Gasto variable por categoría a día {p.pace.day}</span>
      <span class="legend"><span class="key"><span class="sw box" style="background:var(--s1)"></span>Este mes</span><span class="key"><span class="sw tick"></span>Lo habitual a día {p.pace.day}</span></span></div>
    <div class="cats">{"".join(rows)}</div>
  </div>
</section>"""


def _movements_table(movs: list[insights.Movement]) -> str:
    rows = "".join(
        f'<tr><td>{m.day[8:10]}/{m.day[5:7]}/{m.day[2:4]}</td><td class="desc">{esc(m.description)}</td>'
        f"<td>{fmt_eur(m.amount_cents).replace('-', '−')}</td></tr>"
        for m in movs
    )
    return f'<div class="scroll"><table class="movs"><tr><th>Fecha</th><th>Concepto</th><th>Importe</th></tr>{rows}</table></div>'


def section_ranking(p: Panel) -> str:
    cats = [c for c in p.categories if c.monthly_cents > 0]
    if not cats:
        return ""
    total = p.habit.expense_cents or 1
    salary = p.habit.salary_cents or p.habit.income_cents or 1
    top = cats[0].monthly_cents
    items = []
    for c in cats:
        merchants = []
        for m in c.merchants[:25]:
            merchants.append(
                f'<details class="merchant"><summary><span>{esc(m.name)}</span>'
                f'<span class="num">{m.per_month:.1f}/mes</span><span class="num">{eur(m.monthly_cents)}/mes</span></summary>'
                f"{_movements_table(m.movements)}</details>"
            )
        more = len(c.merchants) - 25
        extra = f'<p class="muted small">… y {more} comercios más.</p>' if more > 0 else ""
        trend = ""
        if c.trend is not None and abs(c.trend) >= 0.1:
            arrow = "↑" if c.trend > 0 else "↓"
            cls = "up" if c.trend > 0 else "down"
            trend = f' <span class="delta {cls}">{arrow} {abs(c.trend) * 100:.0f} % vs meses anteriores</span>'
        items.append(
            f'<details class="rank"><summary>{ICON_CHEV}<span class="rname">{esc(c.name)}{" <small>· fijo</small>" if c.fixed else ""}</span>'
            f'<span class="share-wrap"><span class="share" style="width:{c.monthly_cents / top * 100:.1f}%"></span></span>'
            f'<span class="vals">{eur(c.monthly_cents)}/mes<small>{pct(c.monthly_cents / total)} del gasto · {pct(c.monthly_cents / salary)} nómina</small></span></summary>'
            f'<div class="drill"><p class="muted small">Por comercio, media de {esc(", ".join(month_label(m, long=True) for m in p.habit_months))}.{trend} Toca un comercio para ver sus movimientos.</p>'
            f'<div class="merchants">{"".join(merchants)}</div>{extra}</div></details>'
        )
    return f"""
<section aria-labelledby="h-rank">
  <h2 id="h-rank">¿En qué gasto más?</h2>
  <p class="sub">Media de los últimos {len(p.habit_months)} meses: {eur(p.habit.expense_cents)} al mes, el {pct(p.habit.expense_cents / salary)} de tu nómina neta. Toca una categoría para ver en qué comercios se va.</p>
  <div class="panel" id="ranking">{"".join(items)}</div>
</section>"""


def section_money(p: Panel) -> str:
    h = p.habit
    income = h.income_cents or 1
    flow = stack(
        [
            ("Gastos fijos", h.fixed_cents, "var(--ref)", "alquiler, suministros, suscripciones"),
            ("Gasto variable", h.variable_cents, "var(--s2)", ""),
            ("A inversión", h.invested_cents, "var(--s1)", "compras de tu plan y sueltas"),
            ("Ahorro en efectivo", h.saving_cents, "var(--s3)", "lo que queda en las cuentas"),
        ],
        income,
        "Reparto de los ingresos de un mes habitual",
    )

    def color(label: str) -> str:  # el color sigue al tipo de dinero, no a su posición
        if label == "Inversiones":
            return "var(--s1)"
        return "var(--s3)" if "Trade Republic" in label else "var(--ref)"

    total = sum(x.cents for x in p.holdings) or 1
    worth = stack(
        [(x.label, x.cents, color(x.label), x.note) for x in p.holdings],
        total,
        "Patrimonio por tipo",
    )
    first = next((n for n in p.net_worth if not n.missing), None)
    since = ""
    if first is not None and p.net_worth:
        diff = p.net_worth[-1].total_cents - first.total_cents
        since = f" · {eur(diff, sign=True)} desde {month_label(first.month, long=True)}"
    negative = (
        '<p class="muted small">Este mes tipo gastas e inviertes más de lo que ingresas: el ahorro en efectivo es negativo.</p>'
        if h.saving_cents < 0
        else ""
    )
    return f"""
<section aria-labelledby="h-money">
  <h2 id="h-money">¿Dónde está mi dinero?</h2>
  <div class="two">
    <div class="panel"><span class="eyebrow">Un mes habitual, de {eur(h.income_cents)} de ingresos</span>{flow}{negative}</div>
    <div class="panel"><span class="eyebrow">Patrimonio hoy</span><p class="big-line"><b>{eur(total)}</b>{since}</p>{worth}</div>
  </div>
</section>"""


def section_focus(p: Panel) -> str:
    if not p.focus:
        return ""
    cards = []
    for f in p.focus:
        c = f.category
        top_merchants = ", ".join(m.name for m in c.merchants[:3])
        reasons = "; ".join(f.reasons)
        cards.append(
            f'<article class="panel cut"><span class="eyebrow">{esc(c.name)}</span>'
            f'<div class="save">−{eur(f.saving_cents)}/mes</div>'
            f'<p class="why">{eur(c.monthly_cents)} al mes de media; {esc(reasons)}.</p>'
            f'<p class="how">Recortando un {CUT_SHARE * 100:.0f} %. Donde más: {esc(top_merchants)}.</p></article>'
        )
    total = sum(f.saving_cents for f in p.focus)
    return f"""
<section aria-labelledby="h-focus">
  <h2 id="h-focus">¿Dónde debería centrarme?</h2>
  <p class="sub">Tus tres mayores gastos variables. Recortar un {CUT_SHARE * 100:.0f} % en ellos serían {eur(total)} al mes, {eur(total * 12)} al año. Son propuestas: tú decides qué tiene sentido.</p>
  <div class="cuts">{"".join(cards)}</div>
</section>"""


def section_trend(p: Panel) -> str:
    rows = "".join(
        f"<tr><td>{esc(month_label(f.month))}{' *' if f.in_progress else ''}</td><td>{eur(f.salary_cents)}</td><td>{eur(f.income_cents)}</td>"
        f"<td>{eur(f.expense_cents)}</td><td>{eur(f.invested_cents)}</td><td>{eur(f.saving_cents)}</td><td>{pct(f.saving_rate)}</td></tr>"
        for f in p.trend
    )
    nw_rows = "".join(
        f"<tr><td>{esc(month_label(n.month))}</td><td>{eur(n.cash_cents)}</td><td>{eur(n.investments_cents)}</td>"
        f"<td>{eur(n.total_cents)}</td><td>{'mercado' if n.investments_basis == 'mercado' else 'coste'}</td></tr>"
        for n in p.net_worth
        if not n.missing
    )
    return f"""
<section aria-labelledby="h-trend">
  <h2 id="h-trend">¿Cómo voy mes a mes?</h2>
  <div class="panel">
    <div class="chart-head"><span class="chart-title">Ingresos y gasto</span>
      <span class="legend"><span class="key"><span class="sw box" style="background:var(--s1)"></span>Ingresos</span><span class="key"><span class="sw box" style="background:var(--s2)"></span>Gasto</span></span></div>
    {chart_flows(p)}
    <details><summary>Ver como tabla</summary><div class="scroll"><table><tr><th>Mes</th><th>Nómina</th><th>Ingresos</th><th>Gasto</th><th>Invertido</th><th>Ahorro efectivo</th><th>Tasa de ahorro</th></tr>{rows}</table></div>
    <p class="muted small">* mes en curso. Tasa de ahorro = (ingresos − gasto) / ingresos, incluye lo invertido.</p></details>
  </div>
  <div class="panel">
    <div class="chart-head"><span class="chart-title">Patrimonio neto</span>
      <span class="legend"><span class="key"><span class="dot-key full"></span>Inversiones a mercado</span><span class="key"><span class="dot-key"></span>a coste</span></span></div>
    {chart_net_worth(p)}
    <details><summary>Ver como tabla</summary><div class="scroll"><table><tr><th>Mes</th><th>Efectivo</th><th>Inversiones</th><th>Total</th><th>Inversiones a</th></tr>{nw_rows}</table></div></details>
  </div>
</section>"""


def headline(p: Panel) -> str:
    """El mes en una o dos frases (determinista)."""
    pace = p.pace
    if not pace.variable_habit_cents:
        return "Aún no hay meses de referencia suficientes para comparar."
    margin = max(2000, pace.variable_habit_to_day_cents * 0.05)
    if pace.delta_cents > margin:
        first = f"Vas {eur(pace.delta_cents)} por encima de tu ritmo habitual de gasto variable"
    elif pace.delta_cents < -margin:
        first = f"Vas {eur(-pace.delta_cents)} por debajo de tu ritmo habitual de gasto variable"
    else:
        first = "Vas al ritmo habitual de gasto variable"
    movers = sorted(
        (c for c in p.categories if not c.fixed and c.to_day_cents - c.habit_to_day_cents > 1000),
        key=lambda c: c.habit_to_day_cents - c.to_day_cents,
    )
    if movers:
        c = movers[0]
        first += f"; lo que más ha subido es {c.name.lower()} ({eur(c.to_day_cents - c.habit_to_day_cents, sign=True)})"
    second = ""
    if pace.day >= 7 and pace.income_habit_cents:
        second = f" Si sigues así, este mes ahorrarás unos {eur(pace.projected_saving_cents)}"
        second += f" (sueles ahorrar {eur(p.habit.income_cents - p.habit.expense_cents)})."
    return first + "." + second


def render(p: Panel, *, generated_at: str) -> str:
    title_month = month_label(p.pace.month, long=True).capitalize() + " " + p.pace.month[:4]
    warnings = "".join(f'<div class="note" role="note">{esc(w)}</div>' for w in p.warnings)
    body = "".join(
        [
            section_pace(p),
            section_categories_now(p),
            section_ranking(p),
            section_money(p),
            section_focus(p),
            section_trend(p),
        ]
    )
    habit = ", ".join(month_label(m, long=True) for m in p.habit_months)
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Pulso del mes</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<header>
  <div class="eyebrow">{esc(title_month)} · día {p.pace.day} de {p.pace.days}</div>
  <h1>Pulso del mes</h1>
  <p class="lead">{esc(headline(p))}</p>
  <p class="sub">Lo habitual es la media de {esc(habit)}.</p>
</header>
{warnings}
{body}
<section class="notes" aria-labelledby="h-notes">
  <h2 id="h-notes">Cómo se calcula</h2>
  <ul>
    <li><b>Fijos</b>: alquiler, suministros, suscripciones y seguros. El resto es <b>variable</b>, y solo el variable se compara con el ritmo del mes.</li>
    <li>Traspasos entre tus cuentas e inversión no son gasto. <b>Invertido</b> = compras − ventas. <b>Ahorro en efectivo</b> = ingresos − gasto − invertido.</li>
    <li>El patrimonio va a valor de mercado los meses con valoración de Trade Republic de su última semana; el resto, a coste.</li>
    <li>Generado el {esc(generated_at)} por Personal OS en tu PC. Tus datos no salen de tu equipo ni de tu iCloud.</li>
  </ul>
</section>
</div>
<div class="tip" id="tip" hidden></div>
<script>{JS}</script>
</body></html>"""


# --------------------------------------------------------------------------- escribir y abrir


@dataclass(frozen=True)
class PanelResult:
    path: Path
    size: int


def panel_dir(cfg) -> Path:
    """Dentro del buzón de Scriptable (lo lee el script del iPhone); si no hay buzón, en datos."""
    if cfg.apple.mailbox_dir is not None:
        return cfg.apple.mailbox_dir / "panel"
    return cfg.data_dir / "panel"


def generate(application) -> PanelResult:
    from zoneinfo import ZoneInfo

    from personal_os import bootstrap

    tz = ZoneInfo(application.config.timezone)
    now = application.clock.now().astimezone(tz)
    svc = bootstrap.finance_service(application)
    panel = insights.build(svc, today=now.date().isoformat())
    page = render(panel, generated_at=now.strftime("%d/%m/%Y %H:%M"))
    folder = panel_dir(application.config)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / INDEX
    tmp = folder / (INDEX + ".tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, target)
    return PanelResult(target, len(page.encode()))


def open_in_browser(path: Path) -> bool:
    """Abre el fichero en el navegador de Windows desde WSL (o el del sistema)."""
    try:
        if shutil.which("wslpath") and shutil.which("explorer.exe"):
            win = subprocess.run(
                ["wslpath", "-w", str(path)], capture_output=True, text=True, check=True
            ).stdout.strip()
            subprocess.run(["explorer.exe", win], check=False)  # devuelve 1 aunque abra
            return True
        import webbrowser

        return webbrowser.open(path.as_uri())
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------- estilos y JS

ICON_WARN = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 15 14H1z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M8 6v3.6M8 11.6v.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>'
ICON_OK = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5 6.5 12 13 4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
ICON_CHEV = '<svg class="chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'

CSS = """
:root{--page:#f6f6f3;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--muted:#74726c;--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--ref:#898781;--good-ink:#006300;--warn-ink:#8a5a00;--note:#fff4dc;
--ui:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink-2:#c3c2b7;--muted:#9a988f;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--ref:#9a988f;--good-ink:#0ca30c;--warn-ink:#fab219;--note:#2b2414;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 var(--ui);-webkit-text-size-adjust:100%}
.wrap{max-width:1000px;margin:0 auto;padding-inline:16px;padding-block:calc(20px + env(safe-area-inset-top,0px)) calc(56px + env(safe-area-inset-bottom,0px));display:grid;gap:28px}
header{display:grid;gap:4px}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
h1{font-size:clamp(28px,6vw,40px);line-height:1.1;margin:0;font-weight:750;letter-spacing:-.01em}
h2{font-size:21px;margin:0;font-weight:700}
.sub{color:var(--ink-2);margin:0;max-width:65ch}
.lead{font-size:17px;margin:6px 0 0;max-width:62ch;text-wrap:pretty}
.muted{color:var(--muted)}.small{font-size:12.5px;margin:6px 0 0}
section{display:grid;gap:12px}
.panel{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:16px;min-width:0}
.note{background:var(--note);border-radius:8px;padding:10px 14px;font-size:13.5px}
.row-between{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center}
.pace{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:14px;align-items:start}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
@media (max-width:760px){.pace,.two{grid-template-columns:1fr}}
.hero-num{font-size:clamp(34px,8vw,48px);font-weight:650;line-height:1;margin-top:10px}
.hero-of{color:var(--ink-2);margin-top:6px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:13px;font-weight:600;border:1px solid currentColor}
.pill.warn{color:var(--warn-ink)}.pill.good{color:var(--good-ink)}.pill svg{width:14px;height:14px}
.bar-track{position:relative;height:12px;background:var(--grid);border-radius:6px;margin-top:16px}
.bar-fill{position:absolute;inset:0 auto 0 0;background:var(--s1);border-radius:6px}
.bar-mark{position:absolute;top:-5px;bottom:-5px;width:2px;background:var(--ink)}
.bar-legend{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin-top:6px}
.facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 20px;margin-top:18px}
.fact .k{font-size:12.5px;color:var(--muted)}.fact .v{font-size:19px;font-weight:600}.fact .n{font-size:12.5px;color:var(--ink-2)}
.chart-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.chart-title{font-weight:600}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--ink-2)}
.key{display:inline-flex;align-items:center;gap:6px}
.sw{width:14px;height:3px;border-radius:2px;display:inline-block}.sw.box{width:10px;height:10px}
.sw.dash{background:repeating-linear-gradient(90deg,var(--ref) 0 4px,transparent 4px 7px)}
.sw.tick{width:2px;height:12px;background:var(--ink)}
.dot-key{width:10px;height:10px;border-radius:50%;border:2px solid var(--s1);display:inline-block}.dot-key.full{background:var(--s1)}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
svg.chart text{fill:var(--muted);font:11px var(--ui);font-variant-numeric:tabular-nums}
svg.chart .lbl,svg.chart .lbl-strong{paint-order:stroke;stroke:var(--surface);stroke-width:4px;stroke-linejoin:round}
svg.chart .lbl{fill:var(--ink-2);font-size:12px}svg.chart .lbl-strong{fill:var(--ink);font-weight:600;font-size:12px}
svg.chart .hit{fill:transparent}
.tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--ring);box-shadow:0 4px 16px rgba(0,0,0,.14);border-radius:8px;padding:8px 10px;font-size:12.5px;line-height:1.45;z-index:10;font-variant-numeric:tabular-nums;max-width:260px}
.tip b{display:block}
.cats{display:grid}
.cat{display:grid;grid-template-columns:minmax(0,160px) minmax(0,1fr) 96px;gap:12px;align-items:center;padding:7px 0;border-top:1px solid var(--grid)}
.cat:first-child{border-top:0}.cat .name{overflow-wrap:anywhere}.cat small,.rname small{color:var(--muted)}
.vals{text-align:right;font-variant-numeric:tabular-nums;font-size:13.5px}
.vals small{display:block;color:var(--muted);font-size:12px}
.delta{display:block;font-size:12px}.delta.up{color:var(--warn-ink)}.delta.down{color:var(--good-ink)}
.track{position:relative;height:14px}
.track .fill{position:absolute;left:0;top:2px;height:10px;background:var(--s1);border-radius:0 4px 4px 0;min-width:2px}
.track .avg{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--ink)}
@media (max-width:540px){.cat{grid-template-columns:minmax(0,1fr) 90px}.cat .track{grid-column:1/-1;grid-row:2}}
details>summary{cursor:pointer;list-style:none}details>summary::-webkit-details-marker{display:none}
.panel>details>summary,section .panel details:not(.rank):not(.merchant)>summary{color:var(--ink-2);font-size:13px;margin-top:8px}
summary:focus-visible{outline:2px solid var(--s1);outline-offset:2px;border-radius:6px}
details.rank{border-top:1px solid var(--grid)}details.rank:first-child{border-top:0}
details.rank>summary{display:grid;grid-template-columns:18px minmax(0,200px) minmax(0,1fr) minmax(0,230px);gap:12px;align-items:center;padding:10px 4px;border-radius:6px;font-size:14.5px}
details.rank>summary:hover{background:color-mix(in srgb,var(--s1) 7%,transparent)}
.chev{width:14px;height:14px;color:var(--muted);transition:transform .15s}
details[open]>summary .chev{transform:rotate(90deg)}
.share-wrap{display:block}.share{display:block;height:10px;background:var(--s1);border-radius:0 4px 4px 0;min-width:2px}
@media (max-width:600px){details.rank>summary{grid-template-columns:18px minmax(0,1fr) minmax(0,150px)}details.rank .share-wrap{grid-column:2/-1;grid-row:2}}
.drill{padding:0 4px 14px 32px;display:grid;gap:6px}
@media (max-width:600px){.drill{padding-left:4px}}
.merchants{display:grid}
details.merchant{border-top:1px solid var(--grid)}
details.merchant>summary{display:grid;grid-template-columns:minmax(0,1fr) 64px 86px;gap:8px;padding:7px 2px;font-size:14px}
details.merchant>summary .num{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink-2)}
details.merchant[open]>summary{font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:5px 8px;border-bottom:1px solid var(--grid);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:500}
table.movs td.desc{text-align:left;white-space:normal;color:var(--ink-2);font-size:12.5px;min-width:180px}
.scroll{overflow-x:auto}
.stack{display:flex;height:26px;gap:2px;margin:12px 0 10px}.stack>div{border-radius:4px;min-width:4px}
.split{display:grid;gap:8px}
.split-row{display:grid;grid-template-columns:12px minmax(0,1fr) auto;gap:10px;align-items:baseline;font-size:14px}
.split-row small{display:block;color:var(--muted);font-size:12px;font-weight:400}
.split-row .amt{font-variant-numeric:tabular-nums;font-weight:600;text-align:right}
.dot{width:10px;height:10px;border-radius:2px;align-self:center}
.big-line{font-size:15px;color:var(--ink-2);margin:6px 0 0}.big-line b{color:var(--ink);font-size:24px;font-weight:650}
.cuts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
@media (max-width:840px){.cuts{grid-template-columns:1fr}}
.cut{display:grid;gap:6px;align-content:start}.cut .save{font-size:22px;font-weight:650;color:var(--good-ink)}
.cut .why{color:var(--ink-2);font-size:13.5px;margin:0}.cut .how{font-size:13px;margin:0;padding-top:8px;border-top:1px solid var(--grid)}
.notes{color:var(--ink-2);font-size:13.5px}.notes ul{margin:0;padding-left:18px;display:grid;gap:4px}
@media (prefers-reduced-motion:reduce){.chev{transition:none}}
"""

JS = """
(function(){
  var tip=document.getElementById('tip');
  function show(el,x,y){
    var parts=(el.getAttribute('data-tip')||'').split('|');
    tip.innerHTML='';
    parts.forEach(function(t,i){var d=document.createElement(i?'div':'b');d.textContent=t;tip.appendChild(d);});
    tip.hidden=false;
    var w=tip.offsetWidth,h=tip.offsetHeight;
    tip.style.left=Math.max(8,Math.min(x+12,window.innerWidth-w-8))+'px';
    tip.style.top=Math.max(8,y-h-12)+'px';
  }
  function hide(){tip.hidden=true;}
  document.addEventListener('pointermove',function(e){
    var el=e.target.closest&&e.target.closest('[data-tip]');
    if(el&&e.pointerType==='mouse')show(el,e.clientX,e.clientY);else if(e.pointerType==='mouse')hide();
  });
  document.addEventListener('pointerdown',function(e){
    var el=e.target.closest&&e.target.closest('[data-tip]');
    if(el&&e.pointerType!=='mouse'){show(el,e.clientX,e.clientY);}else if(e.pointerType!=='mouse'){hide();}
  });
  document.addEventListener('scroll',hide,{passive:true});
  document.querySelectorAll('svg title').forEach(function(t){t.remove();});
})();
"""
