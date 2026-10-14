"""
Realnet task board ("Planificación Juan esteban") and helpdesk on realnet.com.co,
on top of an ``odoo_json2`` API connection.

Replaces the Chrome-driven flow (hundreds of screenshots a day) with plain API calls.
Writes reproduce exactly what the UI buttons do:
  * "Iniciar"  -> project.task.action_timer_start
  * "Detener"  -> project.task.action_timer_stop, which returns the
                  hr.timesheet.stop.timer.confirmation.wizard; creating it computes the
                  rounded time and action_save_timesheet / action_delete_timesheet applies it.
  * Fecha límite is written before Fecha de inicio, and Tiempo asignado last
    (project_enterprise recomputes allocated_hours from the dates).
Helpdesk functions are READ-ONLY.

The board parameters live in the connection's ``settings`` (DEFAULT_SETTINGS below).
"""
from __future__ import annotations

import html
import json
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from core.api.connectors import ApiError

ODOO_DT = "%Y-%m-%d %H:%M:%S"
LOCAL_DT = "%Y-%m-%d %H:%M"

DEFAULT_SETTINGS = {
    "utc_offset_hours": -5,
    "tasks": {
        "project_id": 50,
        "employee_name": "LÓPEZ POSADA, JUAN ESTEBAN",
        "stage_new": "Nuevo",
        "stage_in_progress": "En proceso",
        "stage_done": "Finalizado",
    },
    "helpdesk": {
        "team_name": "SOPORTE ODOO - PRODUCCION",
        "stage_n3": "Escalado N3 - Desarrollo",
        "first_response_stages": ["Nuevo", "En atención N1"],
        "message_preview_chars": 400,
    },
}

TASK_FIELDS = [
    "name", "stage_id", "date_deadline", "planned_date_begin", "allocated_hours",
    "effective_hours", "timer_start", "user_ids", "description", "display_timesheet_timer",
]

_DEFAULT_TIMERS_PATH = Path.home() / ".odoo_backup_tool" / "api_timers.json"


# --------------------------------------------------------------------------- #
# Small pure helpers (unit-tested)
# --------------------------------------------------------------------------- #

def local_to_utc(text, utc_offset_hours):
    """'2026-10-14 09:00' in local time -> '2026-10-14 14:00:00' UTC (Odoo storage)."""
    local = datetime.strptime(text.strip(), LOCAL_DT)
    return (local - timedelta(hours=utc_offset_hours)).strftime(ODOO_DT)


def utc_to_local(value, utc_offset_hours):
    """Odoo UTC datetime string -> local 'YYYY-MM-DD HH:MM' ('-' when empty)."""
    if not value:
        return "-"
    return (datetime.strptime(value, ODOO_DT) + timedelta(hours=utc_offset_hours)).strftime(LOCAL_DT)


def hhmm_to_hours(text):
    """'02:30' -> 2.5. Also accepts a decimal number ('1.5')."""
    if ":" in text:
        hours, minutes = text.split(":")
        return int(hours) + int(minutes) / 60
    return float(text)


def hours_to_hhmm(value):
    total_minutes = round((value or 0) * 60)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def html_to_text(body, limit=None):
    """Strip tags from an Odoo HTML field and collapse whitespace."""
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", body or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + " […]"
    return text


def text_to_html(text):
    return "".join(f"<p>{html.escape(line)}</p>" for line in text.strip().splitlines() if line.strip())


def m2o_name(value):
    """Many2one JSON value [id, 'name'] -> 'name' ('-' when empty)."""
    return value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else "-"


# --------------------------------------------------------------------------- #
# Service layer
# --------------------------------------------------------------------------- #

class LocalTimerStore:
    """
    Start times of tasks timed locally: ~/.odoo_backup_tool/api_timers.json
    ({"<connection>:<task id>": "<UTC start>"}).

    Why: Odoo only shows the task timer when the API user has exactly one employee in
    the active company (timesheet_grid _compute_display_timesheet_timer). The shared
    user "Ingenieria" of realnet.com.co has none in REALNET S A S, so its timer is
    hidden; and the timesheet must be attributed to a specific employee anyway.
    The local timer keeps the start time on disk (survives a closed terminal) and,
    on stop, creates the "Registro de horas" line directly with the configured employee.
    """

    def __init__(self, path=None):
        self._path = Path(path) if path else _DEFAULT_TIMERS_PATH

    def _load(self):
        try:
            with open(self._path, encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self._path)

    def get(self, key):
        return self._load().get(key)

    def set(self, key, value):
        data = self._load()
        data[key] = value
        self._save(data)

    def pop(self, key):
        data = self._load()
        value = data.pop(key, None)
        self._save(data)
        return value


class RealnetBoard:
    """Business operations on the task board and the helpdesk, on top of the API client."""

    def __init__(self, client, settings, timer_store=None, now=None):
        """
        client: OdooJson2Connector; settings: the connection's settings (see DEFAULT_SETTINGS).
        timer_store / now: injectable for tests (LocalTimerStore, callable returning naive UTC datetime).
        """
        self.client = client
        self.timers = timer_store or LocalTimerStore()
        self._now = now or datetime.utcnow
        self._timer_ns = (getattr(client, "connection", None) or {}).get("name", "default")
        self.tz = settings["utc_offset_hours"]
        self.tasks_cfg = settings["tasks"]
        self.help_cfg = settings["helpdesk"]

    # --- lookups ----------------------------------------------------------- #

    def stage_id(self, stage_name):
        rows = self.client.search_read(
            "project.task.type",
            [("project_ids", "in", [self.tasks_cfg["project_id"]]), ("name", "=", stage_name)],
            ["id"], limit=1,
        )
        if not rows:
            raise ApiError(f"La etapa '{stage_name}' no existe en el proyecto {self.tasks_cfg['project_id']}.")
        return rows[0]["id"]

    def employee_id(self):
        name = self.tasks_cfg["employee_name"]
        rows = self.client.search_read("hr.employee", [("name", "=", name)], ["id"], limit=2)
        if len(rows) != 1:
            raise ApiError(f"Se esperaba exactamente un empleado '{name}' y se encontraron {len(rows)}.")
        return rows[0]["id"]

    def task(self, task_id):
        rows = self.client.read("project.task", [task_id], TASK_FIELDS + ["project_id"])
        if not rows:
            raise ApiError(f"La tarea {task_id} no existe o no es visible.")
        task = rows[0]
        if task["project_id"] and task["project_id"][0] != self.tasks_cfg["project_id"]:
            raise ApiError(f"La tarea {task_id} no pertenece al proyecto configurado.")
        task["timer_source"] = "odoo" if task["timer_start"] else None
        local_start = self.timers.get(self._timer_key(task_id))
        if local_start and not task["timer_start"]:
            task["timer_start"], task["timer_source"] = local_start, "local"
        return task

    def _timer_key(self, task_id):
        return f"{self._timer_ns}:{task_id}"

    def timesheets(self, task_id):
        return self.client.search_read(
            "account.analytic.line", [("task_id", "=", task_id)],
            ["date", "employee_id", "name", "unit_amount"], order="date, id",
        )

    # --- formatting -------------------------------------------------------- #

    def format_task(self, task, with_lines=False):
        out = [
            f"#{task['id']} {task['name']}",
            f"  etapa={m2o_name(task['stage_id'])}  inicio={utc_to_local(task['planned_date_begin'], self.tz)}"
            f"  limite={utc_to_local(task['date_deadline'], self.tz)}"
            f"  asignado={hours_to_hhmm(task['allocated_hours'])}  registrado={hours_to_hhmm(task['effective_hours'])}"
            f"  cronometro={self._timer_label(task)}",
        ]
        description = html_to_text(task.get("description"), 300)
        if description:
            out.append(f"  desc: {description}")
        if with_lines:
            for line in self.timesheets(task["id"]):
                out.append(f"  - {line['date']} {m2o_name(line['employee_id'])} {hours_to_hhmm(line['unit_amount'])} {line['name']}")
        return "\n".join(out)

    def _timer_label(self, task):
        if not task["timer_start"]:
            return "detenido"
        source = " (local)" if task.get("timer_source") == "local" else ""
        return f"CORRIENDO{source} desde {utc_to_local(task['timer_start'], self.tz)}"

    # --- task board -------------------------------------------------------- #

    def task_new(self, title, apply):
        vals = {
            "name": title.strip(),
            "project_id": self.tasks_cfg["project_id"],
            "stage_id": self.stage_id(self.tasks_cfg["stage_new"]),
        }
        if not apply:
            return False, f"PREVIEW crear tarjeta '{vals['name']}' en '{self.tasks_cfg['stage_new']}' (proyecto {vals['project_id']})."
        task_id = self.client.create("project.task", vals)
        return True, f"Creada tarjeta #{task_id} '{vals['name']}' en '{self.tasks_cfg['stage_new']}'."

    def task_plan(self, task_id, deadline, start, hours, description, apply):
        task = self.task(task_id)
        deadline_utc = local_to_utc(deadline, self.tz) if deadline else None
        start_utc = local_to_utc(start, self.tz) if start else None
        if start_utc and not (deadline_utc or task["date_deadline"]):
            raise ApiError("Fecha de inicio requiere Fecha límite (Odoo borra el inicio si no hay límite).")
        if start_utc and deadline_utc and start_utc > deadline_utc:
            raise ApiError("La Fecha de inicio es posterior a la Fecha límite.")

        preview = [f"PREVIEW planear #{task_id} '{task['name']}':"]
        if description:
            preview.append(f"  descripción: {description}")
        if deadline:
            preview.append(f"  fecha límite: {deadline}")
        if start:
            preview.append(f"  fecha inicio: {start}")
        if hours:
            preview.append(f"  tiempo asignado: {hours}")
        if not apply:
            return False, "\n".join(preview)

        # Same order the board requires in the UI: deadline, then start date, then hours.
        # allocated_hours is recomputed from the dates (project_enterprise), so it goes last.
        if description:
            self.client.write("project.task", [task_id], {"description": text_to_html(description)})
        if deadline_utc:
            self.client.write("project.task", [task_id], {"date_deadline": deadline_utc})
        if start_utc:
            self.client.write("project.task", [task_id], {"planned_date_begin": start_utc})
        if hours:
            self.client.write("project.task", [task_id], {"allocated_hours": hhmm_to_hours(hours)})

        task = self.task(task_id)
        problems = []
        if deadline_utc and task["date_deadline"] != deadline_utc:
            problems.append("fecha límite no quedó guardada")
        if start_utc and task["planned_date_begin"] != start_utc:
            problems.append("fecha de inicio no quedó guardada")
        if hours and abs((task["allocated_hours"] or 0) - hhmm_to_hours(hours)) > 0.01:
            problems.append("tiempo asignado no quedó guardado")
        status = "VERIFICADO" if not problems else "REVISAR: " + "; ".join(problems)
        return not problems, f"{status}\n{self.format_task(task)}"

    def task_start(self, task_id, apply):
        task = self.task(task_id)
        in_progress = self.tasks_cfg["stage_in_progress"]
        if task["timer_start"]:
            return True, f"El cronómetro de #{task_id} ya está corriendo desde {utc_to_local(task['timer_start'], self.tz)}."
        use_odoo_timer = bool(task.get("display_timesheet_timer"))
        if not apply:
            how = ("cronómetro de Odoo; si hay otro cronómetro del usuario corriendo, Odoo lo detiene primero"
                   if use_odoo_timer else
                   "cronómetro LOCAL (Odoo no muestra el cronómetro a este usuario de API); al detenerlo se crea "
                   f"la línea de Registro de horas con Empleado = {self.tasks_cfg['employee_name']}")
            return False, f"PREVIEW iniciar #{task_id} '{task['name']}' y pasarla a '{in_progress}' ({how})."
        if m2o_name(task["stage_id"]) != in_progress:
            self.client.write("project.task", [task_id], {"stage_id": self.stage_id(in_progress)})
        if not use_odoo_timer:
            self.timers.set(self._timer_key(task_id), self._now().strftime(ODOO_DT))
            return True, "Cronómetro local iniciado.\n" + self.format_task(self.task(task_id))
        self.client.call("project.task", "action_timer_start", ids=[task_id])
        task = self.task(task_id)
        ok = bool(task["timer_start"])
        return ok, ("Cronómetro iniciado." if ok else "REVISAR: Odoo no inició el cronómetro (¿el proyecto permite hojas de horas?).") + "\n" + self.format_task(task)

    def task_stop(self, task_id, description, discard, apply):
        task = self.task(task_id)
        if not task["timer_start"]:
            return False, f"El cronómetro de #{task_id} no está corriendo."
        if not discard and not (description or "").strip():
            raise ApiError("--desc es obligatorio para registrar tiempo (describe honestamente lo hecho).")
        action = "DESCARTAR el tiempo" if discard else f"registrar el tiempo con descripción: {description}"
        if not apply:
            return False, (f"PREVIEW detener cronómetro de #{task_id} (corre desde "
                           f"{utc_to_local(task['timer_start'], self.tz)}) y {action}; "
                           f"Empleado = {self.tasks_cfg['employee_name']}.")

        if task["timer_source"] == "local":
            return self._stop_local_timer(task, description, discard)

        # Same path as the UI: action_timer_stop returns the "Confirmar el tiempo utilizado"
        # wizard; creating it computes the rounded time spent, then save/delete is applied.
        result = self.client.call("project.task", "action_timer_stop", ids=[task_id])
        timesheet_id = (result or {}).get("context", {}).get("default_timesheet_id") if isinstance(result, dict) else None
        if not timesheet_id:
            return False, f"REVISAR: Odoo no devolvió el asistente de confirmación (respuesta: {result!r})."
        wizard_id = self.client.create(
            "hr.timesheet.stop.timer.confirmation.wizard",
            {"timesheet_name": description or "/"},
            context={"default_timesheet_id": timesheet_id},
        )
        method = "action_delete_timesheet" if discard else "action_save_timesheet"
        self.client.call("hr.timesheet.stop.timer.confirmation.wizard", method, ids=[wizard_id])
        if discard:
            return True, f"Tiempo descartado.\n{self.format_task(self.task(task_id))}"

        employee_id = self.employee_id()
        line = self.client.read("account.analytic.line", [timesheet_id], ["employee_id"])[0]
        if not line["employee_id"] or line["employee_id"][0] != employee_id:
            self.client.write("account.analytic.line", [timesheet_id], {"employee_id": employee_id})
        return True, f"Tiempo registrado.\n{self.format_task(self.task(task_id), with_lines=True)}"

    def _stop_local_timer(self, task, description, discard):
        """Close a LocalTimerStore timer: create the timesheet line directly (or discard it)."""
        key = self._timer_key(task["id"])
        if discard:
            self.timers.pop(key)
            return True, f"Tiempo descartado.\n{self.format_task(self.task(task['id']))}"
        started = datetime.strptime(task["timer_start"], ODOO_DT)
        minutes = max(1, math.ceil((self._now() - started).total_seconds() / 60))
        local_date = (self._now() + timedelta(hours=self.tz)).strftime("%Y-%m-%d")
        self.client.create("account.analytic.line", {
            "name": description.strip(),
            "project_id": self.tasks_cfg["project_id"],
            "task_id": task["id"],
            "employee_id": self.employee_id(),
            "unit_amount": round(minutes / 60, 4),
            "date": local_date,
        })
        # Removed only after Odoo accepted the line: a failed create keeps the timer to retry.
        self.timers.pop(key)
        return True, (f"Tiempo registrado ({hours_to_hhmm(minutes / 60)}).\n"
                      f"{self.format_task(self.task(task['id']), with_lines=True)}")

    def task_done(self, task_id, apply):
        task = self.task(task_id)
        done = self.tasks_cfg["stage_done"]
        employee_id = self.employee_id()
        lines = self.timesheets(task_id)
        wrong = [l for l in lines if not l["employee_id"] or l["employee_id"][0] != employee_id]
        warnings = []
        if task["timer_start"]:
            warnings.append("el cronómetro sigue corriendo: detenlo primero con 'task stop'")
        if not lines:
            warnings.append("no hay registro de horas")
        if not task["date_deadline"] or not task["planned_date_begin"]:
            warnings.append("faltan fechas (límite/inicio)")
        if task["timer_start"]:
            return False, "No se puede finalizar: " + "; ".join(warnings)
        if not apply:
            fix = f"corregir Empleado en {len(wrong)} línea(s) a {self.tasks_cfg['employee_name']}; " if wrong else ""
            note = f" AVISOS: {'; '.join(warnings)}." if warnings else ""
            return False, f"PREVIEW finalizar #{task_id} '{task['name']}': {fix}mover a '{done}'.{note}"
        if wrong:
            self.client.write("account.analytic.line", [l["id"] for l in wrong], {"employee_id": employee_id})
        self.client.write("project.task", [task_id], {"stage_id": self.stage_id(done)})
        return True, f"Finalizada.\n{self.format_task(self.task(task_id), with_lines=True)}"

    def task_list(self, stage, limit):
        domain = [("project_id", "=", self.tasks_cfg["project_id"])]
        if stage:
            domain.append(("stage_id.name", "=", stage))
        rows = self.client.search_read("project.task", domain, ["name", "stage_id", "date_deadline", "timer_start"],
                                       limit=limit, order="id desc")
        return "\n".join(
            f"#{r['id']} [{m2o_name(r['stage_id'])}] {r['name']}  limite={utc_to_local(r['date_deadline'], self.tz)}"
            f"{'  ⏱' if r['timer_start'] else ''}" for r in rows
        ) or "Sin tarjetas."

    # --- helpdesk (read-only) ---------------------------------------------- #

    def _team_domain(self):
        return [("team_id.name", "=", self.help_cfg["team_name"])]

    def _format_ticket_row(self, t):
        return (f"#{t['id']:05d} [{m2o_name(t['stage_id'])}] {t['name']} | {m2o_name(t['partner_id'])}"
                f" | resp={m2o_name(t['user_id'])} | act={utc_to_local(t['write_date'], self.tz)}")

    def ticket_n3(self):
        rows = self.client.search_read(
            "helpdesk.ticket", self._team_domain() + [("stage_id.name", "=", self.help_cfg["stage_n3"])],
            ["name", "stage_id", "partner_id", "user_id", "write_date"], order="write_date desc",
        )
        header = f"{len(rows)} ticket(s) en '{self.help_cfg['stage_n3']}':"
        return "\n".join([header] + [self._format_ticket_row(t) for t in rows])

    def ticket_show(self, ticket_id, full):
        rows = self.client.read("helpdesk.ticket", [ticket_id],
                                ["name", "stage_id", "partner_id", "user_id", "write_date", "create_date", "description"])
        if not rows:
            raise ApiError(f"El ticket {ticket_id} no existe o no es visible.")
        t = rows[0]
        limit = None if full else self.help_cfg["message_preview_chars"]
        out = [self._format_ticket_row(t), f"  creado={utc_to_local(t['create_date'], self.tz)}"]
        description = html_to_text(t.get("description"), limit)
        if description:
            out.append(f"  descripción: {description}")
        messages = self.client.search_read(
            "mail.message",
            [("model", "=", "helpdesk.ticket"), ("res_id", "=", ticket_id), ("message_type", "in", ["comment", "email"])],
            ["date", "author_id", "subtype_id", "body"], order="date asc",
        )
        for m in messages:
            out.append(f"  [{utc_to_local(m['date'], self.tz)}] {m2o_name(m['author_id'])} ({m2o_name(m['subtype_id'])}): "
                       f"{html_to_text(m['body'], limit)}")
        return "\n".join(out)

    def ticket_news(self, since):
        since_utc = local_to_utc(since if " " in since else since + " 00:00", self.tz)
        # A client message = author without an internal (non-share) user. Robust to new staff.
        messages = self.client.search_read(
            "mail.message",
            [("model", "=", "helpdesk.ticket"), ("message_type", "in", ["comment", "email"]),
             ("date", ">=", since_utc),
             "|", ("author_id.user_ids", "=", False), ("author_id.user_ids.share", "=", True)],
            ["date", "author_id", "res_id", "body"], order="date asc",
        )
        if not messages:
            return f"Sin mensajes de clientes desde {since}."
        tickets = {t["id"]: t for t in self.client.search_read(
            "helpdesk.ticket", self._team_domain() + [("id", "in", list({m["res_id"] for m in messages}))],
            ["name", "stage_id", "partner_id", "user_id", "write_date"])}
        out = []
        for m in messages:
            t = tickets.get(m["res_id"])
            if not t:
                continue  # other helpdesk teams are out of scope
            out.append(f"#{t['id']:05d} [{m2o_name(t['stage_id'])}] {t['name']}\n"
                       f"  [{utc_to_local(m['date'], self.tz)}] {m2o_name(m['author_id'])}: "
                       f"{html_to_text(m['body'], self.help_cfg['message_preview_chars'])}")
        return "\n".join(out) or f"Sin mensajes de clientes del equipo desde {since}."

    def ticket_sla(self):
        cutoff = (datetime.utcnow() - timedelta(days=1)).strftime(ODOO_DT)
        rows = self.client.search_read(
            "helpdesk.ticket",
            self._team_domain() + [("stage_id.name", "in", self.help_cfg["first_response_stages"]),
                                   ("create_date", "<", cutoff)],
            ["name", "stage_id", "partner_id", "user_id", "write_date", "create_date"], order="create_date asc",
        )
        header = f"{len(rows)} ticket(s) en {self.help_cfg['first_response_stages']} con más de 24 h (alerta N1):"
        return "\n".join([header] + [self._format_ticket_row(t) + f" | creado={utc_to_local(t['create_date'], self.tz)}"
                                     for t in rows])
