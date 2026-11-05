// Variables used by Scriptable.
// These must be at the top of the file. Do not edit.
// icon-color: deep-blue; icon-glyph: chart-pie;
// Personal OS Panel: abre el panel que genera el PC (<buzón>/panel/index.html)
// en un visor web con JavaScript y le pasa la agenda de hoy (Calendario y Recordatorios del
// iPhone; el PC no la tiene). Solo lectura; no envía nada a ningún sitio.
const hm = (d) => `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;

async function todayAgenda() {
  const now = new Date();
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  let events = [], tasks = [];
  try {
    events = (await CalendarEvent.today([])).filter((e) => e.isAllDay || e.endDate > now)
      .sort((a, b) => (b.isAllDay - a.isAllDay) || (a.startDate - b.startDate))
      .map((e) => ({ when: e.isAllDay ? "todo el día" : hm(e.startDate), title: e.title }));
  } catch (e) {}
  try {
    const due = (await Reminder.allDueToday()).filter((r) => !r.isCompleted);
    const ids = new Set(due.map((r) => r.identifier));
    const late = (await Reminder.allIncomplete()).filter((r) => r.dueDate && r.dueDate < midnight && !ids.has(r.identifier));
    tasks = late.map((r) => ({ title: r.title, late: true })).concat(due.map((r) => ({ title: r.title, late: false })));
  } catch (e) {}
  return { events, tasks };
}

const fm = FileManager.iCloud();
const MAILBOX = "personal-os";
const path = fm.joinPath(fm.documentsDirectory(), MAILBOX + "/panel/index.html");
if (!fm.fileExists(path)) {
  const a = new Alert();
  a.title = "Panel no encontrado";
  a.message = "Aún no hay panel en iCloud. Se genera en el PC con cada sincronización.";
  a.addAction("OK");
  await a.present();
} else {
  await fm.downloadFileFromiCloud(path);
  const wv = new WebView();
  await wv.loadHTML(fm.readString(path));
  try {
    const agenda = await todayAgenda();
    await wv.evaluateJavaScript(`window.posAgenda && window.posAgenda(${JSON.stringify(agenda)})`);
  } catch (e) {}
  await wv.present(true);
}
Script.complete();
