# CLAUDE.md — odoo_backup_tool

Instrucciones específicas para trabajar en este proyecto con Claude Code.

## Descripción

Herramienta de escritorio (Python 3.10 + tkinter) para backup automatizado de instancias Odoo.
Compilada a `.exe` con PyInstaller (`--onefile --windowed`). Branch activo: `develop`.

## Tecnología

| Componente | Detalle |
|---|---|
| GUI | `tkinter` / `ttk`, 11 tabs, `queue.Queue` para actualizaciones thread-safe |
| SSH / SFTP | `paramiko` |
| Google Drive | `google-api-python-client`, Service Account JSON, upload resumable |
| Transferencia S2S | `rsync --partial` (preferido) con fallback a `scp`; `sshpass -e` |
| Empaquetado | PyInstaller `--onefile --windowed`, spec en `OdooBackupTool.spec` |
| Notificaciones | `plyer` (toast Windows), fallback silencioso |
| Scheduler | Daemon thread 60 s, `schedules.json` en `~/.odoo_backup_tool/` |

## Estructura del proyecto

```
odoo_backup_tool/
├── core/
│   ├── gdrive.py          # DriveUploader: streaming, MD5, checkpoint, retry
│   ├── scheduler.py       # ScheduleManager + BackupScheduler (daemon)
│   ├── transfer.py        # rsync/scp servidor-servidor + SFTP download
│   ├── bundle_manager.py  # .tar de artefactos en servidor remoto
│   ├── db_manager.py      # pg_dump via SSH
│   ├── filestore_manager.py
│   ├── ssh_client.py
│   ├── profiles.py        # CRUD perfiles en servers.json
│   ├── notifier.py        # Toast Windows via plyer
│   ├── temp_registry.py   # Manifiesto de temporales /tmp remotos + barrido de huerfanos
│   └── restore_manager.py
├── gui/
│   └── app.py             # BackupApp: 5000+ líneas, todo el wizard
├── OdooBackupTool.spec    # Config PyInstaller
├── build.bat              # Script de compilación
├── requirements.txt
└── .claude/
    └── CLAUDE.md          # Este archivo
```

## Cómo ejecutar en desarrollo

```powershell
# Desde C:\REALNET\tools\odoo_backup_tool
& "C:\Users\Marketing Realnet\AppData\Local\Programs\Python\Python310\python.exe" -m gui.app
```

## Cómo compilar

```powershell
# Genera dist\OdooBackupTool.exe
.\build.bat
```

## Cómo verificar sintaxis

```powershell
$py = "C:\Users\Marketing Realnet\AppData\Local\Programs\Python\Python310\python.exe"
& $py -m py_compile core/gdrive.py core/scheduler.py core/transfer.py gui/app.py
```

## Datos persistentes en tiempo de ejecución

Todos los archivos de usuario se guardan en `~/.odoo_backup_tool/`:

| Archivo | Contenido |
|---|---|
| `servers.json` | Perfiles de servidor (host, port, user, pass, Drive creds) |
| `schedules.json` | Reglas de automatización |
| `upload_checkpoints/*.json` | Checkpoints de uploads Drive interrumpidos |
| `remote_tmp_manifest.json` | Manifiesto de archivos /tmp remotos pendientes de limpieza (ver `core/temp_registry.py`) |

## Convenciones de código

- Comunicación GUI↔hilos exclusivamente via `queue.Queue` + `root.after(100, _poll_queue)`.
- Nunca llamar widgets de tkinter desde hilos secundarios.
- Todo log de scheduler debe pasar por la función `log()` local en `_run_rule()` — ya fuerza el prefijo `[label]` para identificar el cliente en logs concurrentes.
- Al eliminar código, comentarlo en lugar de borrarlo.
- Textos en pantalla: español latinoamericano.

## Flujo de backup (referencia rápida)

```
Phase A (en servidor origen):
  pg_dump → /tmp/odoo_{db}_{ts}.dump
  tar filestore (sin compresion) → /tmp/filestore_{db}_{ts}.tar
  tar bundle → /tmp/{db}_{ts}_obt.tar  (engloba los anteriores)

Phase B (transferencia):
  gdrive  → upload_stream() SFTP→Drive  (streaming, sin copia local)
  local   → sftp.get() al directorio configurado
  remote  → rsync --partial (o scp fallback) servidor a servidor
```

## Notas importantes

- El scheduler es un daemon thread: solo corre mientras el exe esté abierto.
- Los checkpoints de Drive se guardan en `~/.odoo_backup_tool/upload_checkpoints/`; permiten reanudar un upload interrumpido en la próxima ejecución.
- `sshpass -e` (variable de entorno `SSHPASS`) se usa en lugar de `-p` para evitar exponer la contraseña en `ps aux`.
- La carpeta Drive se resuelve una sola vez por instancia de `DriveUploader` (cached en `_resolved_folder_id`).
- `BackupScheduler._drive_upload_semaphore` (Semaphore(2)) acota los uploads a Drive entre reglas simultáneas — Fase A (dump+filestore) de cada regla sigue corriendo en paralelo, solo Fase B (upload) se acota a 2 streams concurrentes. Evita el throttling visto el 2026-07-15.
- `gdrive.py` emite `[ALERTA]` en el log si una subida cae por debajo de 1 MB/s durante 5 chunks seguidos (`_SpeedWatchdog`) — informativo, no aborta la subida.
- El filestore se empaqueta con `tar -cf` (sin compresión) en vez de `zip -1`; los backups antiguos en `.zip` se siguen restaurando correctamente (detección por extensión en `restore_manager.restore_filestore`).
- Para destino Google Drive, el scheduler ya NO envuelve dump+filestore+inventory en el bundle `.tar` — los sube en paralelo directamente (retención por 3 prefijos: `odoo_{db}_`, `filestore_{db}_`, `{db}_`). El bundle se mantiene solo para destino local/remoto (`bundled` flag en `_run_rule`).
- `BackupScheduler.has_active_uploads()` / `active_upload_labels()` exponen si hay una subida a Drive en curso; la GUI (`_action_start_backup`) advierte antes de iniciar una descarga local manual mientras el scheduler está subiendo (compiten por el ancho de banda del servidor origen).
- Concurrencia automático+manual: el botón "Ejecutar ahora" (Tab automatización) ya NO lanza `_run_rule` directamente — usa `BackupScheduler.run_rule_now(rule)`, que respeta el mismo `_active_jobs`/`_jobs_lock` que `_tick()`, evitando que un tick automático y un clic manual corran la misma regla dos veces en paralelo (antes podían colisionar sobre los mismos paths `/tmp` con timestamp de minuto).
- **Watchdog de inactividad en Drive** (`gdrive.py._get_service`): el servicio se construye con `AuthorizedHttp(creds, http=httplib2.Http(timeout=120))` en vez del http por defecto de `build()` — un socket que deja de recibir/enviar bytes 120s revienta con `socket.timeout`/`ConnectionError`/`ssl.SSLError` en vez de colgarse indefinidamente. `_with_retry` ahora captura esos errores de red (`_NETWORK_RETRYABLE`) con el mismo backoff exponencial que usa para HTTP 429/5xx, y `next_chunk()` retoma la subida resumible desde el offset ya confirmado. Corrige el caso del log 2026-07-16: `backup_novalens` colgado en 86% durante ~35 min antes de fallar con `[SSL: WRONG_VERSION_NUMBER]` sin ningún reintento.
- **Escalonado de ráfagas** (`scheduler.py._tick`/`_run_rule_after_delay`): cuando varias reglas quedan "vencidas" y se disparan en el mismo tick de 60s (típico al reabrir la herramienta con horarios atrasados), cada regla subsiguiente en la ráfaga arranca `BackupScheduler._burst_stagger_secs` (default 120s) más tarde que la anterior, para no saturar el semáforo de Drive todas a la vez. Solo aplica al disparo automático de `_tick()` — `run_rule_now()` (botón "Ejecutar ahora") sigue arrancando de inmediato.
- **Cierre robusto de la app / conexiones SSH colgadas**: `ssh_client.py.connect()` ahora activa `transport.set_keepalive(30)` + `sock.settimeout(180)` tras conectar, igual que el timeout de Drive — una conexión SSH/SFTP que deja de responder falla en vez de colgarse indefinidamente. `SSHClient.close()` corre el `close()` real de paramiko en un hilo daemon con espera acotada a 5s (nunca bloquea al llamador), y `SshTerminalPanel.disconnect()` sigue el mismo patrón. `gui/app.py._on_close()` cierra las 5 conexiones (2 terminales PTY + 3 SSHClient) en paralelo en vez de secuencial. Antes, si una transferencia quedaba colgada, cerrar la ventana bloqueaba el hilo principal de Tk indefinidamente — el proceso quedaba "No responde" en el Administrador de tareas y seguía reteniendo el `.exe` bloqueado para el siguiente build.
- `build.bat` verifica el `%ERRORLEVEL%` real de PyInstaller (no solo si el `.exe` existe) y cierra automáticamente instancias de `OdooBackupTool.exe` en ejecución antes de compilar — antes, un build fallido por `PermissionError` (exe bloqueado por una instancia corriendo/colgada) dejaba el `.exe` viejo intacto pero igual imprimía "LISTO", dando una falsa sensación de que el build tenía los cambios nuevos.
- **`_on_close()` distingue trabajo activo de trabajo congelado** antes de pedir confirmación: `self._last_activity_ts` (gui/app.py) se refresca en cada evento drenado por `_poll_queue()` (log/progress/sched_log, manual y programado por igual, misma cola). Si hay un backup manual (`_btn_run` disabled) o una regla programada activa (`BackupScheduler.has_active_jobs()`) Y la última actividad fue hace menos de `_FROZEN_ACTIVITY_THRESHOLD_SECS` (90s) → pide confirmación antes de cerrar. Si está "corriendo" pero sin ninguna actividad en más de 90s (congelado/colgado) → cierra directo sin preguntar, porque no hay nada real que interrumpir.
- **Passphrase de llave SSH del servidor (Tab 7 Addons)**: cuando el modo de llave es "Llave del servidor", `AddonsManager.server_key_needs_passphrase()` pregunta al servidor (via `ssh-keygen -y`, sin transferir la llave) si esta protegida; si lo esta, `_action_sync_addons` pide la passphrase por dialogo (cacheada en `self._server_key_passphrases` por `host:ruta`, igual que las llaves locales) y `AddonsManager.unlock_server_key()` la desbloquea en un `ssh-agent` **en el propio servidor** via un script SSH_ASKPASS de un solo uso — la llave nunca sale del servidor. El `env_prefix` resultante (`SSH_AUTH_SOCK=...`) se antepone a los comandos git en `sync()`/`_sync_submodules()`. Antes, una llave del servidor con passphrase fallaba en silencio porque `BatchMode=yes` no puede pedir la passphrase de forma no interactiva.
- **Registro de temporales remotos y barrido de huérfanos** (`core/temp_registry.py`, `RemoteTempRegistry` + `sweep_orphaned_files`): cada dump/filestore/bundle/inventory que la herramienta crea en `/tmp` de un servidor remoto se registra en `~/.odoo_backup_tool/remote_tmp_manifest.json` (host/puerto/usuario/password/ruta/kind/label/fecha) ANTES de que la conexión que lo creó pueda cerrarse, y se des-registra solo cuando ese archivo se elimina con éxito del servidor. Como el manifiesto vive en disco (no en memoria), sobrevive a un crash, un cierre forzado, o un job que falla sin llegar a su propio código de limpieza. Instancia única compartida entre `BackupApp` (`self._temp_registry`) y `BackupScheduler` (pasado como `temp_registry=` en el constructor) — dos instancias independientes se pisarían el archivo entre sí. Si una regla/backup manual tiene `cleanup_server`/`cleanup=False` (el usuario quiere conservar el archivo a propósito), se des-registra sin borrar — el barrido nunca debe eliminar algo dejado intencionalmente. El barrido corre en un hilo de fondo al iniciar la app (`BackupApp._sweep_orphaned_temp_files`), agrupado por servidor, con un margen de `_MIN_AGE_FOR_SWEEP_SECS` (10 min) para no competir con un job que todavía podría estar corriendo legítimamente. Un servidor inalcanzable durante el barrido deja sus entradas intactas para reintentar en el próximo arranque.
- **Fix `burst_index` inflado por jobs zombie** (`scheduler.py._tick`): las reglas todavía activas (`_active_jobs` vivo) ya NO cuentan para el índice de ráfaga — antes, un job que seguía corriendo horas después de iniciar (p.ej. una subida grande sin terminar, con `last_run_ts` sin actualizar) se seguía viendo "vencido" en cada tick posterior e inflaba el `burst_index` de reglas totalmente ajenas que en realidad llegaban solas a su hora, dándoles un retraso injustificado de `_burst_stagger_secs`. Confirmado en el log 2026-07-17: equiredes/novalens/variedades cada una recibió un "en cola... 120s" sin haber coincidido realmente con otra regla nueva, solo porque bancasa seguía "viva" desde las 9am.
- **Watchdog de progreso real (`_StallWatchdog` en `gdrive.py`)**: el timeout de socket de 120s solo detecta una conexión completamente silenciosa — no detecta una que sigue "respirando" (suficientes bytes para resetear ese timeout) sin avanzar realmente. Confirmado dos veces el 2026-07-17: `novalens` colgado 44 min → `HttpError 200 "OK"`, y `variedades` colgado 30 min → `Redirected but the response is missing a Location header`, ambos sin ninguna línea de progreso durante el colgado. `_StallWatchdog` corre en un hilo aparte durante `upload_stream()`/`upload_file()`; si pasan `_MAX_STALL_SECS` (240s) sin que se complete un chunk (`touch()`), cierra a la fuerza el socket HTTP subyacente (via `svc._http.http.connections`) para que la llamada bloqueada falle de inmediato con un error de conexión — que `_with_retry` ya sabe reintentar reanudando desde el offset confirmado. `_NETWORK_RETRYABLE` ahora incluye `OSError` genérico porque la desconexión forzada no siempre produce un tipo de excepción más específico (en Windows típicamente `WinError 10038`).
- **Fix `tar: file changed as we read it` no debe abortar el backup** (`filestore_manager.py.compress_filestore`): GNU tar sale con código 1 (advertencia, no fatal) cuando un archivo cambia mientras se lee — normal en un filestore de Odoo activo durante horario laboral. El comando ahora usa `tar -cf ...; test $? -le 1` para que solo un código de salida ≥2 (error real) aborte el backup; antes cualquier código distinto de 0 hacía fallar el job completo, perdiendo el backup entero por una advertencia inofensiva (confirmado en el log 2026-07-17, `backup_equiredes`).
- `ProfileManager` (`core/profiles.py`) y `ScheduleManager` (`core/scheduler.py`) ahora escriben `servers.json`/`schedules.json` de forma atómica (archivo temporal + `os.replace`) y `ProfileManager` tiene lock — antes una escritura interrumpida a mitad de camino (GUI escribiendo mientras el scheduler lee) podía dejar el JSON truncado y `_load()` lo reseteaba en silencio a una lista vacía, perdiendo todos los perfiles/reglas guardados.
- **Indicador visual al cerrar la app** (`gui/app.py._on_close`): se abre una ventanita ("Validando tareas pendientes..." → "Validando procesos antes de cerrar...") en el primer instante del clic en cerrar, forzada a pintarse (`update()`) antes de correr cualquier validación bloqueante. Antes, el usuario veía la ventana principal sin reaccionar durante los ~6s de cierre de conexiones SSH en paralelo, fácil de confundir con que el clic no registró o que la app está congelada.
- **`HttpError 200 "OK"` y `'str' object has no attribute 'get'` no deben abortar una subida a Drive que en realidad sí se completó** (`gdrive.py`): confirmado en el log 2026-07-21 — `backup_equiredes` y `backup_novalens` fallaron con `HttpError 200 "OK"` (colgados ~29-90 min antes), y `backup_mega` con `'str' object has no attribute 'get'`. Causa raíz: es un quirk conocido de `googleapiclient` — cuando `_StallWatchdog` fuerza una reconexión a mitad de un chunk, la sesión resumible a veces retoma con una respuesta de finalización mal formada (status 200 pero cuerpo no parseable como JSON, o el cuerpo crudo como `str` en vez del dict esperado) aunque el archivo ya se recibió correctamente en Drive. Dos fixes: (1) `_with_retry` ahora trata status 200/201 como reintentable (`_RETRYABLE_MALFORMED_STATUS`) en vez de abortar de inmediato — reintentar `next_chunk()` reconsulta el estado real; (2) si tras el loop `response` no es un `dict` utilizable, `_fetch_uploaded_file_meta()` busca el archivo por nombre+carpeta directamente en Drive en vez de crashear en `response.get(...)` — si tampoco lo encuentra ahí, lanza `RuntimeError` (nunca reporta éxito falso). También se agregó logging explícito: `_StallWatchdog` ahora loguea cada reconexión forzada (antes era silenciosa, lo que producía huecos de 29-90 min sin ninguna línea en el log) y `_with_retry` loguea el intento final antes de propagar la excepción, en vez de fallar en silencio.
- **`_StallWatchdog` estaba matando subidas sanas-pero-lentas, no solo las colgadas** (`gdrive.py`): confirmado 2026-07-21 — el mismo día que se relajaron los fixes anteriores, las 5 reglas programadas fallaron 5/5 (antes de existir el watchdog, 0 fallaban por esta causa). El watchdog solo mide "chunk completo" (`touch()`), no bytes reales en tránsito, así que `_MAX_STALL_SECS / _CHUNK_SIZE` es un piso de throughput implícito no documentado como tal: con 240s/16MB, exigía ~68 KB/s sostenidos por stream o mataba la conexión creyéndola muerta, aunque siguiera avanzando más lento. El enlace de salida de la oficina estuvo por debajo de eso gran parte del día 21-jul, y el watchdog abortó cada intento de las 5 reglas. Subido `_MAX_STALL_SECS` a 600s y bajado `_CHUNK_SIZE` a 4MB (piso efectivo ahora ~7 KB/s) — sigue detectando los cuelgues reales confirmados el 17-jul (30-44 min), con mucho mas margen para tramos simplemente lentos.
