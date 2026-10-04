// Personal OS — bridge de Recordatorios para Scriptable (protocolo pos-reminders/1).
//
// Ejecutor "tonto": aplica los lotes de comandos del buzón en Apple Recordatorios,
// escribe acks y un snapshot del estado. No contiene lógica de negocio (recurrencias,
// conflictos, etc. viven en el Personal OS). Ver docs/protocol-reminders-v1.md.
//
// Se despliega con `pos bridge install` (copia este fichero a la carpeta de Scriptable
// en iCloud Drive). NO editar la copia del iPhone: editar aquí y reinstalar.

const PROTOCOL = "pos-reminders/1";
const BRIDGE_VERSION = "1.1.0";
const LIST_NAME = "Personal OS";
const MAILBOX = "personal-os";
const WINDOW_DAYS = 90;
const KEEP_PROCESSED = 500;
const KEEP_SNAPSHOTS = 3;
const ACK_RETENTION_DAYS = 14;
const MARKER_RE = /\n*\[pos:([a-z]+_[0-9A-Z]{26})\]\s*$/;
const NAME_RE = /^(batch|ack|snapshot)-[A-Za-z0-9_]+\.json$/;

// ---------------------------------------------------------------------------------------
// Utilidades puras

function pad(n) {
  return String(n).padStart(2, "0");
}

function isoUtc(date) {
  return date ? date.toISOString().replace(/\.\d{3}Z$/, "Z") : null;
}

function newRunId(now) {
  const rand = Math.random().toString(36).slice(2, 10).toUpperCase();
  return "run_" + now.getTime().toString(36).toUpperCase() + rand;
}

function splitNotes(notes) {
  const text = notes || "";
  const m = text.match(MARKER_RE);
  if (!m) return { notes: text.replace(/\s+$/, ""), marker: null };
  return { notes: text.slice(0, m.index).replace(/\s+$/, ""), marker: m[1] };
}

function joinNotes(notes, marker) {
  const body = (notes || "").replace(/\s+$/, "");
  return body ? body + "\n\n[pos:" + marker + "]" : "[pos:" + marker + "]";
}

function dueFromReminder(r) {
  if (!r.dueDate) return null;
  const d = r.dueDate;
  const date = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  const time = r.dueDateIncludesTime ? pad(d.getHours()) + ":" + pad(d.getMinutes()) : null;
  return { date: date, time: time };
}

function dueToDate(due) {
  const [y, m, d] = due.date.split("-").map(Number);
  if (due.time) {
    const [hh, mm] = due.time.split(":").map(Number);
    return new Date(y, m - 1, d, hh, mm, 0, 0);
  }
  return new Date(y, m - 1, d, 0, 0, 0, 0);
}

function stateOf(r) {
  const split = splitNotes(r.notes);
  return {
    title: r.title || "",
    notes: split.notes,
    due: dueFromReminder(r),
    is_completed: !!r.isCompleted,
  };
}

function sameDue(a, b) {
  if (!a || !b) return !a && !b;
  return a.date === b.date && (a.time || null) === (b.time || null);
}

// Compara solo las claves presentes en `expected`.
function matches(state, expected) {
  for (const key of Object.keys(expected)) {
    if (key === "due") {
      if (!sameDue(state.due, expected.due)) return false;
    } else if (key === "notes") {
      if ((state.notes || "").replace(/\s+$/, "") !== (expected.notes || "").replace(/\s+$/, ""))
        return false;
    } else if (state[key] !== expected[key]) {
      return false;
    }
  }
  return true;
}

function applyFields(r, fields, marker) {
  if ("title" in fields) r.title = fields.title;
  if ("notes" in fields) r.notes = joinNotes(fields.notes, marker);
  else r.notes = joinNotes(splitNotes(r.notes).notes, marker);
  if ("due" in fields) {
    if (fields.due) {
      r.dueDate = dueToDate(fields.due);
      r.dueDateIncludesTime = !!fields.due.time;
    } else {
      r.dueDate = null;
    }
  }
  if ("is_completed" in fields) r.isCompleted = !!fields.is_completed;
}

// ---------------------------------------------------------------------------------------
// Ficheros

function mailbox(fm) {
  const root = fm.joinPath(fm.documentsDirectory(), MAILBOX);
  const dirs = {
    root: root,
    outbox: fm.joinPath(root, "outbox"),
    inbox: fm.joinPath(root, "inbox"),
  };
  for (const key of ["outbox", "inbox"]) {
    if (!fm.fileExists(dirs[key])) fm.createDirectory(dirs[key], true);
  }
  return dirs;
}

// Lista ficheros válidos del protocolo, resolviendo placeholders de iCloud (".x.json.icloud").
function listProtocolFiles(fm, dir, prefix) {
  const names = new Set();
  for (const raw of fm.listContents(dir)) {
    let name = raw;
    const placeholder = raw.match(/^\.(.+)\.icloud$/);
    if (placeholder) name = placeholder[1];
    if (NAME_RE.test(name) && name.startsWith(prefix + "-")) names.add(name);
  }
  return Array.from(names).sort();
}

async function readJson(fm, path) {
  if (!fm.isFileDownloaded(path)) await fm.downloadFileFromiCloud(path);
  return JSON.parse(fm.readString(path));
}

function writeJsonAtomic(fm, path, data) {
  const tmp = path + ".tmp";
  if (fm.fileExists(tmp)) fm.remove(tmp);
  fm.writeString(tmp, JSON.stringify(data, null, 2));
  if (fm.fileExists(path)) fm.remove(path);
  fm.move(tmp, path);
}

function loadProcessed(localFm) {
  const path = localFm.joinPath(localFm.documentsDirectory(), "personal-os-bridge-state.json");
  if (!localFm.fileExists(path)) return { path: path, processed: [] };
  try {
    return { path: path, processed: JSON.parse(localFm.readString(path)).processed || [] };
  } catch (e) {
    return { path: path, processed: [] };
  }
}

function saveProcessed(localFm, st) {
  const processed = st.processed.slice(-KEEP_PROCESSED);
  localFm.writeString(st.path, JSON.stringify({ processed: processed }));
}

// Limpieza del inbox: el PC solo lee y registra lo procesado; aquí se borra lo antiguo.
// - snapshots: se conservan los KEEP_SNAPSHOTS más recientes;
// - acks: se borran pasados ACK_RETENTION_DAYS (si el PC no llegó a leerlos, el snapshot
//   permite re-enlazar por marcador).
function cleanupInbox(fm, inbox, now) {
  const snaps = listProtocolFiles(fm, inbox, "snapshot");
  for (const name of snaps.slice(0, Math.max(0, snaps.length - KEEP_SNAPSHOTS))) {
    const p = fm.joinPath(inbox, name);
    if (fm.fileExists(p)) fm.remove(p);
  }
  const limit = now.getTime() - ACK_RETENTION_DAYS * 86400000;
  for (const name of listProtocolFiles(fm, inbox, "ack")) {
    const p = fm.joinPath(inbox, name);
    if (fm.fileExists(p) && fm.modificationDate(p) && fm.modificationDate(p).getTime() < limit) {
      fm.remove(p);
    }
  }
}

// ---------------------------------------------------------------------------------------
// Recordatorios

async function findList() {
  const lists = (await Calendar.forReminders()).filter((c) => c.title === LIST_NAME);
  if (lists.length > 1) throw new Error("Hay " + lists.length + " listas llamadas '" + LIST_NAME + "'");
  return lists[0] || null;
}

function indexReminders(reminders) {
  const byId = new Map();
  const byMarker = new Map();
  for (const r of reminders) {
    byId.set(r.identifier, r);
    const marker = splitNotes(r.notes).marker;
    if (marker && !byMarker.has(marker)) byMarker.set(marker, r);
  }
  return { byId: byId, byMarker: byMarker };
}

function locate(index, cmd) {
  if (cmd.external_id && index.byId.has(cmd.external_id)) return index.byId.get(cmd.external_id);
  if (cmd.marker && index.byMarker.has(cmd.marker)) return index.byMarker.get(cmd.marker);
  return null;
}

function result(cmd, status, r, error) {
  return {
    op_id: cmd.op_id,
    status: status,
    marker: cmd.marker || null,
    external_id: r ? r.identifier : cmd.external_id || null,
    state: r && status !== "deleted" ? stateOf(r) : null,
    error: error || null,
  };
}

function applyCommand(cmd, list, index) {
  const existing = locate(index, cmd);
  if (cmd.type === "upsert") {
    if (!existing) {
      if (cmd.expected) return result(cmd, "not_found", null);
      const r = new Reminder();
      r.calendar = list;
      applyFields(r, cmd.fields, cmd.marker);
      r.save();
      index.byId.set(r.identifier, r);
      index.byMarker.set(cmd.marker, r);
      return result(cmd, "created", r);
    }
    const current = stateOf(existing);
    if (matches(current, cmd.fields)) {
      if (splitNotes(existing.notes).marker !== cmd.marker) {
        applyFields(existing, {}, cmd.marker);
        existing.save();
      }
      return result(cmd, "applied", existing);
    }
    if (cmd.expected && !matches(current, cmd.expected)) return result(cmd, "conflict", existing);
    applyFields(existing, cmd.fields, cmd.marker);
    existing.save();
    return result(cmd, "applied", existing);
  }
  if (cmd.type === "delete") {
    if (!existing) return result(cmd, "not_found", null);
    if (cmd.expected && !matches(stateOf(existing), cmd.expected))
      return result(cmd, "conflict", existing);
    const id = existing.identifier;
    existing.remove();
    index.byId.delete(id);
    if (cmd.marker) index.byMarker.delete(cmd.marker);
    return { op_id: cmd.op_id, status: "deleted", marker: cmd.marker || null, external_id: id, state: null, error: null };
  }
  return result(cmd, "error", null, "Tipo de comando desconocido: " + cmd.type);
}

// ---------------------------------------------------------------------------------------
// Ejecución

async function run(now) {
  const fm = FileManager.iCloud();
  const localFm = FileManager.local();
  const dirs = mailbox(fm);
  const runId = newRunId(now);
  const summary = { run_id: runId, batches: 0, commands: 0, conflicts: 0, errors: 0, reminders: 0 };

  let list = null;
  let listError = null;
  try {
    list = await findList();
  } catch (e) {
    listError = String(e.message || e);
  }

  const st = loadProcessed(localFm);
  const appliedBatches = [];

  if (list) {
    const index = indexReminders(await Reminder.all([list]));
    for (const name of listProtocolFiles(fm, dirs.outbox, "batch")) {
      const path = fm.joinPath(dirs.outbox, name);
      let batch;
      try {
        batch = await readJson(fm, path);
      } catch (e) {
        summary.errors += 1;
        continue; // fichero aún sincronizándose o corrupto: se reintenta en la próxima ejecución
      }
      if (batch.protocol !== PROTOCOL) {
        summary.errors += 1;
        continue;
      }
      if (st.processed.includes(batch.batch_id)) {
        if (fm.fileExists(path)) fm.remove(path);
        continue;
      }
      const results = [];
      for (const cmd of batch.commands || []) {
        let res;
        try {
          res = applyCommand(cmd, list, index);
        } catch (e) {
          res = result(cmd, "error", null, String(e.message || e));
        }
        if (res.status === "conflict") summary.conflicts += 1;
        if (res.status === "error") summary.errors += 1;
        results.push(res);
      }
      writeJsonAtomic(fm, fm.joinPath(dirs.inbox, "ack-" + batch.batch_id + ".json"), {
        protocol: PROTOCOL,
        batch_id: batch.batch_id,
        run_id: runId,
        bridge_version: BRIDGE_VERSION,
        processed_at: isoUtc(new Date()),
        results: results,
      });
      st.processed.push(batch.batch_id);
      saveProcessed(localFm, st);
      // El PC no puede borrar en iCloud Drive (limitación de iCloud para Windows):
      // la limpieza del buzón es responsabilidad del bridge.
      if (fm.fileExists(path)) fm.remove(path);
      appliedBatches.push(batch.batch_id);
      summary.batches += 1;
      summary.commands += results.length;
    }
  }

  // Snapshot final: incompletos + completados dentro de la ventana.
  let reminders = [];
  if (list) {
    const since = new Date(now.getTime() - WINDOW_DAYS * 86400000);
    const seen = new Set();
    const incomplete = await Reminder.allIncomplete([list]);
    const completed = await Reminder.completedBetween(since, new Date(now.getTime() + 86400000), [list]);
    for (const r of incomplete.concat(completed)) {
      if (seen.has(r.identifier)) continue;
      seen.add(r.identifier);
      reminders.push({
        external_id: r.identifier,
        marker: splitNotes(r.notes).marker,
        state: stateOf(r),
        completion_date: isoUtc(r.completionDate),
        creation_date: isoUtc(r.creationDate),
      });
    }
  }
  summary.reminders = reminders.length;

  let tz = null;
  try {
    tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch (e) {
    tz = null;
  }

  writeJsonAtomic(fm, fm.joinPath(dirs.inbox, "snapshot-" + runId + ".json"), {
    protocol: PROTOCOL,
    run_id: runId,
    bridge_version: BRIDGE_VERSION,
    taken_at: isoUtc(new Date()),
    list_name: LIST_NAME,
    list_found: !!list,
    list_error: listError,
    device_timezone: tz,
    window_days: WINDOW_DAYS,
    applied_batches: appliedBatches,
    reminders: reminders,
  });

  cleanupInbox(fm, dirs.inbox, now);

  if (listError || !list) summary.error = listError || "No existe la lista '" + LIST_NAME + "'";
  return summary;
}

async function main() {
  let summary;
  try {
    summary = await run(new Date());
  } catch (e) {
    summary = { error: String(e.message || e) };
  }
  const text = summary.error
    ? "Personal OS: ERROR — " + summary.error
    : "Personal OS: " + summary.batches + " lotes, " + summary.commands + " comandos, " +
      summary.conflicts + " conflictos, " + summary.errors + " errores, " +
      summary.reminders + " recordatorios";
  if (config.runsInApp) {
    const alert = new Alert();
    alert.title = "Personal OS sync";
    alert.message = text;
    alert.addAction("OK");
    await alert.present();
  }
  Script.setShortcutOutput(text);
  Script.complete();
  return summary;
}

await main();
