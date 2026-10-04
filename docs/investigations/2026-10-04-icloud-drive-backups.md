# Investigación: iCloud Drive como destino de backups (2026-10-04)

**Conclusión: viable tras corregir el perfil de Windows (2026-10-05).** Inicialmente se
descartó porque iCloud para Windows no permitía **borrar** ficheros ya sincronizados (sin
borrado no hay retención). La causa era el perfil de Windows registrado sobre un enlace
simbólico; tras registrarlo sobre la carpeta real, la batería completa pasa (ver
[Resolución](#resolución-2026-10-05)). `age` (cifrado) funciona y es independiente del destino.

## Qué necesita el sistema y qué pasó

Prueba real desde WSL sobre `iCloudDrive/PersonalOS-backups-test-2`, repetida tras desactivar
y reactivar iCloud Drive en iCloud para Windows 15.10.39:

| Operación | Resultado |
|---|---|
| Crear backup de la base real | ✅ |
| Cifrar con age (pyrage) | ✅ |
| Escribir en iCloud Drive (tmp + rename) | ✅ |
| Releer y comparar SHA-256 | ✅ idéntico; Windows lo marca sincronizado |
| Descifrar con pyrage **y con el binario oficial `age` 1.3.2** | ✅ interoperable |
| Verificar manifiesto (sha256, integrity_check, filas) | ✅ |
| Borrar desde WSL (`unlink`) | ❌ `EINVAL` |
| Borrar desde PowerShell (`Remove-Item`) y .NET (`File.Delete`) | ❌ `ERROR_CLOUD_FILE_NOT_UNDER_SYNC_ROOT` |
| Borrar con la API de shell del Explorador (a la papelera) | ❌ mismo error |
| Borrar desde el Explorador de Windows (prueba manual del usuario) | ❌ mismo error |
| Borrar por la ruta real `C:\Users\PULSE\iCloudDrive\…` | ❌ mismo error |
| Retención sobre backups ya sincronizados | ❌ (depende del borrado) |

Matiz observado: un fichero **recién creado** sí se puede borrar durante unos segundos; en
cuanto iCloud lo adopta (atributo `ReparsePoint`) deja de poder borrarse. La retención real
borra backups de días atrás, así que fallaría siempre.

Mensaje del sistema: *"La operación solo es compatible con archivos de una raíz para la
sincronización en la nube"* (`ERROR_CLOUD_FILE_NOT_UNDER_SYNC_ROOT`).

## Causa raíz (muy probable)

- El perfil de Windows `C:\Users\sergio` es un **enlace simbólico** a `C:\Users\PULSE` (carpeta
  real). El perfil está registrado como `C:\Users\sergio`.
- iCloud registra su raíz de sincronización (Cloud Files API, `SyncRootManager`) en
  `C:\Users\sergio\iCloudDrive` — la ruta del enlace.
- El sistema de archivos en la nube de Windows trabaja con la ruta final resuelta
  (`C:\Users\PULSE\…`), que no coincide con la raíz registrada. Crear/modificar funciona, pero
  las operaciones que requieren al proveedor de sincronización (borrar) se rechazan.
- Desactivar/reactivar iCloud Drive **no cambia** el registro (sigue en la ruta del enlace).

Es una peculiaridad de **este PC** (perfil enlazado), no necesariamente de iCloud para Windows
en general.

## Implicaciones

- **Bridge del iPhone**: no afectado. Ya estaba diseñado para que el PC nunca borre en el buzón
  (la limpieza la hace el iPhone). Tras la reactivación, el buzón y el bridge siguen operativos
  (`pos doctor`: 13 ok).
- **OneDrive** usa la misma Cloud Files API y vive bajo el mismo perfil enlazado
  (`C:\Users\sergio\OneDrive`): **es probable que tenga el mismo problema**. Debe someterse a la
  misma prueba real antes de adoptarlo.
- Destinos fuera de la Cloud Files API (disco/USB externo, carpeta local no sincronizada,
  `rclone` contra un almacenamiento de objetos, o un backup "delegado" al iPhone vía bridge)
  no tienen esta limitación, cada uno con sus propios compromisos.

## Resolución (2026-10-05)

- `HKLM\...\ProfileList\<SID>\ProfileImagePath` cambiado de `C:\Users\sergio` a
  `C:\Users\PULSE` (script con comprobaciones previas y exportación `.reg` para deshacer,
  ejecutado por el usuario como administrador; fuera del repo en `C:\pos-perfil\`). El enlace
  `sergio → PULSE` se mantiene por compatibilidad.
- Tras reiniciar y reactivar iCloud Drive, la raíz registrada es `C:\Users\PULSE\iCloudDrive`.
- **Las rutas deben usar `PULSE`**, no el enlace: `mailbox_dir` ya apunta a
  `/mnt/c/Users/PULSE/iCloudDrive/...` y el futuro `[backup].dir` también debe hacerlo.

Batería repetida desde WSL en `iCloudDrive/PersonalOS-backups-test-3` con los módulos reales
(`create_backup`, `verify_backup`, `prune`) + `pyrage`: **10/10**.

| Operación | Resultado |
|---|---|
| Crear backup de la base real | ✅ |
| Cifrar con age y escribir en iCloud (tmp + rename) | ✅ |
| iCloud adopta los ficheros (`ReparsePoint`) | ✅ ~13 s |
| Releer y comparar SHA-256 | ✅ |
| Descifrar con pyrage y con el binario `age` | ✅ |
| Verificar manifiesto | ✅ |
| `prune` sobre backups ya sincronizados (3 borrados, 0 avisos) | ✅ |
| Borrar la carpeta completa desde WSL | ✅ |

Además se borraron sin problema las carpetas de la prueba original (`-test`, `-test-2`), cuyos
ficheros llevaban un día adoptados y antes no se podían borrar de ninguna forma.

Pendiente: Fotos de iCloud sigue registrada en la ruta del enlace; si falla al borrar,
desactivarla y reactivarla en la app de iCloud.
