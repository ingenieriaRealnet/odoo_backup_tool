# Diseño: subida directa servidor origen → Google Drive

Estado: **en implementación (fase 2 iniciada 2026-07-21 18:05-18:30)**.
`core/remote_upload_manager.py` existe y tiene tests con mocks pasando,
pero **todavía no está conectado** al scheduler ni a la GUI — las reglas
programadas siguen usando el modo relé (`gdrive.py`) sin excepción hasta
que se complete la integración (ver sección 12).

## 1. Motivación

El 2026-07-21, las 5 reglas programadas del día fallaron 5/5. Diagnóstico (ver
`.claude/CLAUDE.md`, sección "Notas importantes"): la arquitectura actual
transporta cada backup en dos saltos —

```
servidor origen (SFTP) → esta máquina (gdrive.py, streaming) → Google Drive
```

— y ambos tramos comparten la salida a internet de esta máquina (asimétrica,
propia de una conexión de oficina). El semáforo `_drive_upload_semaphore`
(2 concurrentes) reparte ese único caño entre dump+filestore de una misma
regla, y potencialmente entre reglas de distintos clientes si se solapan.
Un watchdog de progreso (`_StallWatchdog`, agregado el 17-jul) asumía
implícitamente un piso de throughput (`_CHUNK_SIZE / _MAX_STALL_SECS`) que,
bajo degradación real del enlace de esta oficina, mataba subidas
sanas-pero-lentas antes de que pudieran terminar — agravando el problema que
se intentaba resolver.

**Conclusión validada con el usuario:** el cuello de botella no es Drive ni
el servidor origen — es que esta máquina actúa de relé obligatorio para
todo el tráfico. Cada servidor origen (típicamente un VPS/cloud con enlace
simétrico bueno) tiene su propia salida a internet, independiente de la de
esta oficina.

## 2. Arquitectura objetivo

```
servidor origen (rclone) → Google Drive   [subida real, sin pasar por esta máquina]
        ↑
        │ SSH (orquestación: credencial efímera, disparo, monitoreo, limpieza)
        │
esta máquina (BackupApp / BackupScheduler)
```

Esta máquina deja de transportar bytes de backup y pasa a **orquestar**: la
misma relación que ya tiene con `pg_dump`/`tar` vía `execute_long()` +
`heartbeat_callback`, aplicada ahora también a la subida.

## 3. Componentes nuevos

### 3.1 `RemoteUploadManager` (nuevo módulo, `core/remote_upload_manager.py`)

Responsable de, por servidor:

1. **Verificar viabilidad** antes de comprometerse al modo directo:
   - `which rclone` — si falta, instalar (ver 3.2).
   - `curl -sS --max-time 10 -o /dev/null -w '%{http_code}' https://www.googleapis.com` — confirma salida HTTPS a Google. Si falla, **no** intentar modo directo; caer automáticamente al modo relé actual (`upload_stream` en `gdrive.py`, sin cambios).
2. **Desplegar la credencial efímera** (ver sección 4) al servidor.
3. **Disparar la subida** vía `execute_long()`, reutilizando el patrón de heartbeat ya usado para tar/pg_dump:
   ```
   rclone copy --config <config temporal> \
       /tmp/{dump,filestore,inventory}_... \
       remote:{folder_id} \
       --progress --stats=15s --stats-one-line --checksum
   ```
4. **Parsear el progreso** de `--stats-one-line` (formato conocido y estable de rclone) para alimentar el mismo `log_callback`/GUI que hoy muestra %/MB/s/ETA.
5. **Limpiar la credencial** del servidor en un `finally` — nunca condicionado a que el upload haya tenido éxito.
6. **Reportar éxito/fallo** con la misma interfaz que `upload_stream()` hoy, para que `scheduler.py._run_rule()` no tenga que distinguir mucho entre ambos modos.

### 3.2 Auto-instalación de `rclone`

Mismo patrón ya existente en `filestore_manager.py.compress_filestore()`
(`which zip || apt-get install -y zip || yum install -y zip`), adaptado:

```bash
which rclone >/dev/null 2>&1 || curl https://rclone.org/install.sh | sudo bash
```

El instalador oficial de rclone es un binario estático — no depende del
gestor de paquetes de la distro (Debian/Ubuntu/RHEL indistinto). Si no hay
`sudo` disponible, fallback a instalación en `$HOME/bin` (rclone lo soporta
sin privilegios root) — a confirmar viabilidad real por servidor en la fase
piloto.

## 4. Credencial efímera (decisión clave del usuario)

**No se deja el Service Account JSON instalado de forma permanente en el
servidor.** Ciclo de vida por corrida:

1. Antes de disparar `rclone`, la app transfiere el JSON al servidor (mismo
   canal SFTP puntual ya usado para la llave SSH del servidor en Tab 7 —
   nunca queda en logs ni en historial de comandos), a una ruta temporal
   tipo `/tmp/.rclone_sa_{rule_id}_{ts}.json` con permisos `600`.
2. Se registra esa ruta en `RemoteTempRegistry` (`core/temp_registry.py`),
   **igual que dump/filestore/bundle** — así el barrido de huérfanos al
   arrancar la app la limpia sola si la conexión se cae a mitad de camino y
   el `finally` normal no llega a ejecutarse. Sin esto, un crash a mitad de
   subida dejaría una credencial viva en el servidor indefinidamente.
3. Al terminar la corrida (éxito o error), la app borra el archivo del
   servidor en un `finally` y lo des-registra del manifiesto.

Resultado: la ventana de exposición de la credencial en el servidor es
"solo mientras ese backup puntual está corriendo", no "para siempre" — y
sigue siendo consistente con el mecanismo de limpieza que ya existe para
otros temporales remotos.

## 5. Service Account por cliente (no una compartida)

Confirmado con el usuario como el diseño correcto: cada cliente
(bancasa/equiredes/novalens/variedades/mega) tiene su propia Service
Account, restringida a su propia carpeta de Drive. Ventajas:

- Un servidor comprometido durante la ventana de exposición (sección 4)
  solo puede escribir en el Drive de **ese** cliente, no en los de los
  demás.
- Cuotas de la API de Drive (peticiones/100s) ya no se comparten entre
  las 5 reglas — cada Service Account tiene su propio cupo.

Esto ya es coherente con el modelo actual (`gdrive_creds_path` /
`gdrive_folder_id` están definidos **por perfil**, no globalmente — ver
`core/profiles.py`), así que no requiere cambio de esquema de datos, solo
usar el que ya existe por cliente en vez de introducir uno compartido.

## 6. Verificación de integridad

`rclone copy --checksum` calcula y compara hash automáticamente en cada
transferencia, y falla la operación si no coincide — equivalente (o mejor,
por ser una herramienta madura y ampliamente probada) al `_HashingReader` +
comparación manual de MD5 que hoy hace `gdrive.py`. No se reimplementa esa
lógica para el modo directo, se delega a rclone.

## 7. Qué NO cambia

- **Retención** (`cleanup_old_files` en `gdrive.py`): son solo llamadas de
  metadata (listar/borrar), no mueven volumen — sigue corriendo desde esta
  máquina sin impacto, en ambos modos.
- **Destinos `local` y `remote`** (servidor-a-servidor vía rsync/scp): no
  pasan por Drive, fuera de alcance de este cambio.
- **Modo relé actual** (`gdrive.py.upload_stream`, `_StallWatchdog`,
  `_with_retry`, etc.): no se elimina. Queda como *fallback automático*
  para servidores que no califican para modo directo (sin salida a
  Google, sin forma de instalar rclone, etc.) y como red de seguridad
  durante el rollout gradual.

## 8. Flag de rollout por regla

Nuevo campo en cada regla programada: `upload_mode: "relay" | "direct"`,
default `"relay"` (comportamiento actual sin cambios hasta migrar
explícitamente). Se activa servidor por servidor.

`run_rule_now()` (botón "Ejecutar ahora", Tab Automatización) debe
extenderse para respetar este flag y disparar el modo directo bajo demanda
— hoy solo sabe ejecutar el flujo de relé. Esto es lo que permite validar
cada servidor sin esperar a su horario real durante el rollout.

## 9. Plan de rollout (decidido con el usuario)

Uno a la vez, apoyado en los horarios ya escalonados de cada regla
(09:00 bancasa, 10:00 equiredes, 11:00 novalens, 12:00 variedades,
13:00 mega):

1. **Piloto en 1 servidor** (candidato: novalens, es el más pequeño — dump
   ~450MB, filestore ~2.8GB — menor riesgo si algo sale mal).
   - Instalar/validar rclone manualmente primero.
   - Activar `upload_mode: "direct"` en esa regla.
   - Validar con "Ejecutar ahora" (una vez extendido, ver sección 8) sin
     esperar al horario 11:00, varias veces si hace falta.
   - Comparar tiempo total y confiabilidad contra el modo relé.
2. **Confirmar en producción** con la corrida programada real (11:00) al
   menos 2-3 días antes de tocar el siguiente cliente.
3. **Repetir cliente por cliente** (equiredes, bancasa, variedades, mega),
   en orden de menor a mayor volumen/riesgo, cada uno validado con
   "Ejecutar ahora" antes de confiar en su horario real.
4. El modo relé permanece disponible indefinidamente como fallback — no
   hay fecha de "apagado" forzoso mientras existan servidores que no
   califiquen para modo directo (sección 3.1, paso 1).

## 10. Fases de desarrollo y esfuerzo estimado

| Fase | Contenido | Estimado |
|---|---|---|
| 1 | Piloto manual en 1 servidor (instalar rclone, probar `rclone copy` a mano, validar hash/tiempos) | ~3h |
| 2 | `RemoteUploadManager`: verificación de viabilidad, disparo vía `execute_long`, parseo de progreso, fallback automático a relé | ~6-8h |
| 3 | Credencial efímera: transferencia SFTP puntual + registro en `RemoteTempRegistry` + limpieza garantizada | ~3h |
| 4 | Auto-instalación de rclone (reutilizando patrón de `filestore_manager.py`) | ~1-2h |
| 5 | GUI: selector `upload_mode` por regla (Tab Automatización) + extender `run_rule_now()` para soportar modo directo | ~3-4h |
| 6 | Rollout gradual y validación por cliente (sección 9) | tiempo de calendario, no desarrollo |

**Total desarrollo: ~16-20h**, antes del tiempo de validación en
producción por cliente.

## 11.5 Resultado de la fase piloto (novalens, 2026-07-21)

**Validado end-to-end en el servidor real de novalens:**
- Ubuntu 24.04, acceso `root`, `rclone` v1.60.1 ya instalado (no fue
  necesario instalarlo). Servidor ya tenía un `rclone.conf` propio con un
  remoto `[contabo]` ajeno a esta herramienta — confirmado que usar
  `--config <archivo temporal aislado>` no lo toca ni lo lee.
- Credencial efímera (SFTP puntual, `chmod 600`, borrado al final) +
  config de rclone aislado: funcionó exactamente como se diseñó en la
  sección 4.
- Subida real de un backup completo (dump 59MB + filestore 2.83GB):
  **~1.9 min total** (dump 4.8s, filestore 108s a ~28.5 MB/s sostenidos),
  verificado con `rclone check` (0 diferencias). El mismo día, en modo
  relé bajo la misma red degradada, esta subida **nunca se completó**
  (falló tras ~49 min) — confirma directamente el diagnóstico de la
  sección 1.

**Salvaguarda agregada tras dos incidentes en la validación manual:**
durante la prueba, un comando de limpieza con alcance de carpeta
(`rclone delete <remote>: --min-age 0s`) borró temporalmente los backups
reales existentes en la carpeta de Drive de novalens (recuperados de la
papelera sin pérdida, pero nunca debió pasar). Regla obligatoria para
`RemoteUploadManager` (sección 3.1):

- **Ninguna limpieza —ni de `/tmp` del servidor ni de Drive— corre hasta
  confirmar explícitamente el exit code de cada subida individual.** Nunca
  de forma incondicional al final del flujo.
- **Nunca usar un comando de limpieza con alcance de carpeta/remoto
  completo en Drive** (`delete <remote>:`, `purge`). Cualquier borrado en
  Drive debe apuntar al **ID de archivo específico** recién subido, nunca
  a un patrón o al root del remoto.
- `rclone copy <origen> <remoto>:` solo acepta un archivo origen por
  invocación — cada artefacto (dump/filestore/inventory) se sube con su
  propia llamada, nunca en batch de múltiples orígenes.

## 11. Riesgos abiertos / a confirmar en la fase piloto

- Disponibilidad real de `sudo` en cada uno de los 5 servidores (afecta
  la instalación de rclone si falla la vía sin privilegios).
- Topología de red real de los 5 servidores: si dos o más comparten el
  mismo proveedor/datacenter con un único enlace ascendente, la
  contención podría reaparecer a otro nivel distinto al diagnosticado.
- Salida HTTPS a `googleapis.com` puede estar bloqueada por firewall en
  algún servidor — cubierto por la verificación de viabilidad (3.1) con
  fallback automático, pero a confirmar caso por caso.

## 12. Estado de implementación (corte 2026-07-21 18:30, sesión con límite de tiempo)

**Hecho:**
- `core/remote_upload_manager.py` — clase `RemoteUploadManager` completa:
  - `check_viable()`: verifica salida HTTPS a Google + `rclone` instalado
    (auto-instala si falta, con y sin `sudo`, reutilizando el patrón de
    `filestore_manager.py` para `zip`).
  - `upload_file()`: despliega credencial efímera + config aislado,
    dispara `rclone copy` vía `execute_long()` (mismo patrón de heartbeat
    que `pg_dump`/`tar`), verifica integridad por archivo (`rclone check
    --one-way`, nunca un comando de alcance-de-carpeta), y limpia la
    credencial en un `finally` — corre siempre, éxito o error.
  - Credencial registrada en `RemoteTempRegistry` antes de usarse, para
    que el barrido de huérfanos la limpie si el proceso muere a mitad de
    camino.
- 5 tests con mocks (`unittest.mock`, sin tocar servidores reales):
  viabilidad ok/sin-internet/auto-instala, subida feliz con
  registro/desregistro de credencial, y subida fallida confirmando que la
  credencial se limpia igual. Todos pasando. `py_compile` limpio en todos
  los módulos tocados.

**Hecho (segundo tramo, 18:09-18:30):**
- `core/scheduler.py._run_rule()` ya integra `RemoteUploadManager`:
  - Nuevo campo opcional por regla `rule["upload_mode"]` (`"direct"` para
    activarlo; cualquier otro valor o ausente = comportamiento actual sin
    cambios).
  - Si `upload_mode == "direct"` y el destino es Drive (no aplica a
    `local`/`remote`), llama `RemoteUploadManager.check_viable()` **una
    vez por corrida de la regla** (no por archivo). Si no es viable,
    loguea el motivo y usa el modo relé de siempre para esa corrida —
    nunca fuerza el modo directo.
  - `upload_fn = _upload_one_to_drive_direct if use_direct else
    _upload_one_to_drive` — ambas rutas (subida paralela de 3 archivos y
    subida de archivo único) usan `upload_fn`, sin duplicar lógica.
  - El modo directo NO pasa por `_drive_upload_semaphore` (no consume el
    ancho de banda de esta máquina, así que no compite con otras reglas
    en modo relé).
  - `run_rule_now()` (botón "Ejecutar ahora") no necesitó cambios propios
    — ya delega directo a `_run_rule(rule)`, así que respeta
    `upload_mode` automáticamente en cuanto el diccionario de la regla lo
    tenga.
- Verificado: con ninguna regla existente teniendo `upload_mode` seteado
  todavía, `rule.get("upload_mode") == "direct"` es `False` para las 5
  reglas actuales — **comportamiento de producción sin cambios** hasta
  que se active explícitamente. `py_compile` limpio en todos los módulos
  tocados.

**Hecho (tercer tramo, 18:15-18:30):**
- Selector en la GUI (`gui/app.py`, diálogo de edición de regla): nuevo
  checkbox "Subida directa servidor -> Drive (rclone, evita el relé por
  esta máquina)" dentro del panel de Drive (`self._pnl_gdrive`) — visible
  solo cuando el destino es Drive, junto con una nota de que cae al modo
  relé automáticamente si el servidor no califica.
  - `self._v_upload_direct` (BooleanVar), inicializado desde
    `r.get("upload_mode") == "direct"` al abrir el diálogo.
  - Al guardar, escribe `"upload_mode": "direct" | "relay"` en el
    diccionario de la regla — coherente con lo que ya lee
    `scheduler.py._run_rule()`.
- `py_compile` limpio + smoke test de arranque de la app (`APP_INIT_OK`)
  sin errores.

- Probado contra la lógica real del diálogo (`_ScheduleDialog`, no mocks
  de UI) usando el `ProfileManager` real y el perfil "Novalens" ya
  guardado en `servers.json`: regla nueva sin marcar -> `upload_mode:
  "relay"`; checkbox marcado -> `"direct"`; reabrir una regla existente
  con `upload_mode: "direct"` -> el checkbox se precarga en `True`. 3/3
  tests pasando.
- **2026-07-22: prueba visual en pantalla confirmada por el usuario**
  tras recompilar y relanzar el `.exe` (ver sección 14) — el checkbox y
  el resto de los cambios de esta sesión se ven y funcionan correctamente
  en la app real, no solo en la lógica aislada.

**Pendiente (no iniciado):**
- Activar `upload_mode: "direct"` en la regla real de novalens y correr
  con "Ejecutar ahora", confirmando en el log la línea "Subida directa
  servidor->Drive habilitada" y que el backup completo (dump+filestore)
  efectivamente sale por rclone, no por streaming.
- Repetir el rollout gradual (sección 9) para equiredes, bancasa,
  variedades, mega una vez novalens quede validado en producción real.

**Siguiente sesión:** abrir la app, ir a Tab Automatización, editar la
regla de novalens, marcar el checkbox nuevo, guardar, y validar con
"Ejecutar ahora" antes de esperar al horario real (11:00).

## 13. Validación real end-to-end en novalens (2026-07-22) — EXITOSA, con un bug encontrado y corregido en el camino

Se activó `upload_mode: "direct"` en la regla real de novalens (via
`ScheduleManager.update()`, no edición manual del JSON) y se disparó una
corrida real completa vía `BackupScheduler.run_rule_now()` — el mismo
código que usa el botón "Ejecutar ahora" de la GUI, no un script aislado.

**Primer intento — bug de concurrencia descubierto:**
El log mostró `"Subida directa servidor->Drive habilitada (rclone)"`,
subió dump+filestore+inventory en paralelo, pero la verificación del
filestore falló: `"file not in Google drive root"`. Investigado: **el
archivo SÍ se había subido correctamente** (confirmado por ID directo en
Drive, timestamp de creación ~2 min DESPUÉS de que la verificación lo
diera por "no encontrado"). Causa raíz: `core/ssh_client.py.execute_long()`
usaba nombres de archivo centinela fijos (`/tmp/.obt_done_ok`,
`/tmp/.obt_done_err`, `/tmp/.obt_pid`, `/tmp/.obt_nohup.log`) — funciona
para llamadas secuenciales, pero `RemoteUploadManager` ahora dispara 3
`execute_long()` concurrentes en el MISMO `SSHClient` (dump + filestore +
inventory subiendo en paralelo vía `ThreadPoolExecutor` en
`scheduler.py._run_rule`), y esas 3 llamadas se pisaban los centinelas
entre sí — una subida se marcaba "terminada" por el centinela de OTRA.

**Fix aplicado** (`core/ssh_client.py.execute_long()`): cada llamada
genera un `call_id` único (`uuid.uuid4().hex[:8]`) y usa rutas de
centinela/PID/log exclusivas para esa invocación. Sin este fix, CUALQUIER
subida paralela de múltiples archivos vía `execute_long()` en la misma
conexión SSH tenía el mismo riesgo — no era exclusivo del modo directo a
Drive, aplica a cualquier uso futuro de `execute_long()` concurrente.

**Segundo intento (tras el fix) — éxito limpio confirmado:**
```
Subida directa servidor->Drive habilitada (rclone).
Subiendo 3 archivos a Drive en paralelo ...
Subida directa completa y verificada: novalens_..._inventory.json
Subida directa completa y verificada: odoo_novalens_....sql
Subida directa completa y verificada: filestore_novalens_....tar
Limpiado del servidor: (los 3 archivos)
Backup completado exitosamente
RESULTADO FINAL: ok
```
`dump 452MB` + `filestore 2.83GB` subidos vía rclone directo, verificados,
`/tmp` del servidor limpiado automáticamente al final (`cleanup_server`).

**Estado tras esta validación:** el modo directo para novalens está
**funcionando correctamente en el código de producción real** (no un
script de prueba aislado). `upload_mode: "direct"` queda activo en la
regla de novalens — la próxima corrida programada real (11:00) usará este
camino automáticamente.

**Deliberadamente NO se activó todavía en los otros 4 clientes**
(equiredes, bancasa, variedades, mega), aunque el chequeo de viabilidad
(`check_viable()`) confirmó que los 4 califican (Ubuntu, root, sudo,
salida HTTPS a Google — bancasa y equiredes no tenían rclone instalado
pero el auto-instalador lo resuelve). Esto respeta el plan de rollout
gradual de la sección 9: confirmar en producción real 2-3 días antes de
tocar el siguiente cliente. Migrar a los demás sin esperar esa validación
sería repetir el mismo patrón de riesgo que causó el incidente del 21-jul.

## 14. Recompilación y despliegue (2026-07-22)

Al pedir recompilar (`build.bat`), se encontró y corrigió un problema
real no relacionado con el diseño de subida directa pero que bloqueaba
la validación en caliente:

- **`build.bat` tenía finales de línea Unix (LF) en vez de Windows
  (CRLF)** — corrompía la interpretación de `cmd.exe`, produciendo
  errores crípticos ("Odoo no se reconoce como un comando..."). Corregido
  (solo finales de línea, sin tocar el contenido/lógica del script).
- **`build.sh` (Linux) estaba desincronizado de `build.bat`**: le
  faltaban los `--hidden-import` de Google Drive
  (`google.oauth2.service_account`, `googleapiclient.*`, etc.) y de
  `plyer` — un build en Linux habría compilado "exitosamente" pero con
  la subida a Drive y las notificaciones rotas en tiempo de ejecución.
  Sincronizado con los mismos hidden-imports (adaptando
  `plyer.platforms.win.notification` → `plyer.platforms.linux.notification`
  para ese SO) y el mismo patrón de verificación de código de salida real
  de PyInstaller que ya tenía `build.bat`. Verificado con `bash -n`
  (sintaxis válida) — no probado end-to-end porque no hay máquina Linux
  disponible en esta sesión.

Con `build.bat` corregido, la compilación de Windows terminó limpia
("Build complete!"). Se cerró la instancia anterior del `.exe` (no había
ninguna corriendo) y se lanzó la nueva build
(`output\dist\OdooBackupTool.exe`, generado 2026-07-22) — arrancó y quedó
respondiendo sin crashear, confirmando que todas las dependencias nuevas
(Google API, plyer) se empaquetaron bien.

**Confirmado por el usuario tras prueba visual en pantalla:** el
checkbox "Subida directa servidor -> Drive" y el resto de los cambios de
esta sesión (indicador de cierre, etc.) se ven y funcionan correctamente
en la app real compilada — ya no es solo una validación de lógica
aislada.

## 15. Rollout real más rápido de lo planeado, y timeout fijo descubierto (2026-07-22)

El usuario activó `upload_mode: "direct"` en las 5 reglas (no solo
novalens como preveía el rollout gradual de la sección 9) y las corrió.
Resultado de una corrida real con las 5 en simultáneo:

| Cliente | Resultado | Detalle |
|---|---|---|
| bancasa | OK | ~22 MiB/s, filestore 11.1 GiB |
| equiredes | OK | ~20-24 MiB/s, filestore 7.6 GiB |
| mega | OK | ~24-25 MiB/s, filestore 7.5 GiB |
| variedades | **ERROR: Timeout** | ~3.3-3.6 MiB/s, filestore 14.6 GiB — el más grande y el servidor con menor ancho de banda propio |

**Causa del error de variedades:** no fue un cuelgue real. La subida
avanzó de forma sana y sostenida durante **72 minutos** (10.4 de 14.6 GB,
72%) sin ningún hueco ni reintento — simplemente ese servidor tiene menos
ancho de banda de salida que los otros 4. `RemoteUploadManager.upload_file()`
tenía un **timeout fijo de 3600s (1h)** en la llamada a `execute_long()`,
sin relación al tamaño del archivo ni a la velocidad real del servidor —
mismo patrón de falla que el incidente de `_StallWatchdog` (sección 15.5
del diagnóstico original): un mecanismo rígido matando una transferencia
lenta-pero-sana en vez de una realmente muerta.

**Fix aplicado:** `_upload_timeout()` (nuevo método) calcula el timeout
por archivo según su tamaño real (`stat -c %s` en el servidor) asumiendo
un piso conservador de 1 MiB/s + 25% de margen, acotado entre 1h (piso,
para archivos chicos) y 6h (techo, para no dejar un proceso realmente
colgado sin detectar indefinidamente). Para el caso real de variedades
(14.6 GiB) esto da ~5.2h de margen — sobra para los 72 min que
efectivamente necesitó. Verificado con 4 tests con mocks: caso real de
variedades, archivo chico (respeta el piso), archivo hipotético enorme
(respeta el techo), y fallback si `stat` falla.

**Nota:** el ancho de banda limitado de variedades es del servidor mismo,
no de esta máquina — coherente con el diagnóstico original (sección 1):
ya no hay nada que optimizar de este lado, es una característica real del
hosting de ese cliente específico.

## 16. Segundo falso negativo: latencia de indexación de Drive (2026-07-23)

Primera corrida programada real de las 5 reglas tras recompilar con el
fix del timeout. bancasa's dump (1.79 GiB) falló verificación:
`"file not in Google drive root ''"` a las 09:13:44. Verificado en Drive
por ID: el archivo **sí existía**, creado a las **09:13:39 — 5 segundos
antes** de que el check lo diera por no encontrado.

**Causa:** latencia de indexación de la API de Drive — `rclone copy`
reportando éxito solo significa que la llamada de subida retornó, no que
el archivo ya es visible vía `files.list` (que es como `rclone check`
localiza el archivo por nombre). Documentado ya en el propio código de
`gdrive.py` para la ruta de relé (`_fetch_uploaded_file_meta`); esta es
la misma clase de problema aplicada a la ruta directa.

**Fix:** el `rclone check` en `RemoteUploadManager.upload_file()` ahora
reintenta hasta 3 veces con 5s de espera entre intentos antes de declarar
fallo real. Verificado con 2 tests con mocks: recuperación al 3er intento
(con el delay real de ~10s), y fallo genuino si nunca aparece (no se
convierte en un falso positivo permanente).

Recompilado y relanzado tras este fix (mismo procedimiento de las
secciones 14/15).
