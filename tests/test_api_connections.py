"""
API connections: manager (api_connections.json + secrets), connectors, Realnet
board service and CLI previews. Offline: fake transport, fake secret store, temp dir.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_cli  # noqa: E402
from core.api.connections import ApiConnectionManager  # noqa: E402
from core.api.connectors import ApiError, OdooJson2Connector, RestBearerConnector  # noqa: E402
from core.api.realnet_board import (  # noqa: E402
    RealnetBoard, hhmm_to_hours, hours_to_hhmm, html_to_text, local_to_utc, utc_to_local,
)


class MemorySecrets:
    def __init__(self):
        self.data = {}

    def get(self, name):
        return self.data.get(name)

    def set(self, name, user, secret):
        self.data[name] = secret

    def delete(self, name):
        self.data.pop(name, None)

    def has(self, name):
        return name in self.data


class MemoryTimers:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def pop(self, key):
        return self.data.pop(key, None)


ODOO = {"name": "realnet", "type": "odoo_json2", "base_url": "https://x.test/", "database": "db1", "user": "u@x"}


CONFIG = {
    "utc_offset_hours": -5,
    "tasks": {"project_id": 50, "employee_name": "LÓPEZ POSADA, JUAN ESTEBAN",
              "stage_new": "Nuevo", "stage_in_progress": "En proceso", "stage_done": "Finalizado"},
    "helpdesk": {"team_name": "SOPORTE ODOO - PRODUCCION", "stage_n3": "Escalado N3 - Desarrollo",
                 "first_response_stages": ["Nuevo"], "message_preview_chars": 50},
}
STAGES = {"Nuevo": 1, "En proceso": 2, "Finalizado": 3}
EMPLOYEE = 77


class FakeClient:
    """Records every call; emulates just enough of the ORM for the board logic."""

    def __init__(self):
        self.calls = []
        self.task = {"id": 10, "name": "T", "project_id": [50, "P"], "stage_id": [1, "Nuevo"],
                     "date_deadline": False, "planned_date_begin": False, "allocated_hours": 0,
                     "effective_hours": 0, "timer_start": False, "user_ids": [], "description": ""}
        self.lines = []

    def search_read(self, model, domain, fields, limit=None, order=None, context=None):
        self.calls.append(("search_read", model))
        if model == "project.task.type":
            return [{"id": STAGES[domain[1][2]]}]
        if model == "hr.employee":
            return [{"id": EMPLOYEE}]
        if model == "account.analytic.line":
            return self.lines
        return []

    def read(self, model, ids, fields):
        self.calls.append(("read", model))
        if model == "project.task":
            return [dict(self.task)]
        return [{"id": ids[0], "employee_id": [5, "David"]}]

    def create(self, model, vals, context=None):
        self.calls.append(("create", model, vals, context))
        return 99

    def write(self, model, ids, vals):
        self.calls.append(("write", model, ids, vals))
        if model == "project.task":
            self.task.update(vals)
        return True

    def call(self, model, method, **params):
        self.calls.append(("call", model, method, params))
        if method == "action_timer_stop":
            return {"res_model": "hr.timesheet.stop.timer.confirmation.wizard",
                    "context": {"default_timesheet_id": 555}}
        return True

    def writes(self):
        return [c for c in self.calls if c[0] in ("write", "create", "call")]


class HelperTests(unittest.TestCase):
    def test_timezone_roundtrip(self):
        self.assertEqual(local_to_utc("2026-10-14 09:00", -5), "2026-10-14 14:00:00")
        self.assertEqual(utc_to_local("2026-10-14 14:00:00", -5), "2026-10-14 09:00")
        self.assertEqual(utc_to_local(False, -5), "-")

    def test_hours(self):
        self.assertEqual(hhmm_to_hours("02:30"), 2.5)
        self.assertEqual(hhmm_to_hours("1.5"), 1.5)
        self.assertEqual(hours_to_hhmm(1.75), "01:45")

    def test_html_to_text(self):
        self.assertEqual(html_to_text("<p>Hola &amp; chao</p><p>dos</p>"), "Hola & chao\ndos")
        self.assertTrue(html_to_text("x" * 100, 10).endswith("[…]"))


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.timers = MemoryTimers()
        self.clock = [datetime(2026, 10, 14, 14, 0, 0)]
        self.board = RealnetBoard(self.client, CONFIG, timer_store=self.timers, now=lambda: self.clock[0])

    def test_local_timer_when_odoo_hides_it(self):
        # display_timesheet_timer False (shared API user without employee): local timer, no Odoo call.
        ok, text = self.board.task_start(10, apply=True)
        self.assertTrue(ok, text)
        self.assertIn("(local)", text)
        self.assertEqual(self.timers.data, {"default:10": "2026-10-14 14:00:00"})
        self.assertNotIn("action_timer_start", [c[2] for c in self.client.calls if c[0] == "call"])

        self.clock[0] = datetime(2026, 10, 14, 15, 30, 20)  # 90 min 20 s -> rounded up to 91 min
        ok, text = self.board.task_stop(10, "Trabajo real", discard=False, apply=True)
        self.assertTrue(ok, text)
        line = next(c for c in self.client.calls if c[0] == "create")
        self.assertEqual(line[1], "account.analytic.line")
        self.assertEqual(line[2], {"name": "Trabajo real", "project_id": 50, "task_id": 10,
                                   "employee_id": EMPLOYEE, "unit_amount": round(91 / 60, 4), "date": "2026-10-14"})
        self.assertEqual(self.timers.data, {})

    def test_local_timer_discard_creates_nothing(self):
        self.board.task_start(10, apply=True)
        self.board.task_stop(10, None, discard=True, apply=True)
        self.assertEqual(self.timers.data, {})
        self.assertFalse([c for c in self.client.calls if c[0] == "create"])

    def test_odoo_timer_used_when_visible(self):
        self.client.task["display_timesheet_timer"] = True
        self.board.task_start(10, apply=True)
        self.assertIn("action_timer_start", [c[2] for c in self.client.calls if c[0] == "call"])
        self.assertEqual(self.timers.data, {})

    def test_preview_never_writes(self):
        ok, text = self.board.task_new("Titulo", apply=False)
        self.assertTrue(text.startswith("PREVIEW"))
        self.board.task_plan(10, "2026-10-14 17:00", "2026-10-14 09:00", "02:00", "d", apply=False)
        self.board.task_start(10, apply=False)
        self.board.task_done(10, apply=False)
        self.assertEqual(self.client.writes(), [])

    def test_plan_writes_deadline_before_start_and_hours_last(self):
        ok, text = self.board.task_plan(10, "2026-10-14 17:00", "2026-10-14 09:00", "02:00", None, apply=True)
        written = [list(c[3].keys())[0] for c in self.client.calls if c[0] == "write"]
        self.assertEqual(written, ["date_deadline", "planned_date_begin", "allocated_hours"])
        self.assertTrue(ok, text)

    def test_plan_rejects_start_after_deadline(self):
        with self.assertRaises(ApiError):
            self.board.task_plan(10, "2026-10-14 08:00", "2026-10-14 09:00", None, None, apply=True)

    def test_stop_uses_confirmation_wizard_and_fixes_employee(self):
        self.client.task["timer_start"] = "2026-10-14 14:00:00"
        ok, _ = self.board.task_stop(10, "Trabajo real", discard=False, apply=True)
        self.assertTrue(ok)
        create = next(c for c in self.client.calls if c[0] == "create")
        self.assertEqual(create[1], "hr.timesheet.stop.timer.confirmation.wizard")
        self.assertEqual(create[3], {"default_timesheet_id": 555})
        self.assertIn(("call", "hr.timesheet.stop.timer.confirmation.wizard", "action_save_timesheet", {"ids": [99]}),
                      self.client.calls)
        self.assertIn(("write", "account.analytic.line", [555], {"employee_id": EMPLOYEE}), self.client.calls)

    def test_stop_requires_description(self):
        self.client.task["timer_start"] = "2026-10-14 14:00:00"
        with self.assertRaises(ApiError):
            self.board.task_stop(10, "", discard=False, apply=True)

    def test_done_blocks_running_timer(self):
        self.client.task["timer_start"] = "2026-10-14 14:00:00"
        ok, text = self.board.task_done(10, apply=True)
        self.assertFalse(ok)
        self.assertEqual(self.client.writes(), [])

    def test_done_fixes_wrong_employee_then_moves_stage(self):
        self.client.lines = [{"id": 1, "date": "2026-10-14", "employee_id": [5, "David"], "name": "x", "unit_amount": 1}]
        ok, _ = self.board.task_done(10, apply=True)
        writes = [c for c in self.client.calls if c[0] == "write"]
        self.assertEqual(writes[0], ("write", "account.analytic.line", [1], {"employee_id": EMPLOYEE}))
        self.assertEqual(writes[1], ("write", "project.task", [10], {"stage_id": STAGES["Finalizado"]}))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "api_connections.json"
        self.secrets = MemorySecrets()
        self.manager = ApiConnectionManager(self.path, self.secrets)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_never_writes_secret_to_json(self):
        self.manager.save(dict(ODOO))
        self.manager.set_secret("realnet", "SUPERSECRET")
        self.assertNotIn("SUPERSECRET", self.path.read_text(encoding="utf-8"))
        self.assertTrue(self.manager.has_secret("realnet"))

    def test_validation(self):
        for bad in ({**ODOO, "name": "con espacio"}, {**ODOO, "type": "nope"},
                    {**ODOO, "base_url": "http://realnet.com.co"}, {**ODOO, "database": ""}):
            with self.assertRaises(ValueError):
                self.manager.save(bad)
        self.manager.save({**ODOO, "base_url": "http://localhost:8070"})

    def test_rename_moves_secret_and_rejects_clash(self):
        self.manager.save(dict(ODOO))
        self.manager.save({**ODOO, "name": "otro"})
        self.manager.set_secret("realnet", "K")
        with self.assertRaises(ValueError):
            self.manager.save({**ODOO, "name": "otro"}, original_name="realnet")
        self.manager.save({**ODOO, "name": "realnet2"}, original_name="realnet")
        self.assertEqual(self.secrets.data, {"realnet2": "K"})
        self.assertEqual(sorted(c["name"] for c in self.manager.list()), ["otro", "realnet2"])

    def test_delete_removes_secret(self):
        self.manager.save(dict(ODOO))
        self.manager.set_secret("realnet", "K")
        self.manager.delete("realnet")
        self.assertEqual(self.manager.list(), [])
        self.assertFalse(self.manager.has_secret("realnet"))

    def test_reload_when_other_process_writes(self):
        self.manager.save(dict(ODOO))
        other = ApiConnectionManager(self.path, self.secrets)
        other.save({**ODOO, "name": "nuevo"})
        os.utime(self.path, (1, 1))  # force a different mtime even on coarse clocks
        self.assertEqual(len(self.manager.list()), 2)

    def test_connector_requires_secret(self):
        self.manager.save(dict(ODOO))
        with self.assertRaisesRegex(ApiError, "clave"):
            self.manager.connector("realnet")
        self.manager.set_secret("realnet", "K")
        self.assertIsInstance(self.manager.connector("realnet", transport=lambda *a: (200, b"1")), OdooJson2Connector)


class ConnectorTests(unittest.TestCase):
    def test_odoo_headers_and_body(self):
        seen = {}

        def transport(method, url, headers, body):
            seen.update(method=method, url=url, headers=headers, body=json.loads(body))
            return 200, b"[1, 2]"

        connector = OdooJson2Connector(ODOO, "KEY", transport=transport)
        self.assertEqual(connector.call("res.partner", "search", domain=[]), [1, 2])
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], "https://x.test/json/2/res.partner/search")
        self.assertEqual(seen["headers"]["Authorization"], "bearer KEY")
        self.assertEqual(seen["headers"]["X-Odoo-Database"], "db1")
        self.assertEqual(seen["body"], {"domain": []})

    def test_odoo_error_message(self):
        connector = OdooJson2Connector(ODOO, "KEY", transport=lambda *a: (401, b'{"message": "Invalid apikey"}'))
        with self.assertRaisesRegex(ApiError, "res.partner.search -> HTTP 401: Invalid apikey"):
            connector.call("res.partner", "search", domain=[])

    def test_rest_bearer(self):
        seen = {}

        def transport(method, url, headers, body):
            seen.update(method=method, url=url, auth=headers["Authorization"], body=body)
            return 200, b'{"ok": true}'

        connector = RestBearerConnector({"name": "r", "type": "rest_bearer", "base_url": "https://api.test"},
                                        "T", transport=transport)
        self.assertEqual(connector.call("get", "/v1/items", query={"q": "a b"}), {"ok": True})
        self.assertEqual(seen, {"method": "GET", "url": "https://api.test/v1/items?q=a+b", "auth": "Bearer T", "body": None})


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = ApiConnectionManager(Path(self.tmp.name) / "c.json", MemorySecrets())

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = api_cli.main(list(argv), manager=self.manager)
        return code, out.getvalue()

    def test_add_with_board_defaults_and_list(self):
        code, _ = self.run_cli("conn", "add", "realnet", "--type", "odoo_json2", "--url", "https://x.test",
                               "--database", "db1", "--board-defaults")
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.get("realnet")["settings"]["tasks"]["project_id"], 50)
        code, out = self.run_cli("conn", "list")
        self.assertIn("clave=NO", out)

    def test_delete_is_preview_without_yes(self):
        self.manager.save(dict(ODOO))
        code, out = self.run_cli("conn", "delete", "realnet")
        self.assertEqual(code, api_cli.EXIT_PREVIEW)
        self.assertIsNotNone(self.manager.get("realnet"))

    def test_board_without_key_is_clean_error(self):
        self.manager.save(dict(ODOO))
        code, out = self.run_cli("board", "task", "list")
        self.assertEqual(code, 2)
        self.assertIn("no tiene clave", out)


if __name__ == "__main__":
    unittest.main()
