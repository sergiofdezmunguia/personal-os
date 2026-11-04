"""Panel v2 (ADR 0009, revisado): pestañas Hoy · Dinero · Cartera · Hábitos.

«Dinero» es el análisis de siempre (`panel.py`). Este módulo añade las otras pestañas y los
datos extra que necesitan. Igual que el resto del panel: HTML generado en el PC, legible sin
JavaScript (sin JS las pestañas se ven una debajo de otra) y sin recursos externos.

La agenda de hoy no está en el PC: la pone el visor de Scriptable al abrir el panel
(`window.posAgenda(...)`) leyendo el Calendario y Recordatorios del iPhone.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, timedelta

from personal_os.finance import analyst, investments
from personal_os.finance.insights import Panel
from personal_os.habits import health

MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DOW = ["L", "M", "X", "J", "V", "S", "D"]


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def eur(cents: int | None, *, decimals: bool = False, sign: bool = False) -> str:
    if cents is None:
        return "—"
    value = abs(cents) / 100
    text = f"{value:,.2f}" if decimals else f"{value:,.0f}"
    text = text.replace(",", "·").replace(".", ",").replace("·", ".")
    prefix = "−" if cents < 0 else ("+" if sign and cents > 0 else "")
    return f"{prefix}{text} €"


def short_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.day} {MONTHS[d.month - 1]}"


@dataclass(frozen=True)
class Extras:
    """Lo que el panel v2 añade al análisis de gasto."""

    yesterday: analyst.Spend | None = None
    recurring: list[analyst.Recurring] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    contributions: list[investments.MonthContribution] = field(default_factory=list)
    plan: list[investments.PlanLine] = field(default_factory=list)
    habits: health.Summary | None = None
    habits_file: bool = False


def build_extras(
    svc, panel: Panel, *, today: str, alerts: list[dict], health_text: str | None
) -> Extras:
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    summary = None
    if health_text is not None:
        days, _ = health.parse(health_text)
        summary = health.summarize(days, today=today)
    return Extras(
        yesterday=analyst.spend(svc, yesterday, yesterday),
        recurring=analyst.recurring(svc, today=today),
        alerts=alerts,
        contributions=investments.contributions(svc, months=6, today=today),
        plan=investments.deduce_plan(svc, today),
        habits=summary,
        habits_file=health_text is not None,
    )


# --------------------------------------------------------------------------- Hoy


def _card(title: str, body: str, *, extra: str = "") -> str:
    return f'<div class="panel card"><div class="row-between"><h3>{esc(title)}</h3>{extra}</div>{body}</div>'


def tab_today(p: Panel, x: Extras) -> str:
    pace = p.pace
    cards = []
    # Ritmo del mes, en pequeño.
    full = max(pace.variable_habit_cents, pace.variable_cents, 1)
    fill = min(pace.variable_cents / full, 1) * 100
    mark = min(pace.variable_habit_to_day_cents / full, 1) * 100
    diff = pace.variable_cents - pace.variable_habit_to_day_cents
    if pace.variable_habit_cents:
        word = "por encima" if diff > 0 else "por debajo"
        cls = "warn" if diff > 0 else "good"
        status = f'<span class="pill {cls}">{eur(abs(diff))} {word} del ritmo</span>'
    else:
        status = ""
    cards.append(_card("Este mes", f"""
      <div class="hero-num">{eur(pace.variable_cents)}</div>
      <div class="hero-of">de gasto variable a día {pace.day} · sueles llevar {eur(pace.variable_habit_to_day_cents)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:{fill:.1f}%"></div><div class="bar-mark" style="left:{mark:.1f}%"></div></div>
      <p class="muted small">Fijos pagados: {eur(pace.fixed_cents)} · previsto a fin de mes: {eur(pace.projected_cents)}</p>""",
        extra=status))  # fmt: skip

    # Ayer.
    y = x.yesterday
    if y is not None:
        if y.expense_cents:
            tops = "".join(
                f'<div class="li"><span>{esc(n)}</span><span class="num">{eur(c, decimals=True)}</span></div>'
                for n, c in y.top
            )
            body = f'<div class="big-line"><b>{eur(y.expense_cents, decimals=True)}</b> en {y.payments} pago{"s" * (y.payments != 1)}</div><div class="list">{tops}</div>'
        else:
            body = '<p class="muted">Ayer no hubo gastos.</p>'
        if y.uncategorized:
            body += f'<p class="muted small">{y.uncategorized} sin categoría ({eur(y.uncategorized_cents, decimals=True)})</p>'
        cards.append(_card("Ayer", body))

    # Agenda: la rellena el visor de Scriptable con el Calendario del iPhone.
    cards.append(
        _card(
            "Agenda",
            '<div id="agenda" class="list"><p class="muted small">Ábrelo desde Scriptable («Personal OS Panel») para ver tus citas y tareas de hoy.</p></div>',
        )
    )

    # Avisos.
    if x.alerts:
        items = "".join(
            f'<div class="alert"><b>{esc(a["title"])}</b><span>{esc(a["body"])}</span></div>'
            for a in x.alerts
        )
        cards.append(_card("Avisos", f'<div class="alerts">{items}</div>'))

    # Cartera.
    for lp in p.portfolio:
        base = lp.value_cents - lp.day_change_cents
        day = f"{100 * lp.day_change_cents / base:+.2f} %".replace(".", ",") if base else ""
        cls = "good" if lp.day_change_cents >= 0 else "warn"
        cards.append(_card("Cartera", f"""
          <div class="hero-num">{eur(lp.value_cents)}</div>
          <div class="hero-of">{eur(lp.gain_cents, sign=True)} sobre lo aportado ({str(lp.gain_pct).replace('.', ',')} %) · cierre del {short_day(lp.as_of)}</div>""",
            extra=f'<span class="pill {cls}">{eur(lp.day_change_cents, sign=True)} · {day}</span>' if day else ""))  # fmt: skip

    # Hábitos de ayer.
    if x.habits and x.habits.days and x.habits.days[-1].values:
        cards.append(_card("Ayer en Salud", _habit_tiles(x.habits.days[-1], x.habits)))
    return f'<section class="view" id="v-hoy" aria-label="Hoy"><div class="cards">{"".join(cards)}</div></section>'


# --------------------------------------------------------------------------- Dinero (extra)


def section_recurring(x: Extras) -> str:
    if not x.recurring:
        return ""
    total = sum(r.monthly_cents for r in x.recurring if r.active)
    rows = []
    for r in x.recurring:
        rise = (
            f'<span class="delta up">sube {eur(r.rise_cents, decimals=True)} (antes {eur(r.previous_cents, decimals=True)})</span>'
            if r.previous_cents
            else ""
        )
        amount = ("≈ " if r.variable else "") + eur(r.last_cents, decimals=True)
        nxt = f"próximo ~{short_day(r.next_day)}" if r.next_day else ""
        rows.append(
            f'<div class="li"><span>{esc(r.merchant)}<small>{esc(r.cadence)} · {nxt}</small>{rise}</span>'
            f'<span class="num">{amount}</span></div>'
        )
    return f"""
<section aria-labelledby="h-rec">
  <h2 id="h-rec">Cobros recurrentes</h2>
  <div class="panel"><div class="big-line"><b>{eur(total)}</b> al mes · {eur(total * 12)} al año</div>
  <div class="list">{"".join(rows)}</div>
  <p class="muted small">Detectados por patrón (mismo comercio cada mes o cada año). Los «≈» varían de importe.</p></div>
</section>"""


# --------------------------------------------------------------------------- Cartera


def tab_portfolio(p: Panel, x: Extras) -> str:
    if not p.portfolio:
        body = '<div class="panel"><p class="muted">Sin precios del día todavía: se descargan con la sincronización del PC.</p></div>'
        return f'<section class="view" id="v-cartera" aria-label="Cartera"><h2>Cartera</h2>{body}</section>'
    parts = []
    for lp in p.portfolio:
        rows = []
        for pos in lp.positions:
            weight = 100 * (pos.value_cents or 0) / lp.value_cents if lp.value_cents else 0
            gain = pos.gain_cents or 0
            cls = "down" if gain >= 0 else "up"
            rows.append(f"""<div class="pos">
              <div class="row-between"><span class="pname">{esc(pos.name)}</span><span class="num">{eur(pos.value_cents)}</span></div>
              <div class="track"><div class="fill" style="width:{weight:.1f}%;background:var(--s3)"></div></div>
              <div class="row-between muted small0"><span>{weight:.0f} % · {esc(str(pos.shares.normalize()).replace('.', ','))} part.</span><span class="delta {cls}">{eur(gain, sign=True)}</span></div>
            </div>""")  # fmt: skip
        parts.append(f"""<div class="panel">
          <div class="facts">
            <div class="fact"><div class="k">Valor</div><div class="v">{eur(lp.value_cents)}</div><div class="n">cierre del {short_day(lp.as_of)}</div></div>
            <div class="fact"><div class="k">Aportado</div><div class="v">{eur(lp.cost_cents)}</div><div class="n">a precio medio</div></div>
            <div class="fact"><div class="k">Resultado</div><div class="v">{eur(lp.gain_cents, sign=True)}</div><div class="n">{str(lp.gain_pct).replace('.', ',')} %</div></div>
            <div class="fact"><div class="k">Última sesión</div><div class="v">{eur(lp.day_change_cents, sign=True)}</div><div class="n">frente al cierre anterior</div></div>
          </div></div>
          <div class="panel"><h3>Posiciones</h3><div class="positions">{"".join(rows)}</div></div>""")  # fmt: skip
    if x.contributions:
        top = max((c.plan_cents + c.manual_cents for c in x.contributions), default=1) or 1
        bars = []
        current = p.pace.month
        for c in x.contributions:
            total = c.plan_cents + c.manual_cents
            h = 100 * total / top
            short = c.month < current and c.expected_plan_cents is not None and bool(c.plan_short)
            bars.append(
                f'<div class="m"><span class="val">{eur(total)}</span>'
                f'<div class="b{" short" if short else ""}" style="height:{max(h, 2):.0f}%"></div>'
                f'<span class="lab">{MONTHS[int(c.month[5:7]) - 1]}</span></div>'
            )
        plan = sum(pl.monthly_cents for pl in x.plan)
        parts.append(f"""<div class="panel"><div class="row-between"><h3>Aportado por mes</h3><span class="muted small0">plan: {eur(plan)} al mes</span></div>
          <div class="months">{"".join(bars)}</div>
          <p class="muted small">En naranja, meses en que alguna aportación del plan quedó corta.</p></div>""")  # fmt: skip
    return f'<section class="view" id="v-cartera" aria-label="Cartera"><h2>Cartera</h2>{"".join(parts)}</section>'


# --------------------------------------------------------------------------- Hábitos

LABELS = {"pasos": "Pasos", "ejercicio": "Ejercicio", "sueno": "Sueño", "kcal": "Comido",
          "activas": "Activas", "peso": "Peso"}  # fmt: skip
UNITS = {"pasos": "", "ejercicio": " min", "sueno": " h", "kcal": " kcal", "activas": " kcal",
         "peso": " kg"}  # fmt: skip


def _fmt(key: str, v: float | None) -> str:
    if v is None:
        return "—"
    if key in ("sueno", "peso"):
        return f"{v:.1f}".replace(".", ",") + UNITS[key]
    return f"{int(v):,}".replace(",", ".") + UNITS[key]


def _habit_tiles(day: health.Day, s: health.Summary) -> str:
    tiles = []
    for key in ("pasos", "ejercicio", "sueno"):
        v = day.get(key)
        met = day.met(key)
        mark = "✓ " if met else ""
        streak = s.streaks.get(key, 0)
        note = f"racha {streak} días" if streak > 1 else f"objetivo {_fmt(key, health.GOALS[key])}"
        tiles.append(
            f'<div class="hab{" ok" if met else ""}"><span class="k">{LABELS[key]}</span>'
            f'<span class="v">{mark}{_fmt(key, v)}</span><span class="n">{note}</span></div>'
        )
    return f'<div class="habs">{"".join(tiles)}</div>'


def tab_habits(x: Extras) -> str:
    s = x.habits
    if not x.habits_file or s is None:
        body = """<div class="panel"><p>Aún no llegan datos de Salud.</p>
          <p class="muted small">Crea el atajo «Salud del día» (pasos, minutos de ejercicio, horas de sueño y kcal de Fitia) que añade cada noche una línea a <b>iCloud Drive › PersonalOS-extractos › salud › salud.txt</b>. Los pasos están en <code>docs/setup/iphone.md</code>.</p></div>"""
        return f'<section class="view" id="v-habitos" aria-label="Hábitos"><h2>Hábitos</h2>{body}</section>'
    steps = [d.get("pasos") for d in s.days]
    top = max((v for v in steps if v), default=1)
    cells = []
    first = date.fromisoformat(s.days[0].day)
    cells += ['<div class="d pad"></div>'] * first.weekday()
    for d in s.days:
        v = d.get("pasos")
        lvl = 0 if v is None else min(4, 1 + int(3.999 * v / top))
        met = " met" if d.met("pasos") else ""
        tip = f"{short_day(d.day)}|Pasos: {_fmt('pasos', v)}|Ejercicio: {_fmt('ejercicio', d.get('ejercicio'))}|Sueño: {_fmt('sueno', d.get('sueno'))}"
        cells.append(
            f'<div class="d l{lvl}{met}" data-tip="{esc(tip)}" tabindex="0"><span class="n">{date.fromisoformat(d.day).day}</span></div>'
        )
    dow = "".join(f'<div class="dow">{x}</div>' for x in DOW)
    avgs = "".join(
        f'<div class="fact"><div class="k">{LABELS[k]}</div><div class="v">{_fmt(k, s.averages.get(k))}</div><div class="n">media 28 días</div></div>'
        for k in ("pasos", "ejercicio", "sueno", "kcal")
        if k in s.averages
    )
    last = next((d for d in reversed(s.days) if d.values), None)
    tiles = _habit_tiles(last, s) if last else ""
    return f"""<section class="view" id="v-habitos" aria-label="Hábitos"><h2>Hábitos</h2>
      <div class="panel"><h3>{"Último día: " + short_day(last.day) if last else "Sin datos recientes"}</h3>{tiles}</div>
      <div class="panel"><div class="row-between"><h3>Pasos · 4 semanas</h3><span class="muted small0">✓ = objetivo de {_fmt("pasos", health.GOALS["pasos"])}</span></div>
        <div class="cal">{dow}{"".join(cells)}</div></div>
      <div class="panel"><div class="facts">{avgs}</div></div>
    </section>"""


# --------------------------------------------------------------------------- estilos y JS

CSS = """
.tabs{display:none}
html.js .view{display:none}html.js .view.on{display:grid;gap:14px}
html.js .tabs{display:block;position:fixed;left:0;right:0;bottom:0;z-index:20;background:color-mix(in srgb,var(--surface) 94%,transparent);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-top:1px solid var(--ring);padding:6px max(8px,env(safe-area-inset-right,0px)) calc(6px + env(safe-area-inset-bottom,0px)) max(8px,env(safe-area-inset-left,0px))}
.tabs nav{max-width:560px;margin:0 auto;display:grid;grid-template-columns:repeat(4,1fr)}
.tabs button{display:flex;flex-direction:column;align-items:center;gap:2px;padding:6px 0;color:var(--muted);background:none;border:0;font:500 11.5px var(--ui);cursor:pointer}
.tabs button span.i{font-size:20px;line-height:1}
.tabs button[aria-current="page"]{color:var(--s1)}
html.js .wrap{padding-bottom:calc(84px + env(safe-area-inset-bottom,0px))}
.view{display:grid;gap:14px}
.cards{display:grid;gap:12px}
.card h3,.panel h3{font-size:15px;margin:0}
.card .hero-num{font-size:34px}
.list{display:grid;margin-top:8px}
.li{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid var(--grid);font-size:14px;align-items:baseline}
.li:first-child{border-top:0}.li small{display:block;color:var(--muted);font-size:12px}.li .delta{font-size:12px}
.li .num,.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.alerts{display:grid;gap:8px;margin-top:8px}
.alert{display:grid;gap:2px;background:var(--note);border-radius:8px;padding:8px 12px;font-size:13.5px}
.habs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px}
.hab{display:grid;gap:2px;border:1px solid var(--ring);border-radius:10px;padding:8px 10px;min-width:0}
.hab.ok{border-color:var(--s3)}.hab .k{font-size:12px;color:var(--muted)}.hab .v{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}.hab .n{font-size:11.5px;color:var(--ink-2)}
.positions{display:grid;gap:10px;margin-top:8px}.pos{display:grid;gap:4px}.pname{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.small0{font-size:12.5px}
.months{display:grid;grid-template-columns:repeat(6,1fr);gap:6px;align-items:end;height:120px;margin-top:10px}
.months .m{display:flex;flex-direction:column;align-items:center;gap:4px;height:100%;justify-content:flex-end;min-width:0}
.months .b{width:100%;max-width:34px;border-radius:5px 5px 2px 2px;background:var(--s3)}.months .b.short{background:var(--s2)}
.months .lab,.months .val{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin-top:10px}
.cal .dow{font-size:11px;color:var(--muted);text-align:center}
.cal .d{aspect-ratio:1;max-width:100%;border-radius:7px;position:relative;background:var(--grid)}
.cal .d.pad{background:transparent}
.cal .d .n{position:absolute;top:2px;left:4px;font-size:10px;color:var(--ink-2)}
.cal .d.l1{background:color-mix(in srgb,var(--s1) 22%,var(--surface))}.cal .d.l2{background:color-mix(in srgb,var(--s1) 45%,var(--surface))}
.cal .d.l3{background:color-mix(in srgb,var(--s1) 70%,var(--surface))}.cal .d.l4{background:var(--s1)}
.cal .d.l3 .n,.cal .d.l4 .n{color:#fff}
.cal .d.met::after{content:"✓";position:absolute;right:4px;bottom:1px;font-size:11px;color:#fff;font-weight:700}
.agenda-row{display:grid;grid-template-columns:62px minmax(0,1fr);gap:10px;padding:6px 0;border-top:1px solid var(--grid);font-size:14px}
.agenda-row:first-child{border-top:0}.agenda-row .when{color:var(--muted);font-variant-numeric:tabular-nums;font-size:13px}
.agenda-row.late .what{color:var(--warn-ink)}
"""

JS = """
(function(){
  document.documentElement.classList.add('js');
  var views=[].slice.call(document.querySelectorAll('.view'));
  var buttons=[].slice.call(document.querySelectorAll('.tabs button'));
  function show(id){
    views.forEach(function(v){v.classList.toggle('on',v.id===id);});
    buttons.forEach(function(b){if(b.dataset.view===id)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
    try{localStorage.setItem('pos-tab',id);}catch(e){}
    window.scrollTo(0,0);
  }
  buttons.forEach(function(b){b.addEventListener('click',function(){show(b.dataset.view);});});
  var saved=null;try{saved=localStorage.getItem('pos-tab');}catch(e){}
  show(saved&&document.getElementById(saved)?saved:'v-hoy');
  // El visor de Scriptable llama a esto con la agenda del iPhone.
  window.posAgenda=function(a){
    var box=document.getElementById('agenda');if(!box)return;
    box.innerHTML='';
    function row(when,what,late){var d=document.createElement('div');d.className='agenda-row'+(late?' late':'');
      var w=document.createElement('span');w.className='when';w.textContent=when;var t=document.createElement('span');t.className='what';t.textContent=what;
      d.appendChild(w);d.appendChild(t);box.appendChild(d);}
    (a.events||[]).forEach(function(e){row(e.when,e.title,false);});
    (a.tasks||[]).forEach(function(t){row(t.late?'atrasada':'tarea','○ '+t.title,t.late);});
    if(!box.children.length){var p=document.createElement('p');p.className='muted small';p.textContent='Nada en la agenda de hoy ni tareas pendientes.';box.appendChild(p);}
  };
})();
"""

TABS = """<div class="tabs" role="navigation" aria-label="Secciones"><nav>
<button type="button" data-view="v-hoy"><span class="i" aria-hidden="true">☀️</span>Hoy</button>
<button type="button" data-view="v-dinero"><span class="i" aria-hidden="true">💶</span>Dinero</button>
<button type="button" data-view="v-cartera"><span class="i" aria-hidden="true">📈</span>Cartera</button>
<button type="button" data-view="v-habitos"><span class="i" aria-hidden="true">🏃</span>Hábitos</button>
</nav></div>"""
