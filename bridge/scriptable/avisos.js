// Variables used by Scriptable.
// These must be at the top of the file. Do not edit.
// icon-color: orange; icon-glyph: bell;
// Personal OS Avisos: muestra como notificación los avisos que deja el PC en
// <buzón>/panel/avisos.json (buenos días, resumen semanal, subidas de cuotas…). Lo llama
// «Personal OS Sync» al terminar (automatizaciones de Atajos) y se puede ejecutar a mano.
// Cada aviso se muestra una sola vez y solo dentro de su ventana. Instalado por `pos bridge install`.

const MAILBOX = "personal-os";
const PANEL_SCRIPT = "Personal OS Panel";
const STATE_FILE = "personal-os-avisos.json";
const KEEP_DAYS = 30;

const hm = (d) => `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;

// Avisos que tocan ahora: dentro de su ventana y no mostrados antes.
function due(notices, delivered, now) {
  return (notices || []).filter((n) => n && n.id && !delivered[n.id]
    && new Date(n.deliver_after) <= now && now <= new Date(n.expires));
}

// «Hoy: 12:30 🩺 Médico · todo el día 🎉 Festivo · 2 tareas».
function agendaLine(events, tasks) {
  const ev = (events || []).slice().sort((a, b) => (b.isAllDay - a.isAllDay) || (a.startDate - b.startDate));
  const parts = ev.slice(0, 4).map((e) => (e.isAllDay ? "" : hm(e.startDate) + " ") + e.title);
  if (ev.length > 4) parts.push(`+${ev.length - 4} más`);
  const n = (tasks || []).length;
  if (n) parts.push(`${n} tarea${n === 1 ? "" : "s"}`);
  return parts.length ? "Hoy: " + parts.join(" · ") : "Hoy no tienes nada en la agenda.";
}

function prune(delivered, now) {
  const limit = now.getTime() - KEEP_DAYS * 86400000;
  const out = {};
  for (const [id, at] of Object.entries(delivered || {})) if (new Date(at).getTime() >= limit) out[id] = at;
  return out;
}

async function loadNotices() {
  const fm = FileManager.iCloud();
  const path = fm.joinPath(fm.documentsDirectory(), MAILBOX + "/panel/avisos.json");
  if (!fm.fileExists(path)) return [];
  await fm.downloadFileFromiCloud(path);
  return JSON.parse(fm.readString(path)).notices || [];
}

async function todayAgenda() {
  let events = [], tasks = [];
  try { events = await CalendarEvent.today([]); } catch (e) {}
  try { tasks = (await Reminder.allDueToday()).filter((r) => !r.isCompleted); } catch (e) {}
  return agendaLine(events, tasks);
}

async function deliver(now = new Date()) {
  const local = FileManager.local();
  const statePath = local.joinPath(local.documentsDirectory(), STATE_FILE);
  let delivered = {};
  try { if (local.fileExists(statePath)) delivered = JSON.parse(local.readString(statePath)).delivered || {}; } catch (e) {}
  const todo = due(await loadNotices(), delivered, now);
  for (const n of todo) {
    const note = new Notification();
    note.title = n.title;
    note.body = n.agenda ? `${n.body}\n${await todayAgenda()}` : n.body;
    note.threadIdentifier = "personal-os";
    note.identifier = "pos-" + n.id;
    note.openURL = `scriptable:///run/${encodeURIComponent(PANEL_SCRIPT)}`;
    await note.schedule();
    delivered[n.id] = now.toISOString();
  }
  local.writeString(statePath, JSON.stringify({ delivered: prune(delivered, now) }));
  return todo.length;
}

async function main() {
  let text;
  try {
    const n = await deliver();
    text = n ? `Personal OS: ${n} aviso${n === 1 ? "" : "s"}` : "Personal OS: sin avisos nuevos";
  } catch (e) {
    text = "Personal OS avisos: ERROR — " + (e.message || e);
  }
  Script.setShortcutOutput(text);
  Script.complete();
}

const IN_SCRIPTABLE = typeof Script !== "undefined" && typeof Notification !== "undefined";
if (typeof module !== "undefined" && module) module.exports = { due, agendaLine, prune, deliver };
// Importado desde «Personal OS Sync» no se ejecuta solo: lo llama con deliver().
if (IN_SCRIPTABLE && Script.name() === "Personal OS Avisos") await main();
