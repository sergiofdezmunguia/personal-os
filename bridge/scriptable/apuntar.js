// Variables used by Scriptable.
// These must be at the top of the file. Do not edit.
// icon-color: red; icon-glyph: calendar-plus;
// Apuntar: «tengo médico el martes 24 a las 12:30» → evento «🩺 Médico» en el Calendario
// del iPhone. Todo en el iPhone: sin red y sin IA (reglas fijas, ver parseEvent).
// Uso: desde Atajos/Siri (Dictar texto → Ejecutar script «Apuntar» con el texto) o
// abriendo el script y escribiendo. Instalado por `pos bridge install`.

const CALENDAR_NAME = ""; // vacío = el calendario predeterminado del iPhone

// Categoría: emoji, palabras clave (sin tildes), duración en minutos (0 = todo el día).
const CATEGORIES = [
  ["🩺", ["medico", "medica", "doctor", "doctora", "dentista", "fisio", "analisis", "hospital",
    "centro de salud", "consulta", "revision", "oculista", "psicolog", "vacuna", "urgencias"], 60],
  ["✈️", ["vuelo", "avion", "aeropuerto", "embarque"], 120],
  ["🚆", ["tren", "ave ", "renfe", "autobus", "guagua", "ferry", "barco"], 120],
  ["🎬", ["cine", "pelicula"], 150],
  ["🎵", ["concierto", "festival", "fiesta", "discoteca", "teatro", "musical"], 180],
  ["🍽️", ["comida", "comer", "cena", "cenar", "almuerzo", "desayuno", "brunch", "restaurante"], 120],
  ["🍻", ["quedada", "quedar", "copas", "cerveza", "tomar algo", "afterwork"], 120],
  ["🏋️", ["gym", "gimnasio", "entreno", "entrenar", "padel", "partido", "futbol", "baloncesto",
    "basket", "correr", "yoga", "crossfit", "piscina"], 90],
  ["💼", ["reunion", "trabajo", "oficina", "entrevista", "call", "llamada", "cliente"], 60],
  ["🎓", ["clase", "master", "bootcamp", "examen", "curso", "tutoria", "ingles"], 60],
  ["🎂", ["cumple", "aniversario", "boda", "bautizo"], 0],
  ["💈", ["peluqueria", "barberia", "corte de pelo", "unas", "manicura"], 60],
  ["📌", ["banco", "notaria", "itv", "dni", "pasaporte", "hacienda", "ayuntamiento",
    "cita previa", "gestoria", "seguro", "taller", "abogado"], 60],
];
const DEFAULT = ["📅", [], 60];

const WEEKDAYS = { domingo: 0, lunes: 1, martes: 2, miercoles: 3, jueves: 4, viernes: 5, sabado: 6 };
const MONTHS = { enero: 0, febrero: 1, marzo: 2, abril: 3, mayo: 4, junio: 5, julio: 6,
  agosto: 7, septiembre: 8, setiembre: 8, octubre: 9, noviembre: 10, diciembre: 11 };
const NUMBERS = { una: 1, uno: 1, dos: 2, tres: 3, cuatro: 4, cinco: 5, seis: 6, siete: 7,
  ocho: 8, nueve: 9, diez: 10, once: 11, doce: 12 };

const plain = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

// Las tildes se quitan para buscar, conservando posiciones: cada letra con tilde es 1 carácter.
function fold(s) {
  return Array.from(s).map((c) => (plain(c) || c)[0]).join("");
}

function addDays(d, n) {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() + n);
  return x;
}

// Devuelve {title, emoji, start, end, allDay, warnings} o {error}.
function parseEvent(text, now) {
  const original = String(text || "").trim().replace(/\s+/g, " ");
  if (!original) return { error: "No has dicho nada." };
  let t = fold(original); // misma longitud que original
  const cut = []; // tramos a quitar del título
  const take = (m) => { if (m) cut.push([m.index, m.index + m[0].length]); return m; };
  const warnings = [];
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

  // --- franja del día (antes que «mañana» = día siguiente)
  let period = null;
  let m = take(/\b(de|por) la (manana|tarde|noche)\b/.exec(t));
  if (m) { period = m[2]; t = t.slice(0, m.index) + " ".repeat(m[0].length) + t.slice(m.index + m[0].length); }
  let noon = take(/\b(a )?mediodia\b/.exec(t));

  // --- día
  let day = null;
  if ((m = take(/\bpasado manana\b/.exec(t)))) day = addDays(today, 2);
  else if ((m = take(/\bmanana\b/.exec(t)))) day = addDays(today, 1);
  else if ((m = take(/\bhoy\b/.exec(t)))) day = today;

  const wdRe = /\b(el |este |esta |el proximo |el que viene )?(lunes|martes|miercoles|jueves|viernes|sabado|domingo)( que viene| proximo)?\b/;
  const wd = take(wdRe.exec(t));
  const weekday = wd ? WEEKDAYS[wd[2]] : null;

  const numRe = /\b(el )?(dia )?(\d{1,2})(?:\/(\d{1,2})(?:\/(\d{2,4}))?| de (enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)(?: de (\d{4}))?)?\b(?! ?(?::|\.|h\b|horas?\b|y |menos ))/;
  // El número de día no puede ser la hora: se busca fuera de «a las …».
  const masked = t
    .replace(/\bde \d{1,2}(?:[:.]\d{2})? (?:a|hasta) (?:las? )?\d{1,2}(?:[:.]\d{2})?/g, (s) => " ".repeat(s.length))
    .replace(/\ba (las?|eso de las?) [^ ]+( y (media|cuarto)| menos cuarto)?/g, (s) => " ".repeat(s.length));
  const dn = numRe.exec(masked);
  if (dn && !day) {
    take(dn);
    const n = parseInt(dn[3], 10);
    let month = dn[4] ? parseInt(dn[4], 10) - 1 : dn[6] ? MONTHS[dn[6]] : null;
    let year = dn[5] ? parseInt(dn[5].length === 2 ? "20" + dn[5] : dn[5], 10) : dn[7] ? parseInt(dn[7], 10) : null;
    if (n < 1 || n > 31 || (month !== null && (month < 0 || month > 11))) return { error: "El día no es válido." };
    if (month === null) {
      // «el 24» / «martes 24»: el próximo mes (desde hoy) con ese día y, si se dijo, ese día de la semana.
      for (let k = 0; k < (weekday === null ? 13 : 3) && !day; k++) {
        const c = new Date(today.getFullYear(), today.getMonth() + k, n);
        if (c.getDate() !== n || c < today) continue;
        if (weekday === null || c.getDay() === weekday) day = c;
      }
      if (!day) return { error: `No encuentro un ${Object.keys(WEEKDAYS)[weekday]} ${n} próximo.` };
    } else {
      if (year === null) {
        year = today.getFullYear();
        if (new Date(year, month, n) < today) year += 1;
      }
      day = new Date(year, month, n);
      if (day.getDate() !== n) return { error: "Esa fecha no existe." };
      if (weekday !== null && day.getDay() !== weekday) warnings.push("El día de la semana no coincide con la fecha; uso la fecha.");
    }
  } else if (weekday !== null && !day) {
    let diff = (weekday - today.getDay() + 7) % 7;
    if (diff === 0) diff = 7;
    day = addDays(today, diff);
  }
  if (!day) return { error: "No he entendido el día. Prueba con «el martes 24» o «mañana»." };

  // --- hora
  let hour = null, minute = 0;
  const timeRe = /\b(?:a (?:las?|eso de las?) |sobre las? |de )?(\d{1,2}|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)(?:(?::|\.|h)(\d{2})|( y media| y cuarto| menos cuarto))?(?: ?h(?:oras)?\b)?/g;
  let tm;
  const found = [];
  while ((tm = timeRe.exec(t))) {
    const lead = /^(a |sobre |de )/.test(tm[0]);
    const explicit = tm[2] !== undefined || tm[3] !== undefined || lead;
    if (!explicit) continue;
    if (cut.some(([a, b]) => tm.index >= a && tm.index < b)) continue; // es el día
    found.push(tm);
  }
  const toHM = (x) => {
    let h = NUMBERS[x[1]] || parseInt(x[1], 10);
    let mi = x[2] ? parseInt(x[2], 10) : 0;
    if (x[3] === " y media") mi = 30;
    else if (x[3] === " y cuarto") mi = 15;
    else if (x[3] === " menos cuarto") { h -= 1; mi = 45; }
    return [h, mi];
  };
  let endHM = null;
  if (found.length) {
    take(found[0]);
    [hour, minute] = toHM(found[0]);
    // «de 10 a 12», «hasta las 12»
    const range = /^(?: a | hasta )(?:las? )?(\d{1,2})(?:(?::|\.)(\d{2}))?/.exec(t.slice(found[0].index + found[0][0].length));
    if (range) {
      endHM = [parseInt(range[1], 10), range[2] ? parseInt(range[2], 10) : 0];
      cut.push([found[0].index + found[0][0].length, found[0].index + found[0][0].length + range[0].length]);
    }
  } else if (noon) { hour = 14; minute = 0; }
  if (hour !== null) {
    if (hour > 23 || minute > 59) return { error: "La hora no es válida." };
    const pm = period === "tarde" || (period === "noche" && hour >= 5);
    if (pm && hour < 12) hour += 12;
    else if (period === null && hour >= 1 && hour <= 7 && !/vuelo|avion|aeropuerto|tren|renfe|autobus|guagua|ferry/.test(plain(original))) {
      hour += 12; // «a las 5» casi siempre es por la tarde
      warnings.push("Entiendo que es por la tarde.");
    }
    if (endHM && endHM[0] < hour && endHM[0] + 12 <= 23) endHM[0] += 12;
  }

  // --- duración «durante 2 horas», «hora y media»
  let duration = null;
  const du = take(/\b(?:durante |por )?(\d+|una|dos|tres|cuatro) horas?( y media)?\b|\bhora y media\b/.exec(t));
  if (du) duration = du[0].includes("hora y media") && !du[1] ? 90 : (NUMBERS[du[1]] || parseInt(du[1], 10)) * 60 + (du[2] ? 30 : 0);

  // --- título: lo que queda, sin muletillas
  cut.sort((a, b) => a[0] - b[0]);
  let rest = "", pos = 0;
  for (const [a, b] of cut) { if (a > pos) rest += original.slice(pos, a); pos = Math.max(pos, b); }
  rest += original.slice(pos);
  rest = rest.replace(/\s+/g, " ").trim();
  const fillers = /^(oye |vale )?(tengo que ir al |tengo que ir a la |tengo que |tengo una? |tengo el |tengo la |tengo |apuntame |apunta |apuntar |ponme |pon |anade |recuerdame |hay |voy al |voy a la |voy a |me toca )+/i;
  rest = rest.replace(new RegExp(fillers.source, "i"), (s) => "");
  let fr = fold(rest);
  const lead = /^(?:(el|la|los|las|un|una|al|a|de|del|en)\b ?)+/.exec(fr);
  if (lead) { rest = rest.slice(lead[0].length); fr = fold(rest); }
  const tail = /(?: ?\b(el|la|los|las|a|de|del|en|y|para|que|dia|sobre|hasta|las?))+[ ,.]*$/.exec(fr);
  if (tail) rest = rest.slice(0, tail.index);
  rest = rest.replace(/[ ,.;:]+$/g, "").replace(/^[ ,.;:]+/, "").trim();
  if (!rest) rest = "Evento";
  rest = rest.charAt(0).toUpperCase() + rest.slice(1);

  // --- categoría
  const hay = " " + plain(original) + " ";
  let cat = DEFAULT;
  for (const c of CATEGORIES) if (c[1].some((k) => hay.includes(k))) { cat = c; break; }
  const [emoji, , catMinutes] = cat;
  const allDay = hour === null;
  const start = new Date(day.getFullYear(), day.getMonth(), day.getDate(), allDay ? 0 : hour, allDay ? 0 : minute);
  let end;
  if (allDay) end = addDays(start, 1);
  else if (endHM) end = new Date(day.getFullYear(), day.getMonth(), day.getDate(), endHM[0], endHM[1]);
  else end = new Date(start.getTime() + (duration || catMinutes || 60) * 60000);
  if (end <= start) end = new Date(start.getTime() + 60 * 60000);
  const title = /^\p{Extended_Pictographic}/u.test(rest) ? rest : `${emoji} ${rest}`;
  return { title, emoji, start, end, allDay, warnings };
}

function describe(p) {
  const days = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];
  const months = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre"];
  const s = p.start;
  const when = `${days[s.getDay()]} ${s.getDate()} de ${months[s.getMonth()]}`;
  const hm = (d) => `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  return p.allDay ? `${p.title} · ${when} (todo el día)` : `${p.title} · ${when}, ${hm(s)}–${hm(p.end)}`;
}

async function main() {
  let text = args.shortcutParameter || (args.plainTexts && args.plainTexts[0]) || "";
  const siri = config.runsWithSiri || config.runsInActionExtension || !!args.shortcutParameter;
  if (!text && !siri) {
    const a = new Alert();
    a.title = "Apuntar";
    a.message = "Ej.: «tengo médico el martes 24 a las 12:30»";
    a.addTextField("¿Qué apunto?");
    a.addAction("Apuntar");
    a.addCancelAction("Cancelar");
    if ((await a.present()) === -1) return Script.complete();
    text = a.textFieldValue(0);
  }
  const p = parseEvent(text, new Date());
  if (p.error) {
    if (siri) Script.setShortcutOutput(`No lo he apuntado: ${p.error}`);
    else { const e = new Alert(); e.title = "No lo he apuntado"; e.message = p.error; e.addAction("OK"); await e.present(); }
    return Script.complete();
  }
  const ev = new CalendarEvent();
  ev.title = p.title;
  ev.startDate = p.start;
  ev.endDate = p.end;
  ev.isAllDay = p.allDay;
  ev.notes = `Apuntado: «${text}»`;
  if (CALENDAR_NAME) ev.calendar = await Calendar.forEventsByTitle(CALENDAR_NAME);
  const summary = describe(p) + (p.warnings.length ? `\n${p.warnings.join(" ")}` : "");
  if (siri) {
    ev.save();
    Script.setShortcutOutput(`Apuntado: ${summary}`);
    return Script.complete();
  }
  const c = new Alert();
  c.title = "¿Lo apunto así?";
  c.message = summary;
  c.addAction("Guardar");
  c.addAction("Editar antes (avisos, lugar…)");
  c.addCancelAction("Cancelar");
  const r = await c.present();
  if (r === 0) ev.save();
  else if (r === 1) await ev.presentEdit();
  Script.complete();
}

if (typeof module !== "undefined" && module.exports) module.exports = { parseEvent, describe };
else main();
