"""
Deteccion de contenedores Docker con PostgreSQL en el servidor remoto.

Solo lectura: nunca crea, detiene ni modifica contenedores. Se usa para
poblar el selector de "Contenedor" en Tab 2 (Base de datos) y en Tab Trial,
permitiendo que DBManager/TrialManager operen contra Postgres dockerizado.
"""
from __future__ import annotations

import json
import re

from .ssh_client import SSHClient

_POSTGRES_IMAGE_RE = re.compile(r"postgres", re.IGNORECASE)


class DockerManager:
    """Consulta contenedores Docker en el servidor conectado por SSH."""

    def __init__(self, ssh: SSHClient) -> None:
        self._ssh = ssh
        self._sudo_needed: bool | None = None

    # ── Disponibilidad ───────────────────────────────────────────────────

    def is_docker_available(self) -> bool:
        """
        True si 'docker ps' responde en el servidor (con o sin sudo).

        Cachea si hizo falta sudo, para que list_postgres_containers()
        no tenga que volver a probarlo.
        """
        code, _, _ = self._ssh.execute("docker ps >/dev/null 2>&1")
        if code == 0:
            self._sudo_needed = False
            return True

        code, _, _ = self._ssh.execute("sudo docker ps >/dev/null 2>&1")
        if code == 0:
            self._sudo_needed = True
            return True

        return False

    @property
    def sudo_needed(self) -> bool:
        """True si 'docker' requiere sudo en este servidor. Llama a is_docker_available() antes."""
        return bool(self._sudo_needed)

    # ── Listado de contenedores Postgres ─────────────────────────────────

    def list_postgres_containers(self) -> list[dict]:
        """
        Lista contenedores cuya imagen sugiere PostgreSQL (o que publican
        el puerto 5432), con su usuario de Postgres inferido.

        Returns:
            Lista de dicts: {"name", "image", "status", "ports", "pg_user"}
        """
        if self._sudo_needed is None:
            if not self.is_docker_available():
                raise RuntimeError(
                    "Docker no esta disponible en este servidor "
                    "(o el usuario SSH no tiene permisos)."
                )

        prefix = "sudo docker" if self._sudo_needed else "docker"

        code, out, err = self._ssh.execute(
            f"{prefix} ps -a --format "
            f"'{{{{.Names}}}}|{{{{.Image}}}}|{{{{.Status}}}}|{{{{.Ports}}}}'"
        )
        if code != 0:
            raise RuntimeError(f"No se pudo listar contenedores Docker:\n{err}")

        containers: list[dict] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            name, image, status, ports = parts
            if not (_POSTGRES_IMAGE_RE.search(image) or "5432" in ports):
                continue
            containers.append({
                "name":   name.strip(),
                "image":  image.strip(),
                "status": status.strip(),
                "ports":  ports.strip(),
                "pg_user": self._infer_pg_user(name.strip(), prefix),
            })

        return containers

    def _infer_pg_user(self, container: str, prefix: str) -> str:
        """
        Intenta inferir el usuario de Postgres del contenedor leyendo la
        variable de entorno POSTGRES_USER. Si no aparece, asume 'postgres'
        (default de la imagen oficial de Postgres).
        """
        code, out, _ = self._ssh.execute(
            f"{prefix} inspect {container} --format '{{{{json .Config.Env}}}}'"
        )
        if code != 0 or not out.strip():
            return "postgres"

        try:
            env_list = json.loads(out.strip())
        except (json.JSONDecodeError, ValueError):
            return "postgres"

        for entry in env_list:
            if entry.startswith("POSTGRES_USER="):
                return entry.split("=", 1)[1].strip() or "postgres"

        return "postgres"
