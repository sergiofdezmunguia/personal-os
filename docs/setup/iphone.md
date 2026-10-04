# Puesta en marcha: iPhone + Windows

Pasos manuales, una sola vez. Todo lo demás lo hace `pos`.

## 1. Windows — iCloud para Windows ✅

1. Instala **iCloud** (Microsoft Store), inicia sesión, activa **iCloud Drive**.
2. En el Explorador: `iCloud Drive` › carpeta **Scriptable** › clic derecho ›
   **Mantener siempre en este dispositivo** (sin esto, lo que escribe el iPhone no baja).
   Desde WSL se ve como `/mnt/c/Users/<usuario>/iCloudDrive/iCloud~dk~simonbs~Scriptable`.

> Limitación conocida: iCloud para Windows **no permite borrar** ficheros de iCloud Drive.
> El Personal OS nunca borra en el buzón; la limpieza la hace el bridge en el iPhone.

## 2. iPhone — Scriptable y lista ✅

1. Instala **Scriptable** y ábrelo una vez (Ajustes › iCloud › iCloud Drive › Scriptable activado).
2. En **Recordatorios**, crea una lista de iCloud llamada exactamente `Personal OS`.

## 3. Configuración local ✅

`~/.config/personal-os/config.toml` (fuera del repo):

```toml
timezone = "Atlantic/Canary"          # debe coincidir con la del iPhone
[apple]
apple_id = "tu-apple-id@…"
reminders_list = "Personal OS"
calendar_name = "Personal OS"
mailbox_dir = "/mnt/c/Users/PULSE/iCloudDrive/iCloud~dk~simonbs~Scriptable/personal-os"
```

Instalar/actualizar el bridge: `uv run pos bridge install` → script **Personal OS Sync**.

## 4. iPhone — automatizaciones (Atajos)

Atajos › **Automatización** › **+** › *Crear automatización personal*:

1. **App** › elige **Recordatorios** › marca **Se cierra** (desmarca *Se abre*) ›
   **Ejecutar inmediatamente** › Siguiente › *Nueva automatización en blanco* ›
   Añadir acción › **Scriptable › Run Script** › Script: `Personal OS Sync` ›
   toca la flecha de la acción y desactiva **Run In App** › OK.
2. **Hora del día** (repítelo para 08:00, 14:00, 20:00, por ejemplo) ›
   **Diariamente** › **Ejecutar inmediatamente** › misma acción.

iOS muestra una notificación breve cada vez que se ejecuta una automatización (restricción de
Apple, no se puede desactivar).

Opcional: crea un **atajo** normal con la misma acción y añádelo a la pantalla de inicio
como botón "Sincronizar".

## 5. Calendario (CalDAV)

1. En la app **Calendario** › Calendarios › Añadir calendario (cuenta **iCloud**) ›
   nombre exacto `Personal OS`.
2. https://account.apple.com › Inicio de sesión y seguridad › **Contraseñas específicas de
   app** › genera una llamada `personal-os`.
3. En **tu terminal de WSL** (no en el chat de Claude):

   ```bash
   cd ~/projects/personal-os && uv run pos secrets set icloud_app_password
   ```

   Se pide sin eco y se guarda en `~/.config/personal-os/secrets.toml` (permisos 0600).
   `uv run pos secrets status` confirma sin mostrar el valor.

## Comprobación

```bash
POS_LIVE=1 uv run pytest -m live -v   # ciclo real crear/editar/borrar en iCloud Calendar
uv run pos sync status
```
