"""
api_cli.py — command line for the API connections of Odoo Backup Tool.

Meant for terminal use (and for Claude Code: short text output instead of browser
screenshots). Runs from source; the .exe is windowed and has no console.

Connections (stored in ~/.odoo_backup_tool/api_connections.json, keys in the
Windows Credential Manager):
    python api_cli.py conn list
    python api_cli.py conn show realnet
    python api_cli.py conn add realnet --type odoo_json2 --url https://realnet.com.co \
                               --database <db> --user ingenieria@realnet.com.co [--board-defaults]
    python api_cli.py conn set-key realnet        # asks the key without echo (run it yourself)
    python api_cli.py conn test realnet
    python api_cli.py conn delete realnet --yes

Realnet task board / helpdesk (connection 'realnet' unless -c is given):
    python api_cli.py board task new "Título"
    python api_cli.py board task plan 231 --deadline "2026-10-14 17:00" --start "2026-10-14 09:00" --hours 02:00 --desc "..."
    python api_cli.py board task start 231
    python api_cli.py board task stop 231 --desc "Lo que se hizo"   [--discard]
    python api_cli.py board task done 231
    python api_cli.py board task show 231 | board task list [--stage Nuevo]
    python api_cli.py board ticket n3 | ticket show 2149 [--full] | ticket news --since 2026-10-13 | ticket sla

SAFETY: every write prints a PREVIEW and exits with code 3 without changing anything,
unless --yes is given (after the person confirmed the preview).
"""
from __future__ import annotations

import argparse
import copy
import getpass
import json
import sys

from core.api.connections import ApiConnectionManager
from core.api.connectors import CONNECTOR_TYPES, ApiError, OdooJson2Connector
from core.api.realnet_board import DEFAULT_SETTINGS, RealnetBoard

EXIT_PREVIEW = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Conexiones API de Odoo Backup Tool.")
    sub = parser.add_subparsers(dest="area", required=True)

    conn = sub.add_parser("conn", help="Administrar conexiones").add_subparsers(dest="cmd", required=True)
    conn.add_parser("list")
    for cmd in ("show", "test", "set-key"):
        conn.add_parser(cmd).add_argument("name")
    p = conn.add_parser("delete"); p.add_argument("name"); p.add_argument("--yes", action="store_true")
    p = conn.add_parser("add")
    p.add_argument("name"); p.add_argument("--type", required=True, choices=sorted(CONNECTOR_TYPES))
    p.add_argument("--url", required=True); p.add_argument("--user", default="")
    p.add_argument("--database", default=""); p.add_argument("--test-path", default="")
    p.add_argument("--notes", default="")
    p.add_argument("--board-defaults", action="store_true",
                   help="Cargar los parámetros por defecto del tablero Realnet en 'settings'.")

    board = sub.add_parser("board", help="Tablero de tareas y mesa de ayuda de Realnet")
    board.add_argument("-c", "--conn", default="realnet", help="Conexión odoo_json2 a usar (default: realnet)")
    area = board.add_subparsers(dest="board_area", required=True)

    task = area.add_parser("task").add_subparsers(dest="cmd", required=True)
    p = task.add_parser("new"); p.add_argument("title"); p.add_argument("--yes", action="store_true")
    p = task.add_parser("plan"); p.add_argument("id", type=int)
    p.add_argument("--deadline"); p.add_argument("--start"); p.add_argument("--hours"); p.add_argument("--desc")
    p.add_argument("--yes", action="store_true")
    p = task.add_parser("start"); p.add_argument("id", type=int); p.add_argument("--yes", action="store_true")
    p = task.add_parser("stop"); p.add_argument("id", type=int); p.add_argument("--desc")
    p.add_argument("--discard", action="store_true"); p.add_argument("--yes", action="store_true")
    p = task.add_parser("done"); p.add_argument("id", type=int); p.add_argument("--yes", action="store_true")
    p = task.add_parser("show"); p.add_argument("id", type=int)
    p = task.add_parser("list"); p.add_argument("--stage"); p.add_argument("--limit", type=int, default=20)

    ticket = area.add_parser("ticket").add_subparsers(dest="cmd", required=True)
    ticket.add_parser("n3")
    ticket.add_parser("sla")
    p = ticket.add_parser("show"); p.add_argument("id", type=int); p.add_argument("--full", action="store_true")
    p = ticket.add_parser("news"); p.add_argument("--since", required=True, help="AAAA-MM-DD[ HH:MM] hora local")
    return parser


# ── conn ─────────────────────────────────────────────────────────────────────

def run_conn(manager: ApiConnectionManager, args) -> tuple[bool, str]:
    if args.cmd == "list":
        rows = manager.list()
        if not rows:
            return True, "Sin conexiones. Cree una con: api_cli.py conn add ..."
        return True, "\n".join(
            f"{c['name']:<16} {c['type']:<12} {c['base_url']:<36} clave={'sí' if manager.has_secret(c['name']) else 'NO'}"
            for c in rows)
    if args.cmd == "show":
        connection = manager.get(args.name)
        if not connection:
            raise ApiError(f"No existe la conexión '{args.name}'.")
        connection["clave_guardada"] = manager.has_secret(args.name)
        return True, json.dumps(connection, indent=2, ensure_ascii=False)
    if args.cmd == "add":
        connection = {"name": args.name, "type": args.type, "base_url": args.url, "user": args.user,
                      "notes": args.notes, "settings": copy.deepcopy(DEFAULT_SETTINGS) if args.board_defaults else {}}
        if args.database:
            connection["database"] = args.database
        if args.test_path:
            connection["test_path"] = args.test_path
        existed = manager.get(args.name) is not None
        manager.save(connection, original_name=args.name if existed else None)
        return True, f"Conexión '{args.name}' {'actualizada' if existed else 'creada'}. Guarde la clave con: conn set-key {args.name}"
    if args.cmd == "set-key":
        if not sys.stdin.isatty():
            raise ApiError("set-key debe ejecutarse en una terminal interactiva (la clave se pide sin mostrarla).")
        manager.set_secret(args.name, getpass.getpass(f"Clave API de '{args.name}' (no se muestra): "))
        return True, f"Clave de '{args.name}' guardada en el Administrador de credenciales de Windows."
    if args.cmd == "test":
        return True, manager.connector(args.name).test()
    if args.cmd == "delete":
        if not args.yes:
            return False, f"PREVIEW eliminar la conexión '{args.name}' y su clave guardada."
        manager.delete(args.name)
        return True, f"Conexión '{args.name}' eliminada."
    raise ApiError("Comando no reconocido.")


# ── board ────────────────────────────────────────────────────────────────────

def run_board(manager: ApiConnectionManager, args) -> tuple[bool, str]:
    connector = manager.connector(args.conn)
    if not isinstance(connector, OdooJson2Connector):
        raise ApiError(f"La conexión '{args.conn}' no es de tipo odoo_json2.")
    settings = connector.connection.get("settings") or {}
    board = RealnetBoard(connector, {**DEFAULT_SETTINGS, **settings})

    if args.board_area == "task":
        if args.cmd == "new":
            return board.task_new(args.title, args.yes)
        if args.cmd == "plan":
            return board.task_plan(args.id, args.deadline, args.start, args.hours, args.desc, args.yes)
        if args.cmd == "start":
            return board.task_start(args.id, args.yes)
        if args.cmd == "stop":
            return board.task_stop(args.id, args.desc, args.discard, args.yes)
        if args.cmd == "done":
            return board.task_done(args.id, args.yes)
        if args.cmd == "show":
            return True, board.format_task(board.task(args.id), with_lines=True)
        if args.cmd == "list":
            return True, board.task_list(args.stage, args.limit)
    if args.cmd == "n3":
        return True, board.ticket_n3()
    if args.cmd == "show":
        return True, board.ticket_show(args.id, args.full)
    if args.cmd == "news":
        return True, board.ticket_news(args.since)
    if args.cmd == "sla":
        return True, board.ticket_sla()
    raise ApiError("Comando no reconocido.")


def main(argv=None, manager: ApiConnectionManager | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    manager = manager or ApiConnectionManager()
    try:
        ok, text = run_conn(manager, args) if args.area == "conn" else run_board(manager, args)
    except (ApiError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(text)
    if text.startswith("PREVIEW"):
        return EXIT_PREVIEW
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
