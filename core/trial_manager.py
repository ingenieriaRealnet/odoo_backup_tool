"""
TrialManager — gestion de parametros de licencia/trial de Odoo.

Flujo principal:
  1. find_odoo()          Detecta el binario y config de Odoo en el servidor.
  2. create_clean_db()    Crea una BD limpia e inicializa Odoo con -i base.
  3. query_db_params()    Lee ir_config_parameter WHERE key LIKE 'database%'.
  4. apply_trial_params() Actualiza una BD objetivo con esos valores y elimina
                          las entradas de expiracion.
  5. drop_db()            Elimina la BD temporal (limpieza).

Todos los metodos son seguros para llamar desde un hilo de fondo; no
tocan ninguna variable de tkinter directamente.
"""
from __future__ import annotations

import re
from typing import Callable

from .ssh_client import SSHClient

# Rutas candidatas para la configuracion de Odoo
_CONF_CANDIDATES = [
    "/etc/odoo/odoo.conf",
    "/etc/odoo18/odoo.conf",
    "/etc/odoo17/odoo.conf",
    "/opt/odoo/odoo.conf",
    "/opt/odoo18/odoo.conf",
    "/opt/odoo/conf/odoo.conf",
    "/home/odoo/odoo.conf",
]

# Claves de ir_config_parameter que se manejan en el flujo Trial.
# Las primeras tres se copian; las ultimas dos se eliminan.
_KEYS_COPY   = ("database.secret", "database.uuid", "database.create_date")
_KEYS_DELETE = ("database.expiration_reason", "database.expiration_date")


class TrialManager:
    """Gestiona la creacion de BD limpia y la transferencia de parametros trial."""

    def __init__(self, ssh: SSHClient) -> None:
        self._ssh = ssh

    # ── Deteccion de Odoo ─────────────────────────────────────────────────────

    def find_odoo(self) -> tuple[str, str]:
        """
        Detecta el binario y el archivo de configuracion de Odoo en el servidor.

        Estrategia:
          1. Lee los argumentos del proceso odoo-bin en ejecucion (ps aux).
          2. Busca el binario con find si el proceso no esta activo.
          3. Prueba rutas de config conocidas.

        Returns:
            (odoo_bin_path, odoo_conf_path) — cadenas vacias si no se encuentra.
        """
        odoo_bin  = ""
        odoo_conf = ""

        # ── 1. Proceso en ejecucion ──────────────────────────────────────────
        _, ps_out, _ = self._ssh.execute(
            "ps aux | grep -E '(odoo-bin|odoo\\.py)' | grep -v grep | head -5"
        )
        if ps_out.strip():
            for line in ps_out.splitlines():
                # Extraer ruta del binario (primer token que termina en odoo-bin u odoo.py)
                bin_match = re.search(r'(\S+(?:odoo-bin|odoo\.py))', line)
                if bin_match and not odoo_bin:
                    odoo_bin = bin_match.group(1)
                # Extraer -c /ruta/config.conf
                conf_match = re.search(r'-c\s+(\S+\.conf)', line)
                if conf_match and not odoo_conf:
                    odoo_conf = conf_match.group(1)

        # ── 2. Busqueda del binario si el proceso no esta corriendo ──────────
        if not odoo_bin:
            _, find_out, _ = self._ssh.execute(
                "find /opt /usr /home -maxdepth 7 -name 'odoo-bin' -type f "
                "2>/dev/null | head -3",
                timeout=20,
            )
            for line in find_out.splitlines():
                candidate = line.strip()
                if candidate:
                    odoo_bin = candidate
                    break

        # ── 3. Busqueda del config si no se encontro en el proceso ───────────
        if not odoo_conf:
            for path in _CONF_CANDIDATES:
                code, _, _ = self._ssh.execute(f"test -f {path}")
                if code == 0:
                    odoo_conf = path
                    break

        return odoo_bin, odoo_conf

    # ── BD temporal ───────────────────────────────────────────────────────────

    def create_clean_db(
        self,
        db_name:    str,
        odoo_bin:   str,
        odoo_conf:  str,
        log_callback:  Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> None:
        """
        Crea una BD PostgreSQL vacia e inicializa Odoo con el modulo base.

        Args:
            db_name:    Nombre de la BD temporal a crear.
            odoo_bin:   Ruta absoluta al binario odoo-bin en el servidor.
            odoo_conf:  Ruta absoluta al archivo de configuracion de Odoo.
            log_callback: Funcion opcional para emitir mensajes de progreso.
            cancel_event: threading.Event; si se activa, cancela la operacion.

        Raises:
            RuntimeError: Si createdb o la inicializacion de Odoo fallan.
        """
        def _log(msg: str) -> None:
            if log_callback:
                log_callback(msg)

        # Limpiar BD residual de una ejecucion anterior (si existe)
        _log(f"Verificando si '{db_name}' ya existe ...")
        self._ssh.execute(f"sudo -u postgres dropdb --if-exists {db_name}")

        # Crear BD en blanco con el owner odoo para que peer auth funcione
        _log(f"Creando BD PostgreSQL '{db_name}' (owner: odoo) ...")
        code, _, err = self._ssh.execute(
            f"sudo -u postgres createdb -O odoo {db_name}"
        )
        if code != 0:
            raise RuntimeError(f"Error al crear la BD '{db_name}':\n{err}")

        _log(f"BD '{db_name}' creada.  Iniciando Odoo con '-i base' ...")
        _log("(Este proceso puede tardar 1-3 minutos)")

        def _heartbeat(status: str) -> None:
            _log(f"  [odoo init] {status}")

        # Ejecutar como usuario 'odoo' para que la autenticacion peer de
        # PostgreSQL funcione (el config usa db_user = odoo con peer auth).
        # --no-http evita abrir el servidor web durante la inicializacion.
        code, _, err = self._ssh.execute_long(
            f"sudo -u odoo {odoo_bin} -c {odoo_conf} -d {db_name} "
            f"-i base --stop-after-init --no-http",
            watch_cmd=f"sudo -u postgres psql -d {db_name} -t -c "
                      f"\"SELECT count(*) FROM ir_config_parameter\" 2>/dev/null "
                      f"|| echo 'inicializando...'",
            heartbeat_callback=_heartbeat,
            heartbeat_interval=15,
            timeout=600,
            cancel_event=cancel_event,
        )
        if code != 0:
            # Intentar limpiar la BD fallida
            self._ssh.execute(f"sudo -u postgres dropdb --if-exists {db_name}")
            raise RuntimeError(
                f"Error al inicializar Odoo en '{db_name}':\n{err or '(sin detalle)'}"
            )

        _log(f"BD '{db_name}' inicializada correctamente.")

    def query_db_params(self, db_name: str) -> list[dict]:
        """
        Lee los parametros database.* de ir_config_parameter.

        Returns:
            Lista de dicts con keys: key, value, create_date, write_date.
            Vacia si no se encuentran registros.

        Raises:
            RuntimeError: Si la consulta falla.
        """
        sql = (
            "SELECT key, value, create_date::text, write_date::text "
            "FROM ir_config_parameter "
            "WHERE key LIKE 'database%' "
            "ORDER BY key"
        )
        code, out, err = self._ssh.execute(
            f"sudo -u postgres psql -d {db_name} -t -A "
            f"--field-separator='|' -c \"{sql}\""
        )
        if code != 0:
            raise RuntimeError(
                f"Error al consultar '{db_name}':\n{err or out}"
            )

        params: list[dict] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) >= 2:
                params.append({
                    "key":         parts[0].strip(),
                    "value":       parts[1].strip() if len(parts) > 1 else "",
                    "create_date": parts[2].strip() if len(parts) > 2 else "",
                    "write_date":  parts[3].strip() if len(parts) > 3 else "",
                })
        return params

    def drop_db(self, db_name: str) -> None:
        """Elimina la BD temporal (best-effort, no lanza excepciones)."""
        self._ssh.execute(f"sudo -u postgres dropdb --if-exists {db_name}")

    # ── Aplicar valores trial ─────────────────────────────────────────────────

    def apply_trial_params(
        self,
        target_db:    str,
        source_params: list[dict],
        log_callback:  Callable[[str], None] | None = None,
    ) -> None:
        """
        Actualiza ir_config_parameter en target_db con los valores de source_params
        y elimina las entradas de expiracion.

        Operaciones:
          - UPDATE database.secret, database.uuid, database.create_date
            (value, create_date, write_date copiados de la BD fuente).
          - DELETE database.expiration_reason, database.expiration_date.

        Args:
            target_db:     Nombre de la BD que recibe los cambios.
            source_params: Lista de dicts de query_db_params() de la BD limpia.
            log_callback:  Funcion opcional para progreso.

        Raises:
            RuntimeError: Si alguna operacion de base de datos falla.
        """
        def _log(msg: str) -> None:
            if log_callback:
                log_callback(msg)

        # Indizar params por clave para acceso rapido
        by_key = {p["key"]: p for p in source_params}

        # ── UPDATES ───────────────────────────────────────────────────────────
        statements: list[str] = []
        for key in _KEYS_COPY:
            p = by_key.get(key)
            if not p:
                _log(f"ADVERTENCIA: clave '{key}' no encontrada en BD fuente — se omite.")
                continue

            val  = p["value"].replace("'", "''")
            cd   = p["create_date"].replace("'", "''")
            wd   = p["write_date"].replace("'", "''")

            statements.append(
                f"UPDATE ir_config_parameter "
                f"SET value='{val}', create_date='{cd}', write_date='{wd}' "
                f"WHERE key='{key}';"
            )
            _log(f"  UPDATE {key} → {val[:36]}...")

        # ── DELETES ───────────────────────────────────────────────────────────
        for key in _KEYS_DELETE:
            statements.append(
                f"DELETE FROM ir_config_parameter WHERE key='{key}';"
            )
            _log(f"  DELETE {key}")

        if not statements:
            raise RuntimeError("No hay operaciones a ejecutar.")

        sql_block = " ".join(statements)
        _log(f"Aplicando {len(statements)} operacion(es) en '{target_db}' ...")

        code, out, err = self._ssh.execute(
            f"sudo -u postgres psql -d {target_db} -c \"{sql_block}\""
        )
        if code != 0:
            raise RuntimeError(
                f"Error al aplicar cambios en '{target_db}':\n{err or out}"
            )

        _log(f"Parametros actualizados correctamente en '{target_db}'.")

    # ── Verificacion ─────────────────────────────────────────────────────────

    def verify_target_params(self, target_db: str) -> list[dict]:
        """
        Lee los parametros database.* de la BD objetivo tras la actualizacion.

        Util para confirmar que los cambios se aplicaron correctamente.
        Reutiliza query_db_params() con el nombre de la BD objetivo.
        """
        return self.query_db_params(target_db)

    # ── Listado de BDs ────────────────────────────────────────────────────────

    def list_databases(self) -> list[str]:
        """Lista todas las BDs no-sistema disponibles en el servidor."""
        _SYSTEM = {"template0", "template1", "postgres"}
        code, out, err = self._ssh.execute(
            "sudo -u postgres psql -l -t -A --field-separator='|'"
        )
        if code != 0:
            raise RuntimeError(f"No se pudo listar las bases de datos:\n{err}")
        dbs: list[str] = []
        for line in out.splitlines():
            name = line.split("|")[0].strip()
            if name and name not in _SYSTEM:
                dbs.append(name)
        return sorted(dbs)
