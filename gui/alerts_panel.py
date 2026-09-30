"""
"Configuración" page of the monitor: the background-monitor switch, the
monitoring thresholds and the e-mail account used to send alerts and the
daily digest (core/alerting.py, monitor_settings.json).
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from core import background_service
from core.alerting import email_is_configured, send_email
from core.instance_lock import SchedulerLock
from gui import theme

# How often the background-monitor status line is refreshed (registry + PID
# check, both instant). Keeps it truthful when the monitor is started or
# stopped from outside the window.
_BACKGROUND_REFRESH_MS = 4000

# SMTP preset for Google Workspace / Gmail accounts (realnet.com.co is hosted
# on Google). Requires an app password, not the account password.
_GMAIL_PRESET = {"host": "smtp.gmail.com", "port": "587", "security": "starttls"}

_SECURITY_LABELS = {"starttls": "STARTTLS (puerto 587)", "ssl": "SSL/TLS (puerto 465)", "none": "Sin cifrado"}

# (settings key, label, warn/crit pair caption) — one row per threshold pair.
_THRESHOLD_ROWS = (
    ("disk", "Disco usado (%)", "disk_warn_pct", "disk_crit_pct"),
    ("days_full", "Días estimados hasta llenarse", "days_to_full_warn", "days_to_full_crit"),
    ("fails", "Respaldos fallidos seguidos", "backup_fail_warn", "backup_fail_crit"),
    ("stale", "Días sin respaldo correcto", "days_without_backup_warn", "days_without_backup_crit"),
)


class AlertsPanel(ttk.Frame):
    """
    Args:
        parent:    Scrollable page container.
        monitor:   core.monitor.HealthMonitor (owns MonitorSettings).
        ui_queue:  The app's thread->GUI queue (for the test e-mail result).
        on_saved:  Called after a successful save so other views repaint.
    """

    def __init__(self, parent: tk.Widget, monitor, ui_queue: queue.Queue,
                 on_saved: Callable[[], None]) -> None:
        super().__init__(parent)
        self._monitor = monitor
        self._q = ui_queue
        self._on_saved = on_saved
        self.columnconfigure(0, weight=1)

        self._v_enabled = tk.BooleanVar()
        self._v_interval = tk.StringVar()
        self._v_thresholds: dict[str, tk.StringVar] = {}
        self._v_mail_enabled = tk.BooleanVar()
        self._v_host = tk.StringVar()
        self._v_port = tk.StringVar()
        self._v_security = tk.StringVar()
        self._v_user = tk.StringVar()
        self._v_password = tk.StringVar()
        self._v_sender = tk.StringVar()
        self._v_recipients = tk.StringVar()
        self._v_digest = tk.BooleanVar()
        self._v_digest_hour = tk.StringVar()
        self._v_reminder = tk.StringVar()

        self._background_busy = False

        self._build_background()
        self._build_monitoring()
        self._build_thresholds()
        self._build_email()
        self._build_actions()
        self.load()
        self._refresh_background_status()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build_background(self) -> None:
        """Switch for the headless monitor (core/background_service.py)."""
        box = ttk.LabelFrame(self, text="Monitor en segundo plano", padding=theme.PAD)
        box.grid(row=0, column=0, sticky="ew", pady=(0, theme.PAD))
        box.columnconfigure(0, weight=1)
        ttk.Label(
            box, wraplength=640, justify="left",
            text="Ejecuta los respaldos programados y el monitoreo de clientes aunque esta ventana esté "
                 "cerrada, y se inicia solo cada vez que inicia sesión en Windows. Mientras la ventana está "
                 "abierta, ella ejecuta el programador y el monitor espera; al cerrarla, el monitor lo asume.",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        self._lbl_background = tk.Label(box, text="", font=theme.FONT_BOLD, bg=theme.C_BG, anchor="w")
        self._lbl_background.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._btn_background_start = ttk.Button(box, text="Iniciar ahora", command=self._start_background)
        self._btn_background_start.grid(row=1, column=1, padx=(8, 0), pady=(8, 0))
        self._btn_background = ttk.Button(box, text="Activar", style="Primary.TButton",
                                          command=self._toggle_background)
        self._btn_background.grid(row=1, column=2, padx=(8, 0), pady=(8, 0))

    def _build_monitoring(self) -> None:
        box = ttk.LabelFrame(self, text="Monitoreo de clientes", padding=theme.PAD)
        box.grid(row=1, column=0, sticky="ew", pady=(0, theme.PAD))
        ttk.Checkbutton(box, text="Sondear automáticamente los servidores con respaldo programado",
                        variable=self._v_enabled).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(box, text="Sondear cada").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(box, textvariable=self._v_interval, width=5, justify="center").grid(row=1, column=1, padx=6, pady=(6, 0))
        ttk.Label(box, text="horas. El sondeo es de solo lectura (disco, PostgreSQL, Odoo).").grid(
            row=1, column=2, sticky="w", pady=(6, 0))

    def _build_thresholds(self) -> None:
        box = ttk.LabelFrame(self, text="Umbrales", padding=theme.PAD)
        box.grid(row=2, column=0, sticky="ew", pady=(0, theme.PAD))
        ttk.Label(box, text="Alerta", font=theme.FONT_BOLD, foreground=theme.LEVEL_COLORS["warn"][0]).grid(row=0, column=1, padx=8)
        ttk.Label(box, text="Crítico", font=theme.FONT_BOLD, foreground=theme.LEVEL_COLORS["crit"][0]).grid(row=0, column=2, padx=8)
        for row, (_key, label, warn_key, crit_key) in enumerate(_THRESHOLD_ROWS, start=1):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", pady=2)
            for column, key in ((1, warn_key), (2, crit_key)):
                var = tk.StringVar()
                self._v_thresholds[key] = var
                ttk.Entry(box, textvariable=var, width=6, justify="center").grid(row=row, column=column, padx=8, pady=2)
        ttk.Label(
            box, foreground=theme.C_MUTED,
            text="Un único respaldo fallido se muestra en el panel pero no genera alerta: suele ser transitorio.",
        ).grid(row=len(_THRESHOLD_ROWS) + 1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_email(self) -> None:
        box = ttk.LabelFrame(self, text="Correo de alertas", padding=theme.PAD)
        box.grid(row=3, column=0, sticky="ew", pady=(0, theme.PAD))
        box.columnconfigure(1, weight=1)
        ttk.Checkbutton(box, text="Enviar alertas y resumen diario por correo",
                        variable=self._v_mail_enabled).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Button(box, text="Usar Google Workspace (Gmail)", command=self._apply_gmail_preset).grid(
            row=0, column=3, sticky="e")

        fields = (
            ("Servidor SMTP:", self._v_host, 1, 0, 34, ""),
            ("Puerto:", self._v_port, 1, 2, 6, ""),
            ("Usuario:", self._v_user, 3, 0, 34, ""),
            ("Contraseña:", self._v_password, 3, 2, 22, "*"),
            ("Remitente:", self._v_sender, 4, 0, 34, ""),
        )
        for label, var, row, column, width, show in fields:
            ttk.Label(box, text=label).grid(row=row, column=column, sticky="w", pady=3, padx=(0 if column == 0 else 12, 6))
            ttk.Entry(box, textvariable=var, width=width, show=show).grid(row=row, column=column + 1, sticky="w", pady=3)

        ttk.Label(box, text="Seguridad:").grid(row=2, column=0, sticky="w", pady=3, padx=(0, 6))
        ttk.Combobox(box, textvariable=self._v_security, values=list(_SECURITY_LABELS.values()),
                     state="readonly", width=24).grid(row=2, column=1, sticky="w", pady=3)

        ttk.Label(box, text="Destinatarios:").grid(row=5, column=0, sticky="w", pady=3, padx=(0, 6))
        ttk.Entry(box, textvariable=self._v_recipients).grid(row=5, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Label(box, text="Separados por coma. Reciben cada cambio de estado y el resumen diario.",
                  foreground=theme.C_MUTED).grid(row=6, column=1, columnspan=3, sticky="w")

        digest = ttk.Frame(box)
        digest.grid(row=7, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Checkbutton(digest, text="Resumen diario a las", variable=self._v_digest).pack(side="left")
        ttk.Entry(digest, textvariable=self._v_digest_hour, width=4, justify="center").pack(side="left", padx=6)
        ttk.Label(digest, text="h.   Recordar lo crítico cada").pack(side="left")
        ttk.Entry(digest, textvariable=self._v_reminder, width=4, justify="center").pack(side="left", padx=6)
        ttk.Label(digest, text="horas.").pack(side="left")
        ttk.Label(
            box, foreground=theme.C_MUTED, wraplength=640, justify="left",
            text="Con Google Workspace o Gmail, la contraseña NO es la de la cuenta sino una «contraseña de "
                 "aplicación» de 16 caracteres (myaccount.google.com/apppasswords; requiere la verificación en "
                 "dos pasos activa). El resumen diario funciona también como señal de vida: si un día no llega, "
                 "el monitor no está en ejecución. La contraseña se guarda sin cifrar en este equipo, igual que "
                 "los perfiles de servidor.",
        ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(6, 0))

    def _build_actions(self) -> None:
        row = ttk.Frame(self)
        row.grid(row=4, column=0, sticky="w")
        ttk.Button(row, text="Guardar", style="Primary.TButton", command=self.save).pack(side="left")
        self._btn_test = ttk.Button(row, text="Enviar correo de prueba", command=self._send_test)
        self._btn_test.pack(side="left", padx=8)
        self._lbl_result = ttk.Label(row, text="", foreground=theme.C_MUTED)
        self._lbl_result.pack(side="left")

    # ── Load / save ──────────────────────────────────────────────────────────

    def load(self) -> None:
        """Fill the form from monitor_settings.json."""
        def fmt(value) -> str:
            # 80.0 -> "80", 0.5 -> "0,5" (values are stored as floats).
            number = float(value)
            return str(int(number)) if number.is_integer() else str(number).replace(".", ",")

        data = self._monitor.settings.get()
        email = data["email"]
        self._v_enabled.set(bool(data.get("enabled", True)))
        self._v_interval.set(fmt(data.get("probe_interval_hours", 6)))
        for key, var in self._v_thresholds.items():
            var.set(fmt(data["thresholds"][key]))
        self._v_mail_enabled.set(bool(email.get("enabled")))
        self._v_host.set(email.get("host", ""))
        self._v_port.set(str(email.get("port", 587)))
        self._v_security.set(_SECURITY_LABELS.get(email.get("security", "starttls"), _SECURITY_LABELS["starttls"]))
        self._v_user.set(email.get("user", ""))
        self._v_password.set(email.get("password", ""))
        self._v_sender.set(email.get("sender", ""))
        self._v_recipients.set(", ".join(email.get("recipients", [])))
        self._v_digest.set(bool(data.get("digest_enabled", True)))
        self._v_digest_hour.set(fmt(data.get("digest_hour", 8)))
        self._v_reminder.set(fmt(data.get("reminder_hours", 24)))

    def _collect(self) -> dict:
        """
        Read and validate the form.

        Raises:
            ValueError: With a user-facing message naming the invalid field.
        """
        def number(var: tk.StringVar, label: str, low: float, high: float) -> float:
            try:
                value = float(var.get().replace(",", "."))
            except ValueError:
                raise ValueError(f"«{label}» debe ser un número.") from None
            if not low <= value <= high:
                raise ValueError(f"«{label}» debe estar entre {low:g} y {high:g}.")
            return value

        data = self._monitor.settings.get()
        data["enabled"] = self._v_enabled.get()
        data["probe_interval_hours"] = number(self._v_interval, "Sondear cada", 0.25, 48)

        thresholds = data["thresholds"]
        for _key, label, warn_key, crit_key in _THRESHOLD_ROWS:
            thresholds[warn_key] = number(self._v_thresholds[warn_key], f"{label} (alerta)", 0, 10000)
            thresholds[crit_key] = number(self._v_thresholds[crit_key], f"{label} (crítico)", 0, 10000)
        # "Days until full" counts down, so critical must be the SMALLER
        # number there; everything else grows towards critical.
        if thresholds["days_to_full_crit"] > thresholds["days_to_full_warn"]:
            raise ValueError("En «Días estimados hasta llenarse», crítico debe ser menor o igual que alerta.")
        for _key, label, warn_key, crit_key in _THRESHOLD_ROWS:
            if warn_key != "days_to_full_warn" and thresholds[crit_key] < thresholds[warn_key]:
                raise ValueError(f"En «{label}», crítico debe ser mayor o igual que alerta.")

        security = next((k for k, v in _SECURITY_LABELS.items() if v == self._v_security.get()), "starttls")
        recipients = [r.strip() for r in self._v_recipients.get().replace(";", ",").split(",") if r.strip()]
        for address in recipients:
            if "@" not in address or " " in address:
                raise ValueError(f"Destinatario no válido: «{address}».")
        data["email"] = {
            "enabled": self._v_mail_enabled.get(),
            "host": self._v_host.get().strip(),
            "port": int(number(self._v_port, "Puerto", 1, 65535)),
            "security": security,
            "user": self._v_user.get().strip(),
            "password": self._v_password.get(),
            "sender": self._v_sender.get().strip(),
            "recipients": recipients,
        }
        if data["email"]["enabled"] and not email_is_configured(data["email"]):
            raise ValueError("Para activar el correo indique servidor SMTP, remitente (o usuario) y al menos un destinatario.")

        data["digest_enabled"] = self._v_digest.get()
        data["digest_hour"] = int(number(self._v_digest_hour, "Hora del resumen diario", 0, 23))
        data["reminder_hours"] = number(self._v_reminder, "Recordar lo crítico cada", 1, 168)
        return data

    def save(self) -> bool:
        """Validate and persist. Returns True on success."""
        try:
            data = self._collect()
        except ValueError as exc:
            messagebox.showwarning(theme.APP_TITLE, str(exc), parent=self)
            return False
        self._monitor.settings.save(data)
        self._lbl_result.config(text="Configuración guardada.", foreground=theme.LEVEL_COLORS["ok"][0])
        self._on_saved()
        return True

    def _send_test(self) -> None:
        """Send a test message with the values currently in the form (unsaved is fine)."""
        try:
            email_cfg = {**self._collect()["email"], "enabled": True}
        except ValueError as exc:
            messagebox.showwarning(theme.APP_TITLE, str(exc), parent=self)
            return
        self._btn_test.config(state="disabled")
        self._lbl_result.config(text="Enviando correo de prueba...", foreground=theme.C_MUTED)

        def _work() -> None:
            try:
                send_email(
                    email_cfg,
                    "[Respaldos Odoo] Correo de prueba",
                    "Este es un correo de prueba de Odoo Backup Tool.\n\n"
                    "Si lo recibe, las alertas de monitoreo y el resumen diario llegarán a esta dirección.",
                )
                outcome = (True, "Correo de prueba enviado. Revise la bandeja de entrada.")
            except Exception as exc:  # noqa: BLE001 — shown to the user verbatim
                outcome = (False, f"No se pudo enviar: {exc}")
            self._q.put(("ui_call", lambda: self._test_finished(*outcome)))
        threading.Thread(target=_work, name="alert-test-mail", daemon=True).start()

    def _apply_gmail_preset(self) -> None:
        """Fill server/port/security for Google Workspace; user and password stay the user's."""
        self._v_host.set(_GMAIL_PRESET["host"])
        self._v_port.set(_GMAIL_PRESET["port"])
        self._v_security.set(_SECURITY_LABELS[_GMAIL_PRESET["security"]])
        if not self._v_sender.get().strip() and self._v_user.get().strip():
            self._v_sender.set(self._v_user.get().strip())
        self._lbl_result.config(
            text="Servidor de Google cargado. Complete usuario, contraseña de aplicación y destinatarios.",
            foreground=theme.C_MUTED)

    # ── Background monitor switch ────────────────────────────────────────────

    def _refresh_background_status(self) -> None:
        """Repaint the switch from the registry and the running process (cheap)."""
        if not self._background_busy:
            try:
                enabled = background_service.is_enabled()
                pid = background_service.running_pid()
            except OSError:
                enabled, pid = False, None
            owner = SchedulerLock().owner_info()
            ok_fg, warn_fg, off_fg = (theme.LEVEL_COLORS["ok"][0], theme.LEVEL_COLORS["warn"][0],
                                      theme.LEVEL_COLORS["off"][0])
            if pid and owner.get("mode") == "headless" and owner.get("pid") == pid:
                text, color = f"● Activo: ejecuta los respaldos y el monitoreo (PID {pid}).", ok_fg
            elif pid:
                text, color = (f"● Activo, en espera: asumirá el programador al cerrar esta ventana (PID {pid}).",
                               ok_fg)
            elif enabled:
                text, color = "◐ Registrado al iniciar sesión, pero no está en ejecución ahora.", warn_fg
            else:
                text, color = ("○ Desactivado: los respaldos y el monitoreo solo corren con esta ventana abierta.",
                               off_fg)
            self._lbl_background.config(text=text, fg=color)
            self._btn_background.config(text="Desactivar" if enabled or pid else "Activar",
                                        style="TButton" if enabled or pid else "Primary.TButton")
            if enabled and not pid:
                self._btn_background_start.grid()
            else:
                self._btn_background_start.grid_remove()
        self.after(_BACKGROUND_REFRESH_MS, self._refresh_background_status)

    def _toggle_background(self) -> None:
        try:
            active = background_service.is_enabled() or background_service.running_pid()
        except OSError:
            active = False
        if active:
            owner = SchedulerLock().owner_info()
            runs_schedule = owner.get("mode") == "headless" and owner.get("pid") == background_service.running_pid()
            detail = (
                "\n\nAhora mismo está ejecutando el programador: si hay un respaldo en curso, se "
                "interrumpe su transferencia (lo que ya corre en el servidor continúa). Esta ventana "
                "asumirá el programador en menos de un minuto."
                if runs_schedule else ""
            )
            if not messagebox.askyesno(
                theme.APP_TITLE,
                "¿Desactivar el monitor en segundo plano?\n\nDejará de iniciarse con Windows y se "
                "detendrá ahora. Los respaldos y el monitoreo solo correrán con la aplicación abierta."
                + detail,
                parent=self,
            ):
                return
            self._run_background_action(background_service.disable, "Monitor en segundo plano desactivado.")
        else:
            self._run_background_action(
                background_service.enable,
                "Monitor en segundo plano activado: se iniciará con cada inicio de sesión de Windows.",
            )

    def _start_background(self) -> None:
        self._run_background_action(background_service.start_now, "Monitor en segundo plano iniciado.")

    def _run_background_action(self, action: Callable[[], object], success: str) -> None:
        """Run a switch action off the Tk thread (taskkill/Popen can take a moment)."""
        self._background_busy = True
        self._btn_background.config(state="disabled")
        self._btn_background_start.config(state="disabled")

        def _work() -> None:
            try:
                action()
                outcome = (True, success)
            except Exception as exc:  # noqa: BLE001 — shown to the user verbatim
                outcome = (False, f"No se pudo cambiar el monitor en segundo plano: {exc}")
            self._q.put(("ui_call", lambda: self._background_finished(*outcome)))
        threading.Thread(target=_work, name="background-switch", daemon=True).start()

    def _background_finished(self, ok: bool, message: str) -> None:
        self._background_busy = False
        self._btn_background.config(state="normal")
        self._btn_background_start.config(state="normal")
        self._lbl_result.config(text=message[:160], foreground=theme.LEVEL_COLORS["ok" if ok else "crit"][0])
        if not ok:
            messagebox.showerror(theme.APP_TITLE, message, parent=self)

    def _test_finished(self, ok: bool, message: str) -> None:
        self._btn_test.config(state="normal")
        self._lbl_result.config(text=message[:140], foreground=theme.LEVEL_COLORS["ok" if ok else "crit"][0])
        if not ok:
            messagebox.showerror(theme.APP_TITLE, message, parent=self)
