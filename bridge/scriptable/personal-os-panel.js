// Variables used by Scriptable.
// These must be at the top of the file. Do not edit.
// icon-color: deep-blue; icon-glyph: chart-pie;
// Personal OS Panel: abre el panel que genera el PC (<buzón>/panel/index.html)
// en un visor web con JavaScript. Solo lectura; no envía nada a ningún sitio.
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
  await wv.present(true);
}
Script.complete();
