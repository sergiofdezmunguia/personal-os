// Variables used by Scriptable.
// These must be at the top of the file. Do not edit.
// icon-color: deep-blue; icon-glyph: heartbeat;
// Personal OS Widget: ritmo de gasto del mes (lo calcula el PC en <buzón>/panel/widget.json)
// + citas del Calendario y tareas de Recordatorios de hoy (leídas en vivo del iPhone).
// Tocarlo abre el panel completo. Solo lectura. Instalado por `pos bridge install`.

const MAILBOX = "personal-os";
const PANEL_SCRIPT = "Personal OS Panel";
const C = {
  bg: Color.dynamic(new Color("#ffffff"), new Color("#171a20")),
  ink: Color.dynamic(new Color("#12151b"), new Color("#eef1f5")),
  muted: Color.dynamic(new Color("#6f7685"), new Color("#8c93a1")),
  track: Color.dynamic(new Color("#e7ebf1"), new Color("#2a2f39")),
  accent: Color.dynamic(new Color("#2a78d6"), new Color("#3987e5")),
  good: Color.dynamic(new Color("#0a7d0a"), new Color("#5fd35f")),
  crit: Color.dynamic(new Color("#c23232"), new Color("#f07a7a")),
};
const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];

const eur = (cents) => Math.round(cents / 100).toLocaleString("es-ES") + " €";
const hm = (d) => `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;

async function loadMoney() {
  const fm = FileManager.iCloud();
  const path = fm.joinPath(fm.documentsDirectory(), MAILBOX + "/panel/widget.json");
  if (!fm.fileExists(path)) return null;
  try {
    await fm.downloadFileFromiCloud(path);
    return JSON.parse(fm.readString(path));
  } catch (e) {
    return null;
  }
}

async function loadAgenda() {
  const now = new Date();
  let events = [];
  try {
    events = (await CalendarEvent.today([])).concat(await CalendarEvent.tomorrow([]));
  } catch (e) {}
  events = events
    .filter((e) => e.isAllDay || e.endDate > now)
    .sort((a, b) => a.startDate - b.startDate);
  let tasks = [];
  try {
    tasks = (await Reminder.allDueToday()).filter((r) => !r.isCompleted);
    const ids = new Set(tasks.map((r) => r.identifier));
    const late = (await Reminder.allIncomplete()).filter((r) => r.dueDate && r.dueDate < now && !ids.has(r.identifier));
    tasks = late.concat(tasks);
  } catch (e) {}
  return { events, tasks };
}

function bar(width, ratio, mark) {
  const h = 8;
  const ctx = new DrawContext();
  ctx.size = new Size(width, h + 6);
  ctx.opaque = false;
  ctx.respectScreenScale = true;
  const track = new Path();
  track.addRoundedRect(new Rect(0, 3, width, h), 4, 4);
  ctx.addPath(track);
  ctx.setFillColor(C.track);
  ctx.fillPath();
  const fill = new Path();
  fill.addRoundedRect(new Rect(0, 3, Math.max(h, width * Math.min(1, ratio)), h), 4, 4);
  ctx.addPath(fill);
  ctx.setFillColor(ratio > mark ? C.crit : C.accent);
  ctx.fillPath();
  ctx.setFillColor(C.ink);
  ctx.fillRect(new Rect(Math.min(width - 2, width * mark), 0, 2, h + 6));
  return ctx.getImage();
}

function text(stack, s, size, color, weight) {
  const t = stack.addText(s);
  t.font = weight === "bold" ? Font.boldSystemFont(size) : weight === "mono" ? Font.mediumMonospacedSystemFont(size) : Font.systemFont(size);
  t.textColor = color || C.ink;
  t.lineLimit = 1;
  return t;
}

function moneyBlock(stack, m, width) {
  if (!m) {
    text(stack, "Sin datos del PC", 12, C.muted);
    text(stack, "Se generan en cada sincronización", 10, C.muted);
    return;
  }
  const [, mm] = m.month.split("-");
  text(stack, `Gastado en ${MONTHS[parseInt(mm, 10) - 1]}`, 11, C.muted);
  text(stack, eur(m.variable_cents), 24, C.ink, "bold");
  stack.addSpacer(4);
  const total = Math.max(1, m.variable_habit_cents);
  stack.addImage(bar(width, m.variable_cents / total, m.variable_habit_to_day_cents / total)).imageSize = new Size(width, 14);
  stack.addSpacer(3);
  const diff = m.variable_habit_to_day_cents - m.variable_cents;
  text(stack, `${diff >= 0 ? "−" : "+"}${eur(Math.abs(diff))} vs tu ritmo`, 11, diff >= 0 ? C.good : C.crit);
}

function agendaBlock(stack, a, maxEvents, maxTasks) {
  const now = new Date();
  const today = now.toDateString();
  text(stack, "Hoy", 11, C.muted);
  const shown = a.events.slice(0, maxEvents);
  if (!shown.length) text(stack, "Sin citas", 12, C.muted);
  for (const e of shown) {
    const when = e.isAllDay ? "todo el día" : hm(e.startDate);
    const day = e.startDate.toDateString() === today ? "" : "mañana ";
    const row = stack.addStack();
    row.spacing = 4;
    text(row, day + when, 11, C.muted, "mono");
    text(row, e.title, 12, C.ink);
  }
  if (maxTasks > 0) {
    stack.addSpacer(4);
    const tasks = a.tasks.slice(0, maxTasks);
    if (!a.tasks.length) text(stack, "✓ Nada pendiente hoy", 11, C.good);
    for (const r of tasks) {
      const late = r.dueDate && r.dueDate < new Date(now.getFullYear(), now.getMonth(), now.getDate());
      text(stack, "○ " + r.title, 12, late ? C.crit : C.ink);
    }
    if (a.tasks.length > tasks.length) text(stack, `+${a.tasks.length - tasks.length} más`, 10, C.muted);
  }
}

async function build(family) {
  const [m, a] = await Promise.all([loadMoney(), family === "small" ? null : loadAgenda()]);
  const w = new ListWidget();
  w.backgroundColor = C.bg;
  w.setPadding(14, 14, 14, 14);
  w.url = `scriptable:///run/${encodeURIComponent(PANEL_SCRIPT)}`;
  w.refreshAfterDate = new Date(Date.now() + 30 * 60 * 1000);
  if (family === "small") {
    moneyBlock(w, m, 120);
    w.addSpacer();
    if (m) text(w, `Patrimonio ${eur(m.net_worth_cents)}`, 10, C.muted);
    return w;
  }
  const row = w.addStack();
  row.layoutHorizontally();
  const left = row.addStack();
  left.layoutVertically();
  left.size = new Size(family === "large" ? 0 : 140, 0);
  moneyBlock(left, m, 130);
  if (m) { left.addSpacer(6); text(left, `Patrimonio ${eur(m.net_worth_cents)}`, 10, C.muted); }
  row.addSpacer(12);
  const right = row.addStack();
  right.layoutVertically();
  agendaBlock(right, a, family === "large" ? 6 : 2, family === "large" ? 6 : 2);
  if (family === "large" && m && m.holdings.length) {
    w.addSpacer(10);
    text(w, "Dónde está tu dinero", 11, C.muted);
    for (const h of m.holdings) {
      const r = w.addStack();
      text(r, h.label, 12, C.ink);
      r.addSpacer();
      text(r, eur(h.cents), 12, C.ink, "mono");
    }
  }
  w.addSpacer();
  if (m) text(w, `Datos del PC: ${m.generated_at.slice(11, 16)}`, 9, C.muted);
  return w;
}

const family = config.widgetFamily || "medium";
const widget = await build(family);
if (config.runsInWidget) Script.setWidget(widget);
else await widget.presentMedium();
Script.complete();
