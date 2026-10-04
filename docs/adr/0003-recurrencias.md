# ADR 0003 — El Personal OS es dueño de las recurrencias de tareas

**Estado:** aceptado · 2026-10-04

## Decisión
- `Task` (definición/serie) + `Occurrence` (instancia). Como mucho una ocurrencia abierta.
- A Recordatorios solo va la ocurrencia abierta, como recordatorio **sin repetición**.
- Al completarse (en el iPhone o en la CLI) el Core genera la siguiente.
- Ancla `schedule` (por defecto): la serie sigue el calendario; completar tarde salta a la
  primera fecha de la regla >= hoy. Ancla `completion`: se cuenta desde la compleción.
- Reprogramar una ocurrencia no desplaza la serie (`scheduled_date` vs `due_date`).
- Eventos de calendario: RRULE nativa (no se "completan").

## Por qué
Historial real por ocurrencia, independencia de plataforma, y Atajos/EventKit vía Scriptable
no exponen de forma fiable la semántica de repetición de Recordatorios.

## Coste
La siguiente ocurrencia aparece en el iPhone tras la siguiente sincronización.
