// Simulador mínimo de la API de Scriptable para ejecutar personal-os-sync.js fuera del iPhone.
//
// Uso (CLI):
//   TZ=Europe/Madrid node scriptable-sim.mjs --root <dir> [--script <ruta.js>]
//
// <dir>/icloud   → FileManager.iCloud().documentsDirectory()
// <dir>/local    → FileManager.local().documentsDirectory()
// <dir>/store.json → estado de Recordatorios: { calendars: [{identifier,title}], reminders: [...] }
//
// Imprime en stdout el resumen devuelto por Script.setShortcutOutput.
// Solo implementa lo que usa el bridge; si el bridge empieza a usar algo nuevo, el simulador
// fallará de forma explícita (TypeError) en lugar de comportarse distinto al iPhone.

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

function makeFileManager(baseDir) {
  fs.mkdirSync(baseDir, { recursive: true });
  return {
    documentsDirectory: () => baseDir,
    joinPath: (a, b) => path.join(a, b),
    fileExists: (p) => fs.existsSync(p),
    createDirectory: (p, intermediate) => fs.mkdirSync(p, { recursive: !!intermediate }),
    listContents: (p) => fs.readdirSync(p),
    isFileDownloaded: () => true,
    downloadFileFromiCloud: async () => undefined,
    readString: (p) => fs.readFileSync(p, "utf8"),
    writeString: (p, s) => fs.writeFileSync(p, s, "utf8"),
    remove: (p) => fs.rmSync(p, { force: true }),
    move: (a, b) => fs.renameSync(a, b),
    modificationDate: (p) => (fs.existsSync(p) ? fs.statSync(p).mtime : null),
  };
}

export function createEnvironment(root) {
  const storePath = path.join(root, "store.json");
  const store = fs.existsSync(storePath)
    ? JSON.parse(fs.readFileSync(storePath, "utf8"))
    : { calendars: [], reminders: [] };

  const calendars = store.calendars.map((c) => ({ identifier: c.identifier, title: c.title }));
  const calById = new Map(calendars.map((c) => [c.identifier, c]));

  class Reminder {
    constructor(raw) {
      this._raw = raw || {
        identifier: null,
        calendar: null,
        title: "",
        notes: "",
        dueDate: null,
        dueDateIncludesTime: true,
        isCompleted: false,
        completionDate: null,
        creationDate: null,
      };
    }
    get identifier() { return this._raw.identifier; }
    get title() { return this._raw.title; }
    set title(v) { this._raw.title = v; }
    get notes() { return this._raw.notes; }
    set notes(v) { this._raw.notes = v; }
    get dueDate() { return this._raw.dueDate ? new Date(this._raw.dueDate) : null; }
    set dueDate(v) { this._raw.dueDate = v ? v.toISOString() : null; }
    get dueDateIncludesTime() { return this._raw.dueDateIncludesTime; }
    set dueDateIncludesTime(v) { this._raw.dueDateIncludesTime = !!v; }
    get isCompleted() { return this._raw.isCompleted; }
    set isCompleted(v) {
      this._raw.isCompleted = !!v;
      this._raw.completionDate = v ? new Date().toISOString() : null;
    }
    get completionDate() { return this._raw.completionDate ? new Date(this._raw.completionDate) : null; }
    get creationDate() { return this._raw.creationDate ? new Date(this._raw.creationDate) : null; }
    get calendar() { return calById.get(this._raw.calendar) || null; }
    set calendar(c) { this._raw.calendar = c.identifier; }
    save() {
      if (!this._raw.identifier) {
        this._raw.identifier = crypto.randomUUID().toUpperCase();
        this._raw.creationDate = new Date().toISOString();
        store.reminders.push(this._raw);
      }
    }
    remove() {
      store.reminders = store.reminders.filter((r) => r.identifier !== this._raw.identifier);
    }
    static _in(cals) {
      const ids = new Set((cals || calendars).map((c) => c.identifier));
      return store.reminders.filter((r) => ids.has(r.calendar)).map((r) => new Reminder(r));
    }
    static async all(cals) { return Reminder._in(cals); }
    static async allIncomplete(cals) { return Reminder._in(cals).filter((r) => !r.isCompleted); }
    static async completedBetween(start, end, cals) {
      return Reminder._in(cals).filter(
        (r) => r.isCompleted && r.completionDate >= start && r.completionDate <= end,
      );
    }
  }

  const output = { value: null };
  const context = {
    console,
    Date,
    Math,
    JSON,
    Intl,
    Map,
    Set,
    Array,
    Object,
    String,
    Number,
    Error,
    Promise,
    FileManager: {
      iCloud: () => makeFileManager(path.join(root, "icloud")),
      local: () => makeFileManager(path.join(root, "local")),
    },
    Calendar: { forReminders: async () => calendars },
    Reminder,
    Alert: class { addAction() {} async present() {} },
    Script: { setShortcutOutput: (v) => { output.value = v; }, complete: () => {} },
    config: { runsInApp: false, runsWithSiri: false },
  };

  return {
    context,
    output,
    persist: () => fs.writeFileSync(storePath, JSON.stringify(store, null, 2)),
  };
}

export async function runBridge(root, scriptPath) {
  const env = createEnvironment(root);
  const code = fs.readFileSync(scriptPath, "utf8");
  const wrapped = "(async () => {\n" + code + "\n})()";
  await vm.runInNewContext(wrapped, env.context, { filename: scriptPath });
  env.persist();
  return env.output.value;
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isMain) {
  const args = process.argv.slice(2);
  const get = (flag, dflt) => {
    const i = args.indexOf(flag);
    return i >= 0 ? args[i + 1] : dflt;
  };
  const here = path.dirname(fileURLToPath(import.meta.url));
  const root = get("--root");
  if (!root) {
    console.error("Falta --root");
    process.exit(2);
  }
  const script = get("--script", path.join(here, "..", "personal-os-sync.js"));
  const out = await runBridge(root, script);
  process.stdout.write(String(out) + "\n");
}
