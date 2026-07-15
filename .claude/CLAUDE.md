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
  zip filestore → /tmp/filestore_{db}_{ts}.zip
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
