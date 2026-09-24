"""
Capa de construccion de comandos psql/pg_dump/createdb/dropdb agnostica a
Docker.

Todas las operaciones de BD del proyecto (DBManager, TrialManager) ejecutaban
directamente "sudo -u postgres psql ..." asumiendo PostgreSQL bare-metal.
Este modulo centraliza esa construccion de comandos para que el mismo
codigo funcione tanto contra un Postgres nativo del host como contra uno
corriendo dentro de un contenedor Docker (docker exec).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PgTarget:
    """
    Describe donde y como ejecutar comandos de PostgreSQL en el servidor SSH.

    container vacio (default) reproduce exactamente el comportamiento
    bare-metal anterior a este cambio (sudo -u postgres ...).

    docker_exec_user: usuario del SISTEMA OPERATIVO dentro del contenedor
    con el que corre 'docker exec -u <user>'. Vacio (default) reproduce el
    comportamiento original: 'docker exec -i <container> psql -U <pg_user>'
    sin -u, que funciona con la imagen oficial de postgres (auth trust/md5
    por defecto). Algunos despliegues "todo en uno" (Odoo+Postgres en el
    mismo contenedor, ej. imagenes que configuran pg_hba.conf con auth
    'peer') exigen que el usuario de SO del docker exec coincida con el rol
    de Postgres — confirmado en el contenedor 'ecoerp-demo' de Pruebas_19,
    donde 'psql -U odoo' directo falla con "Peer authentication failed" y
    solo funciona 'docker exec -u postgres <container> psql ...' (sin -U
    explicito, peer-autentica como el rol 'postgres'). Cuando se setea,
    tiene prioridad sobre pg_user para el rol de conexion.
    """
    container: str = ""
    pg_user: str = "postgres"
    docker_sudo: bool = False   # True si 'docker exec' requiere sudo en este servidor
    docker_exec_user: str = ""  # usuario de SO para 'docker exec -u' (auth peer)


def _docker_prefix(target: PgTarget) -> str:
    prefix = "sudo docker" if target.docker_sudo else "docker"
    if target.docker_exec_user:
        return f"{prefix} exec -u {target.docker_exec_user} -i"
    return f"{prefix} exec -i"


def _psql_user_flag(target: PgTarget) -> str:
    """
    Flag -U para psql/pg_dump/createdb/dropdb.

    En modo docker_exec_user (auth peer), se omite -U: el rol de conexion
    lo determina la autenticacion peer contra el usuario de SO del exec
    (docker_exec_user), no un -U explicito.
    """
    if target.docker_exec_user:
        return ""
    return f"-U {target.pg_user} "


def build_psql(target: PgTarget, args: str) -> str:
    """Arma el comando psql correcto segun el target (bare-metal o contenedor)."""
    if target.container:
        return f"{_docker_prefix(target)} {target.container} psql {_psql_user_flag(target)}{args}"
    return f"sudo -u postgres psql {args}"


def build_pg_dump(target: PgTarget, args: str) -> str:
    """Arma el comando pg_dump correcto segun el target."""
    if target.container:
        return f"{_docker_prefix(target)} {target.container} pg_dump {_psql_user_flag(target)}{args}"
    return f"sudo -u postgres pg_dump -U postgres {args}"


def build_createdb(target: PgTarget, args: str) -> str:
    """Arma el comando createdb correcto segun el target."""
    if target.container:
        return f"{_docker_prefix(target)} {target.container} createdb {_psql_user_flag(target)}{args}"
    return f"sudo -u postgres createdb {args}"


def build_dropdb(target: PgTarget, args: str) -> str:
    """Arma el comando dropdb correcto segun el target."""
    if target.container:
        return f"{_docker_prefix(target)} {target.container} dropdb {_psql_user_flag(target)}{args}"
    return f"sudo -u postgres dropdb {args}"


def wrap_exec(target: PgTarget, command: str) -> str:
    """
    Envuelve un comando arbitrario (ej. el binario odoo-bin) para correrlo
    dentro del contenedor si target.container esta definido, o tal cual en
    el host si es bare-metal.
    """
    if target.container:
        return f"{_docker_prefix(target)} {target.container} {command}"
    return command
