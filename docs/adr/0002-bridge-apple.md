# ADR 0002 — Integración con Apple: CalDAV + buzón iCloud Drive con Scriptable

**Estado:** aceptado y validado con iPhone real · 2026-10-04

## Contexto
- Recordatorios no tiene API accesible desde Linux desde iOS 13 (dejó CalDAV).
- Atajos no permite fijar repetición ni leer el identificador de un recordatorio, y su
  lógica no es versionable.
- No existe forma oficial de ejecutar un atajo en remoto (Pushcut requiere un iPhone/iPad
  dedicado siempre encendido).
- iCloud Calendar sí expone CalDAV con contraseña específica de app.

## Decisión
- **Calendario:** CalDAV directo (cliente propio sobre httpx con ETags).
- **Recordatorios:** buzón de ficheros JSON en iCloud Drive (protocolo `pos-reminders/1`) +
  bridge en **Scriptable** (EventKit: identifiers, completionDate), disparado por
  automatizaciones de Atajos (al cerrar Recordatorios + horas fijas).
- Sin Mac. Sin API privada de iCloud (pyicloud): frágil.

## Hallazgos de la validación real
- Funciona ida y vuelta: creación, aviso nativo a la hora (aunque Scriptable no exponga
  alarmas: un recordatorio con fecha+hora notifica), compleción detectada, idempotencia.
- **iCloud para Windows no permite borrar ficheros** (sí crear y sobrescribir). ⇒ El PC
  nunca borra en el buzón; la limpieza la hace el bridge (ver protocolo).
- El iPhone está en `Atlantic/Canary`: la zona es configuración, y el snapshot la reporta
  para avisar de discrepancias.
- La latencia depende de cuándo corre el bridge (automatizaciones) + iCloud (segundos-minutos).

## Riesgos y mitigación
- Scriptable lo mantiene una sola persona (última versión sep-2024). El protocolo es
  independiente del ejecutor: podría sustituirse por un atajo puro, un Mac o una app propia
  sin tocar Core ni adaptador.
