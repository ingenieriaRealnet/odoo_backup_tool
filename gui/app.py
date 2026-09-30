"""
Main application window for Odoo Backup Tool.

Implements a wizard with two sections:
  Backup  (tabs 1-5): connect, select DB, select filestore, destination, execute
  Restore (tab 6)  : upload/locate files, create DB, restore, neutralize

This module holds the window itself: construction, theme, status bar, log
panel, the thread->GUI queue and shutdown. Each page lives in a mixin under
gui/tabs/ and the dialogs in gui/dialogs.py.
"""
from __future__ import annotations
import datetime
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from core.pg_exec import PgTarget
from core.profiles import ProfileManager
from core.ssh_client import SSHClient
from core.version import APP_VERSION, GITHUB_REPO
from core.updater import check_for_update
from gui.help_window import HelpWindow
from core.scheduler import ScheduleManager, BackupScheduler
from core.temp_registry import RemoteTempRegistry, sweep_orphaned_files
from core.history_manager import HistoryManager
from core.instance_lock import SchedulerLock
from core.monitor import HealthMonitor
from gui import theme
from gui.sidebar import Sidebar
from gui.health_panel import HealthPanel
from gui.constants import (
    _KW_LOG_RE, APP_TITLE, _PAD, _FROZEN_ACTIVITY_THRESHOLD_SECS, _OWNERSHIP_RETRY_MS, _TAB_PANEL,
    _SIDEBAR_SECTIONS, _MIN_PAGES_FRACTION, _PROFILE_NEW, _SETTINGS_FILE, _DARK_BG, _LOG_BG,
    _LOG_FG, _C_BG, _C_PURPLE, _C_PURPLE2, _C_PURPLE3, _C_TEAL, _C_WHITE, _C_TEXT, _C_BORDER,
    _C_RED, _C_RED2,
)
from gui.dialogs import _OverwriteDialog
from gui.tabs.monitoring_pages import MonitoringPagesMixin
from gui.tabs.backup_wizard import BackupWizardMixin
from gui.tabs.restore import RestoreMixin
from gui.tabs.addons import AddonsMixin
from gui.tabs.server_tools import ServerToolsMixin
from gui.tabs.automation import AutomationMixin


class BackupApp(
    MonitoringPagesMixin,
    BackupWizardMixin,
    RestoreMixin,
    AddonsMixin,
    ServerToolsMixin,
    AutomationMixin,
):
    """Root window and controller for the Odoo Backup Tool."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._configure_window()
        self._apply_styles()

        # SSH client shared across backup tabs
        self._ssh = SSHClient()
        # SSH client for restore destination (used when target != backup origin)
        self._ssh_restore = SSHClient()
        # SSH client for the backup destination server (Step 4 "otro servidor remoto")
        self._ssh_dest = SSHClient()

        # Objetivo de PostgreSQL (bare-metal o contenedor Docker) para el
        # servidor de origen (Tab 2) y para el servidor usado en Tab Trial.
        # Se completan automaticamente con el perfil conectado y pueden
        # ajustarse manualmente en la UI ("Contenedor:" + "Detectar").
        self._pg_target_source = PgTarget()
        self._pg_target_trial  = PgTarget()
        self._docker_pg_users_source: dict[str, str] = {}
        self._docker_pg_users_trial:  dict[str, str] = {}

        # Persistent connection profiles (loaded from ~/.odoo_backup_tool/servers.json)
        self._profiles = ProfileManager()

        # Schedule rules manager (shared with BackupScheduler)
        self._sched_mgr = ScheduleManager()

        # Registry of remote /tmp files this tool has created but not yet
        # confirmed deleting — shared with BackupScheduler so both the
        # manual flow and scheduled runs write to the same on-disk manifest.
        # See core/temp_registry.py.
        self._temp_registry = RemoteTempRegistry()

        # Persistent, cross-restart record of every backup/restore/addons-sync
        # run — unlike self.log_widget (Tab log panel), which is cleared on
        # every app close by design. See core/history_manager.py and the
        # "Historial" tab. Shared with BackupScheduler so scheduled runs land
        # in the same on-disk log as manual ones.
        self._history = HistoryManager()
        # Buffers the currently in-progress operation's log lines/metadata so
        # they can be flushed to self._history once it finishes — see
        # _history_begin()/_history_end() below. Only one slot: manual
        # backup/restore/addons-sync are treated as mutually exclusive, same
        # simplification the single shared log_widget already makes.
        self._current_op_meta: dict | None = None
        self._current_op_log: list[str] = []

        # Tracks whichever scrollable-tab canvas the mouse is currently over
        # (set by _bind_mousewheel's Enter/Leave handlers) — used by
        # _on_combobox_scroll to redirect scroll-over-a-combobox to the page
        # instead of letting ttk cycle the combobox's own selected value.
        self._active_scroll_canvas: tk.Canvas | None = None

        # ── Backup state variables ────────────────────────────────────────
        self._v_db = tk.StringVar()
        self._v_dump_fmt = tk.StringVar(value="dump")
        self._v_docker_container = tk.StringVar()  # Tab 2: contenedor Postgres (vacio = bare-metal)
        self._v_docker_exec_user = tk.StringVar()  # Tab 2: usuario OS para 'docker exec -u' (auth peer, opcional)
        self._v_fs_root = tk.StringVar()
        self._v_fs_db = tk.StringVar()
        self._v_dest_type = tk.StringVar(value="local")
        self._v_local_dir = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Downloads")
        )
        # Google Drive destination fields
        self._v_gdrive_creds  = tk.StringVar()   # path to service account JSON
        self._v_gdrive_folder = tk.StringVar()   # Drive folder ID

        # Drive fields stored inside the server profile (Tab 1)
        self._v_prof_gdrive_creds  = tk.StringVar()
        self._v_prof_gdrive_folder = tk.StringVar()

        # Bundle options (Tab 5)
        self._v_bundle = tk.BooleanVar(value=True)

        # Restore-from-bundle state (Tab 6)
        self._v_r_restore_mode  = tk.StringVar(value="individual")   # "bundle" | "individual"
        self._v_r_bundle_local  = tk.StringVar(value="")
        self._v_r_bundle_src    = tk.StringVar(value="local")        # "local" | "server"
        self._v_r_bundle_srv    = tk.StringVar(value="")

        # ── Restore state variables ───────────────────────────────────────
        # 'origin' | 'dest' | 'other'
        self._v_r_conn_type  = tk.StringVar(value="origin")
        self._v_r_dump_src   = tk.StringVar(value="local") # 'local' | 'server'
        self._v_r_dump_local = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self._v_r_dump_srv   = tk.StringVar(value="/tmp/odoo_bancasa_prod.dump")
        self._v_r_fs_src     = tk.StringVar(value="local") # 'local' | 'server' | 'none'
        self._v_r_fs_local   = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self._v_r_fs_srv     = tk.StringVar(value="/tmp/filestore_bancasa_prod.tar")
        self._v_r_db_name    = tk.StringVar()
        self._v_r_fs_root    = tk.StringVar()
        self._v_r_jobs       = tk.StringVar(value="4")
        self._v_r_neutralize = tk.BooleanVar(value=False)
        self._v_r_conf       = tk.StringVar(value="/etc/odoo/odoo.conf")
        self._v_r_cleanup    = tk.BooleanVar(value=True)
        self._v_r_inventory  = tk.StringVar()   # path to companion inventory JSON

        # ── Addons sync state variables ───────────────────────────────────
        self._v_a_conn_type  = tk.StringVar(value="origin")
        self._v_a_repo_url   = tk.StringVar()
        self._v_a_branch     = tk.StringVar(value="main")
        self._v_a_target     = tk.StringVar(value="/usr/lib/python3/dist-packages/odoo/addons_custom")
        self._v_a_odoo_user  = tk.StringVar(value="odoo")
        self._v_a_restart      = tk.BooleanVar(value=False)
        self._v_a_service      = tk.StringVar(value="odoo")
        self._v_a_submodules   = tk.BooleanVar(value=False)  # use git submodule update sequence
        self._v_a_force_mirror = tk.BooleanVar(value=False)  # discard local drift, remote always wins
        # Database to run `-u <modulos_cambiados>` against after a sync that
        # touched module code — see _worker_addons. Binary/conf path are
        # resolved from the service above (AddonsManager.resolve_service_launcher),
        # not entered separately, so they can't drift out of sync with it.
        self._v_a_db_name      = tk.StringVar()

        # SSH key source priority cascade:
        #   "server"   — key already on the remote server's ~/.ssh/
        #   "local"    — key from this local machine (upload temporarily)
        #   "generate" — generate a new Ed25519 key on this machine
        self._v_a_key_source   = tk.StringVar(value="server")
        self._v_a_server_key   = tk.StringVar()    # remote path selected from scan
        self._v_a_ssh_key      = tk.StringVar()    # local key path (mode "local")
        self._v_a_gen_key_name = tk.StringVar(value="odoo_github_key")
        self._v_a_pub_key_text = tk.StringVar()    # display-only public key after generate

        # Per-session passphrase cache keyed by key path — NEVER written to disk.
        # Cleared automatically when the app closes.
        self._key_passphrases: dict[str, str] = {}
        # Same, for server-side keys (Tab 7 "server" mode) — keyed by
        # "{host}:{server_key_path}" since these are remote paths, not local
        # ones. See _action_sync_addons / AddonsManager.unlock_server_key.
        self._server_key_passphrases: dict[str, str] = {}

        # Cancellation flag shared between GUI and worker threads
        self._cancel_event = threading.Event()

        # Thread -> GUI message queue
        self._q: queue.Queue = queue.Queue()

        # Retry state — set when a transfer fails after files are ready on the server.
        # Allows re-running only the transfer step with same or different destination.
        self._retry_remote_tmp:  list[str]   = []
        self._retry_conn_params: dict        = {}
        self._retry_dump_path:   str         = ""
        self._retry_inventory:   dict | None = None

        # ── Scheduler ownership ───────────────────────────────────────────
        # Only one process may run the schedule (see core/instance_lock.py):
        # this window, or the headless monitor started by Windows at logon
        # (main.py --headless). When the other one holds the lock this
        # window is a viewer: it shows everything but does not fire rules,
        # sweep /tmp or send alerts. OBT_NO_SCHEDULER=1 (main.py
        # --no-scheduler) forces viewer mode without touching the lock.
        self._no_scheduler = os.environ.get("OBT_NO_SCHEDULER") == "1"
        self._sched_lock = SchedulerLock()
        self._scheduler_owner = (not self._no_scheduler) and self._sched_lock.acquire("gui")

        # Client health monitor (core/monitor.py): probes the servers that
        # have a scheduled rule, tracks consecutive backup failures and
        # raises alerts. Created before the UI because the "Panel" page
        # reads from it.
        self._monitor = HealthMonitor(
            self._sched_mgr, self._profiles,
            notify_queue=self._q, history=self._history,
            owner=self._scheduler_owner,
        )
        self._health_panel: HealthPanel | None = None

        self._build_ui()
        self._poll_queue()
        # First paint of the dashboard from stored data (no network).
        self._health_panel.request_refresh()

        # Kick off the background update check after the UI is ready.
        # The callback schedules the banner display on the main thread via root.after.
        check_for_update(GITHUB_REPO, APP_VERSION, self._on_update_check_result)

        # The backup scheduler daemon thread shares self._q so schedule
        # events land in the same poll loop. It is always constructed (other
        # code queries it, e.g. has_active_jobs()), but only started when
        # this window owns the scheduler.
        self._scheduler = BackupScheduler(
            self._sched_mgr, self._profiles, self._q,
            temp_registry=self._temp_registry,
            history=self._history,
            monitor=self._monitor,
        )
        if self._scheduler_owner:
            self._start_scheduler_services()
        elif not self._no_scheduler:
            # The headless monitor owns the schedule right now; take over if
            # it ever stops, so closing it never leaves nobody in charge.
            self.root.after(_OWNERSHIP_RETRY_MS, self._retry_scheduler_ownership)
        self._refresh_scheduler_owner_ui()
        # self._scheduler.start()
        # threading.Thread(target=self._sweep_orphaned_temp_files, daemon=True).start()

        # Timestamp of the last message drained from self._q (any kind — log,
        # progress, sched_log, ...), refreshed in _poll_queue(). Used by
        # _on_close() to tell a genuinely active backup/restore apart from
        # one that's actually stuck/frozen (no progress for a while) — see
        # _on_close() docstring.
        self._last_activity_ts = time.monotonic()

    # ── Scheduler ownership ───────────────────────────────────────────────

    def _start_scheduler_services(self) -> None:
        """Start everything only the scheduler owner may run."""
        self._sched_mgr.on_result = self._monitor.record_backup_result
        self._scheduler.start()

        # Sweep orphaned remote /tmp files left behind by a crash, a failed
        # job, or the app being force-closed last session — see
        # core/temp_registry.py. Runs in a background thread so a slow/
        # unreachable server never delays app startup. Owner-only: a viewer
        # sweeping would delete the files of a job the owner is still running.
        threading.Thread(target=self._sweep_orphaned_temp_files, daemon=True).start()

    def _retry_scheduler_ownership(self) -> None:
        """Periodically try to take over the schedule from a gone headless monitor."""
        if self._sched_lock.acquire("gui"):
            self._scheduler_owner = True
            self._monitor.become_owner(self._history)
            self._start_scheduler_services()
            self._refresh_scheduler_owner_ui()
            self._append_log("El monitor en segundo plano ya no está activo: esta ventana asume el programador.")
            return
        self.root.after(_OWNERSHIP_RETRY_MS, self._retry_scheduler_ownership)

    def _refresh_scheduler_owner_ui(self) -> None:
        """Reflect on the Automatización page who is running the schedule."""
        label = getattr(self, "_lbl_sched_status", None)
        if label is None:
            return
        if self._scheduler_owner:
            label.config(text="● Programador activo", foreground="#2E8B57")
            self._btn_sched_toggle.config(state="normal")
        elif self._no_scheduler:
            label.config(text="○ Programador desactivado en esta ventana (modo sin programador)", foreground="#888888")
            self._btn_sched_toggle.config(state="disabled")
        else:
            pid = self._sched_lock.owner_info().get("pid", "?")
            label.config(text=f"● Programador activo en el monitor en segundo plano (PID {pid})", foreground="#2E8B57")
            self._btn_sched_toggle.config(state="disabled")

    # ── Theme / Style ─────────────────────────────────────────────────────

    def _apply_styles(self) -> None:
        """
        Configure the ttk visual theme with Odoo brand colors.

        Uses 'clam' as base theme (most customizable cross-platform) and
        defines:
          - TButton            default action buttons
          - Primary.TButton    main CTA buttons (Conectar, Iniciar, etc.)
          - Stop.TButton       cancel/danger buttons (red)
          - TNotebook.Tab      purple when selected, neutral otherwise
          - TLabelframe        purple border + purple label
          - TProgressbar       teal fill bar
        """
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass  # Fallback to whatever theme is available

        self.root.configure(bg=_C_BG)

        # Base containers
        style.configure("TFrame",     background=_C_BG)
        style.configure("TPanedwindow", background=_C_BG)

        # Labels
        style.configure("TLabel", background=_C_BG, foreground=_C_TEXT, font=("Segoe UI", 9))
        style.configure("Bold.TLabel", background=_C_BG, foreground=_C_TEXT, font=("Segoe UI", 10, "bold"))

        # LabelFrame — purple border and title
        style.configure(
            "TLabelframe",
            background=_C_BG, bordercolor=_C_PURPLE,
            relief="groove", borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=_C_BG, foreground=_C_PURPLE,
            font=("Segoe UI", 9, "bold"),
        )

        # Default button
        style.configure(
            "TButton",
            background="#E6DEE3", foreground=_C_TEXT,
            bordercolor=_C_BORDER, focuscolor=_C_PURPLE,
            padding=(8, 4), font=("Segoe UI", 9), relief="flat",
        )
        style.map("TButton",
            background=[
                ("active",   "#D4C8D0"),
                ("pressed",  _C_BORDER),
                ("disabled", "#EAE5E8"),
            ],
            foreground=[("disabled", "#AAAAAA")],
        )

        # Primary action button
        style.configure(
            "Primary.TButton",
            background=_C_PURPLE, foreground=_C_WHITE,
            bordercolor=_C_PURPLE, padding=(12, 6),
            font=("Segoe UI", 10, "bold"), relief="flat",
        )
        style.map("Primary.TButton",
            background=[
                ("active",   _C_PURPLE2),
                ("pressed",  _C_PURPLE3),
                ("disabled", "#B099A8"),
            ],
            foreground=[
                ("active", _C_WHITE), ("pressed", _C_WHITE), ("disabled", "#DDDDDD"),
            ],
        )

        # Navigation / flow button  (Siguiente ->, back, step-advance)
        style.configure(
            "Nav.TButton",
            background=_C_TEAL, foreground=_C_WHITE,
            bordercolor=_C_TEAL, padding=(10, 5),
            font=("Segoe UI", 9, "bold"), relief="flat",
        )
        style.map("Nav.TButton",
            background=[
                ("active",   "#00B8B5"),
                ("pressed",  "#007A78"),
                ("disabled", "#90CECE"),
            ],
            foreground=[
                ("active", _C_WHITE), ("pressed", _C_WHITE), ("disabled", "#DDDDDD"),
            ],
        )

        # Stop / danger button
        style.configure(
            "Stop.TButton",
            background=_C_RED, foreground=_C_WHITE,
            bordercolor=_C_RED, padding=(10, 5),
            font=("Segoe UI", 9, "bold"), relief="flat",
        )
        style.map("Stop.TButton",
            background=[
                ("active",   _C_RED2),
                ("pressed",  "#922B21"),
                ("disabled", "#E0A89E"),
            ],
            foreground=[
                ("active", _C_WHITE), ("pressed", _C_WHITE), ("disabled", "#FFFFFF"),
            ],
        )

        # Main notebook: tab strip hidden, navigation is the sidebar
        # (gui/sidebar.py). An empty layout for the Tab element removes the
        # strip while the notebook keeps managing the pages.
        style.layout("Hidden.TNotebook.Tab", [])
        style.configure("Hidden.TNotebook", background=_C_BG, tabmargins=0, borderwidth=0)

        # Notebook — purple active tab
        style.configure("TNotebook", background=_C_BG, tabmargins=[2, 5, 0, 0])
        style.configure(
            "TNotebook.Tab",
            background="#DDD5DA", foreground="#555050",
            padding=(14, 6), font=("Segoe UI", 9),
        )
        style.map("TNotebook.Tab",
            background=[
                ("disabled", "#C8C0C5"),
                ("selected", _C_PURPLE),
                ("active",   _C_PURPLE2),
            ],
            foreground=[
                ("disabled", "#999090"),
                ("selected", _C_WHITE),
                ("active",   _C_WHITE),
            ],
            font=[
                ("disabled", ("Segoe UI", 9)),
            ],
        )

        # Entry and Combobox
        style.configure(
            "TEntry",
            fieldbackground=_C_WHITE, bordercolor=_C_BORDER,
            selectbackground=_C_PURPLE, selectforeground=_C_WHITE,
        )
        style.configure(
            "TCombobox",
            fieldbackground=_C_WHITE, selectbackground=_C_PURPLE,
            selectforeground=_C_WHITE, arrowcolor=_C_PURPLE,
        )

        # Progressbar — teal fill
        style.configure(
            "TProgressbar",
            background=_C_TEAL, troughcolor="#DDD5DA",
            bordercolor=_C_BORDER, thickness=12,
        )

        # Checkbutton and Radiobutton
        for w in ("TCheckbutton", "TRadiobutton"):
            style.configure(w, background=_C_BG, foreground=_C_TEXT, font=("Segoe UI", 9))
            style.map(w, background=[("active", _C_BG)], indicatorcolor=[("selected", _C_PURPLE)])

        # Scrollbar
        style.configure(
            "TScrollbar",
            background=_C_BORDER, troughcolor=_C_BG, arrowcolor=_C_PURPLE,
        )

    # ── Window setup ─────────────────────────────────────────────────────

    def _configure_window(self) -> None:
        self.root.title(APP_TITLE)
        # self.root.geometry("960x740")
        # self.root.minsize(700, 500)
        # Wider than before: the sidebar takes ~200 px of the old 960.
        self.root.geometry("1180x780")
        self.root.minsize(940, 560)
        self.root.configure(bg="#f0f0f0")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Restore previous geometry if available; otherwise center on screen
        if not self._load_geometry():
            self.root.update_idletasks()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            x = (self.root.winfo_screenwidth()  - w) // 2
            y = (self.root.winfo_screenheight() - h) // 2
            self.root.geometry(f"+{x}+{y}")

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Assemble status bar, header, resizable notebook+log paned area."""

        # Overrides ttk's default "scroll wheel cycles the value" behavior
        # for every Combobox in the app — see _on_combobox_scroll(). One
        # class-level binding covers every tab instead of wiring each combo.
        self.root.bind_class("TCombobox", "<MouseWheel>", self._on_combobox_scroll)

        # ── Status bar — packed first so it stays at the bottom ──────────
        self._build_status_bar()

        # ── Header bar ───────────────────────────────────────────────────
        header = tk.Frame(self.root, bg=_DARK_BG, height=54)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(
            header,
            text="  ⛃  ",
            font=("Segoe UI", 16),
            bg=_DARK_BG, fg="#E8D5E0",
        ).pack(side="left", pady=8)

        tk.Label(
            header,
            text=APP_TITLE,
            font=("Segoe UI", 13, "bold"),
            bg=_DARK_BG, fg="white",
        ).pack(side="left", pady=8)

        tk.Frame(header, bg=_C_TEAL, width=3).pack(side="left", fill="y", padx=(6, 0), pady=10)

        tk.Label(
            header,
            text=f"v{APP_VERSION}  —  Realnet  ",
            font=("Segoe UI", 9),
            bg=_DARK_BG, fg="#C4AAB8",
        ).pack(side="right", pady=8)

        # Help button — opens the documentation window
        tk.Button(
            header,
            text=" ? ",
            font=("Segoe UI", 10, "bold"),
            bg=_C_TEAL, fg="white",
            relief="flat", cursor="hand2",
            padx=6, pady=2,
            command=self._open_help,
        ).pack(side="right", padx=(0, 6), pady=10)

        # ── Update notification banner (hidden; shown by _show_update_banner) ──
        # Created here so it sits between header and paned in the pack order.
        _BNR_BG   = "#FFF3CD"   # amber warning background
        _BNR_FG   = "#664D03"   # dark amber text
        _BNR_BTN  = "#664D03"   # download button background

        self._frm_banner = tk.Frame(self.root, bg=_BNR_BG, pady=5)
        # Not packed yet — _show_update_banner() positions it via pack(before=).

        tk.Label(
            self._frm_banner,
            text="⚠",
            font=("Segoe UI", 11),
            bg=_BNR_BG, fg=_BNR_FG,
        ).pack(side="left", padx=(10, 4))

        self._lbl_banner_text = tk.Label(
            self._frm_banner,
            text="",
            font=("Segoe UI", 9),
            bg=_BNR_BG, fg=_BNR_FG,
        )
        self._lbl_banner_text.pack(side="left")

        self._btn_banner_dl = tk.Button(
            self._frm_banner,
            text=" Descargar actualización ",
            font=("Segoe UI", 8, "bold"),
            bg=_BNR_BTN, fg="white",
            relief="flat", cursor="hand2",
            padx=6, pady=2,
        )
        self._btn_banner_dl.pack(side="left", padx=(12, 4))

        tk.Button(
            self._frm_banner,
            text=" × ",
            font=("Segoe UI", 11, "bold"),
            bg=_BNR_BG, fg=_BNR_FG,
            activebackground=_BNR_BG, activeforeground="#3D2B02",
            relief="flat", cursor="hand2",
            padx=4, pady=0,
            command=self._dismiss_update_banner,
        ).pack(side="right", padx=(0, 8))

        # ── Body: sidebar (navigation, full height) | pages + log ────────
        body = tk.Frame(self.root, bg=_C_BG)
        body.pack(fill="both", expand=True)
        self._body = body   # anchor for _show_update_banner()
        # Reserves the left edge now; the Sidebar itself is created below,
        # once every page exists, and packed into this holder.
        sidebar_holder = tk.Frame(body, bg=theme.C_SIDEBAR_BG, width=theme.SIDEBAR_WIDTH)
        sidebar_holder.pack(side="left", fill="y")
        sidebar_holder.pack_propagate(False)
        tk.Frame(body, bg=_C_BORDER, width=1).pack(side="left", fill="y")

        # ── Resizable PanedWindow: notebook (top) + log (bottom) ─────────
        # tk.PanedWindow gives a visible, draggable sash the user can resize.
        self._paned = tk.PanedWindow(
            # self.root,
            body,
            orient=tk.VERTICAL,
            sashwidth=7,
            sashpad=1,
            sashrelief="flat",
            bg=_C_BORDER,
            bd=0,
        )
        self._paned.pack(side="left", fill="both", expand=True, padx=_PAD, pady=(_PAD, _PAD))

        # Top pane: Notebook. Its own tab strip is hidden ("Hidden.TNotebook",
        # see _apply_styles): with 14 pages a single row of tabs no longer
        # fits and gave wizard steps, server tools and monitoring the same
        # weight. The notebook still holds/switches the pages, so every
        # existing self.nb.select(i) / self.nb.tab(i, state=...) keeps working.
        # self.nb = ttk.Notebook(self._paned)
        self.nb = ttk.Notebook(self._paned, style="Hidden.TNotebook")
        self._paned.add(self.nb, minsize=280, stretch="always")

        self._tab_connection()
        self._tab_database()
        self._tab_filestore()
        self._tab_destination()
        self._tab_execute()
        self._tab_restore()
        self._tab_addons()
        self._tab_explorer()
        self._tab_terminal()
        self._tab_trial()
        self._tab_automation()
        self._tab_history()
        # Monitoring pages — must stay last, see _TAB_PANEL.
        self._tab_health_panel()
        self._tab_alert_settings()

        # Lock backup tabs 2-5 until connected
        for i in range(1, 5):
            self.nb.tab(i, state="disabled")

        self._sidebar = Sidebar(sidebar_holder, self.nb, _SIDEBAR_SECTIONS)
        self._sidebar.pack(fill="both", expand=True)

        # Open on the dashboard: the state of the clients is the first thing
        # to see, not an empty connection form.
        self.nb.select(_TAB_PANEL)
        self._sync_sidebar_loop()

        # Bottom pane: Log panel
        log_outer = ttk.Frame(self._paned)
        self._paned.add(log_outer, minsize=80, stretch="always")
        self._build_log_panel(log_outer)

        # Restore saved sash position (or default 70/30 split)
        self.root.after(80, self._restore_sash)

        # Auto-connect explorer/terminal panels when the user switches to those tabs
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # ── Keyboard shortcuts ────────────────────────────────────────────
        # Ctrl+L: clear log;  F5: reload database list
        self.root.bind("<Control-l>", lambda e: self._clear_log())
        self.root.bind("<Control-L>", lambda e: self._clear_log())
        self.root.bind("<F5>",        lambda e: self._action_load_dbs())

    # ── Tab auto-connect ─────────────────────────────────────────────────

    def _on_tab_changed(self, event=None) -> None:
        """Auto-connect explorer/terminal/trial panels when their tab is selected.

        Called by <<NotebookTabChanged>>.  Uses after(100) so the tab frame
        has finished rendering before triggering network I/O.
        """
        try:
            idx = self.nb.index(self.nb.select())
        except Exception:
            return

        sidebar = getattr(self, "_sidebar", None)
        if sidebar is not None:
            sidebar.sync()

        if idx == _TAB_PANEL and self._health_panel is not None:
            # Re-evaluate from stored data (no network) on every visit.
            self._health_panel.request_refresh()
        elif idx == 7:  # Tab 8 — Explorador
            self.root.after(100, self._auto_connect_explorer)
        elif idx == 8:  # Tab 9 — Terminal
            self.root.after(100, self._auto_connect_terminal)
        elif idx == 9:  # Tab Trial
            self.root.after(100, self._auto_refresh_trial)
        elif idx == 11:  # Tab Historial
            self.root.after(100, self._refresh_history_tree)

    def _auto_connect_explorer(self) -> None:
        """Connect explorer panels that are not yet connected, if SSH is active."""
        panel_l = getattr(self, "_panel_l", None)
        panel_r = getattr(self, "_panel_r", None)
        if panel_l and panel_l._browser is None and self._ssh.connected:
            panel_l.connect_and_navigate("/")
        if panel_r and panel_r._browser is None:
            if self._ssh_restore.connected or self._ssh_dest.connected:
                panel_r.connect_and_navigate("/")

    def _auto_connect_terminal(self) -> None:
        """Connect terminal panels that are not yet connected, if SSH is active."""
        term_l = getattr(self, "_term_l", None)
        term_r = getattr(self, "_term_r", None)
        if term_l and not term_l._connected and self._ssh.connected:
            term_l.connect()
        if term_r and not term_r._connected:
            if self._ssh_restore.connected or self._ssh_dest.connected:
                term_r.connect()

    def _auto_refresh_trial(self) -> None:
        """Refresh Trial tab server status label when the tab is selected."""
        fn = getattr(self, "_trial_refresh_srv", None)
        if fn:
            fn()

    # ── Status bar ───────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        """Fixed thin bar at the bottom: SSH connection state + operation status."""
        _BAR_BG = "#E0D8DC"
        bar = tk.Frame(self.root, bg=_BAR_BG, height=22, relief="sunken", bd=1)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)

        # Connection indicator dot
        self._lbl_status_conn = tk.Label(
            bar, text="  ● Desconectado",
            font=("Segoe UI", 8), bg=_BAR_BG, fg="#888888",
        )
        self._lbl_status_conn.pack(side="left", padx=(4, 2))

        tk.Frame(bar, bg=_C_BORDER, width=1).pack(side="left", fill="y", padx=6, pady=3)

        # Current operation state
        self._lbl_status_op = tk.Label(
            bar, text="Listo",
            font=("Segoe UI", 8), bg=_BAR_BG, fg="#666666",
        )
        self._lbl_status_op.pack(side="left")

        # Keyboard shortcut hints (right-aligned)
        tk.Label(
            bar,
            text="Ctrl+L: limpiar log   F5: recargar BDs  ",
            font=("Segoe UI", 7), bg=_BAR_BG, fg="#AAAAAA",
        ).pack(side="right")

        # Client-monitoring verdict, visible from every page; click opens
        # the dashboard. Updated by _on_health_refresh().
        self._lbl_status_monitor = tk.Label(
            bar, text="● Monitor: sin datos  ",
            font=("Segoe UI", 8), bg=_BAR_BG, fg="#888888", cursor="hand2",
        )
        self._lbl_status_monitor.pack(side="right")
        self._lbl_status_monitor.bind("<Button-1>", lambda _e: self.nb.select(_TAB_PANEL))

    def _set_status_conn(self, text: str, ok: bool = False) -> None:
        """Update the connection indicator in the status bar (GUI thread only)."""
        color = "#2E8B57" if ok else "#888888"
        self._lbl_status_conn.config(text=f"  ● {text}", fg=color)

    def _set_status_op(self, text: str, color: str = "#555555") -> None:
        """Update the operation label in the status bar (GUI thread only)."""
        self._lbl_status_op.config(text=text, fg=color)

    # ── Log panel ─────────────────────────────────────────────────────────

    def _build_log_panel(self, parent: ttk.Frame) -> None:
        """Build the log area with toolbar and color-coded text widget."""
        # ── Toolbar ───────────────────────────────────────────────────────
        toolbar = tk.Frame(parent, bg=_C_BG, pady=2)
        toolbar.pack(fill="x", side="top")

        tk.Label(
            toolbar, text=" Log de operaciones",
            font=("Segoe UI", 9, "bold"),
            bg=_C_BG, fg=_C_PURPLE,
        ).pack(side="left", padx=(4, 0))

        # Auto-scroll toggle: when enabled, new messages auto-scroll to the end
        self._v_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar, text="Auto-scroll",
            variable=self._v_autoscroll,
        ).pack(side="left", padx=10)

        # Right-side action buttons
        ttk.Button(toolbar, text="Exportar", command=self._export_log).pack(side="right", padx=(2, 6))
        ttk.Button(toolbar, text="Copiar",   command=self._copy_log).pack(side="right", padx=2)
        ttk.Button(toolbar, text="Limpiar",  command=self._clear_log).pack(side="right", padx=2)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", side="top")

        # ── Text widget ───────────────────────────────────────────────────
        self.log_widget = tk.Text(
            parent,
            font=("Consolas", 9),
            bg=_LOG_BG,
            fg=_LOG_FG,
            wrap="word",
            state="disabled",
            cursor="arrow",      # read-only appearance
        )
        log_sb = ttk.Scrollbar(parent, orient="vertical", command=self.log_widget.yview)
        self.log_widget.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self.log_widget.pack(fill="both", expand=True)

        # ── Color tags (line-level) ───────────────────────────────────────
        self.log_widget.tag_configure("timestamp", foreground="#6E6466")
        self.log_widget.tag_configure("normal",    foreground=_LOG_FG)
        self.log_widget.tag_configure("error",     foreground="#FF6B6B",
                                                   font=("Consolas", 9, "bold"))
        self.log_widget.tag_configure("success",   foreground="#7FDB7F")
        self.log_widget.tag_configure("warning",   foreground="#FFB347")
        self.log_widget.tag_configure("process",   foreground="#64B4D4")
        # ── Keyword highlight tags (word-level, raised above line tags) ───
        self.log_widget.tag_configure("kw_info",    foreground="#58ADEF",
                                                    font=("Consolas", 9, "bold"))
        self.log_widget.tag_configure("kw_warning", foreground="#FFD700",
                                                    font=("Consolas", 9, "bold"))
        self.log_widget.tag_configure("kw_error",   foreground="#FF4444",
                                                    font=("Consolas", 9, "bold"))
        # Raise keyword tags above all line-level tags so they take visual priority
        self.log_widget.tag_raise("kw_info")
        self.log_widget.tag_raise("kw_warning")
        self.log_widget.tag_raise("kw_error")

    def _clear_log(self) -> None:
        """Clear all content from the log widget."""
        self.log_widget.config(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.config(state="disabled")

    def _copy_log(self) -> None:
        """Copy the full log content to the system clipboard."""
        content = self.log_widget.get("1.0", "end").strip()
        if not content:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self._set_status_op("Log copiado al portapapeles.", color=_C_TEAL)
        self.root.after(2500, lambda: self._set_status_op("Listo"))

    def _export_log(self) -> None:
        """Save log content to a .txt file chosen by the user."""
        content = self.log_widget.get("1.0", "end").strip()
        if not content:
            messagebox.showinfo(APP_TITLE, "El log esta vacio.")
            return
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
        path = filedialog.asksaveasfilename(
            title="Exportar log",
            defaultextension=".txt",
            initialfile=f"odoo_backup_log_{ts}.txt",
            filetypes=[("Texto", "*.txt"), ("Todos", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self._set_status_op(f"Log exportado: {os.path.basename(path)}", color=_C_TEAL)
            self.root.after(3000, lambda: self._set_status_op("Listo"))
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Error exportando log:\n{exc}")

    # ── Geometry / sash persistence ───────────────────────────────────────

    def _load_geometry(self) -> bool:
        """
        Restore window size and position from the previous session.

        Returns True if geometry was successfully restored so the caller
        can skip the default centering step.
        """
        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            geo = data.get("geometry", "")
            if geo:
                self.root.geometry(geo)
                return True
        except Exception:
            pass
        return False

    def _save_geometry(self) -> None:
        """Persist window geometry and log-panel sash position to disk."""
        try:
            os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
            sash_y: int | None = None
            try:
                sash_y = self._paned.sash_coord(0)[1]
            except Exception:
                pass
            settings = {
                "geometry": self.root.geometry(),
                "log_sash": sash_y,
            }
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(settings, fh, indent=2)
        except Exception:
            pass

    def _restore_sash(self) -> None:
        """
        Position the PanedWindow sash after the window is fully rendered.

        Tries to restore the last saved position; falls back to a 70/30
        (notebook/log) default split.
        """
        h = self._paned.winfo_height()
        if h <= 10:
            # Window not yet rendered — retry
            self.root.after(100, self._restore_sash)
            return

        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            sash_y = data.get("log_sash")
            if isinstance(sash_y, (int, float)) and sash_y > 0:
                # self._paned.sash_place(0, 0, int(sash_y))
                # A position saved with the old tab layout can leave the
                # pages less than half the window, which hides most of the
                # dashboard. Keep the user's choice, but not below that.
                self._paned.sash_place(0, 0, max(int(sash_y), int(h * _MIN_PAGES_FRACTION)))
                return
        except Exception:
            pass

        # Default: 70% notebook, 30% log
        # h = self._paned.winfo_height()
        # if h > 10:
        #     self._paned.sash_place(0, 0, int(h * 0.70))
        # else:
        #     # Window not yet rendered — retry
        #     self.root.after(100, self._restore_sash)
        self._paned.sash_place(0, 0, int(h * 0.70))

    # ── Scroll isolation helper ───────────────────────────────────────────

    def _bind_mousewheel(self, canvas: tk.Canvas) -> None:
        """
        Scope mousewheel scrolling to `canvas` only while the pointer is inside it.

        Replaces canvas.bind_all("<MouseWheel>", ...) which would fire on ALL
        canvases simultaneously whenever any scroll event occurs anywhere in the
        window (a known tkinter gotcha when multiple scrollable panels exist).

        Using <Enter>/<Leave> events to install/remove the binding limits the
        handler to the canvas currently under the mouse pointer. Also tracks
        self._active_scroll_canvas so the "TCombobox" class binding installed
        in _build_ui (see _on_combobox_scroll) knows which canvas to scroll
        when the pointer happens to be over a combobox instead of over open
        canvas space.
        """
        def _on_scroll(event: tk.Event) -> None:
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        def _enter(_event: tk.Event) -> None:
            canvas.bind_all("<MouseWheel>", _on_scroll)
            self._active_scroll_canvas = canvas

        def _leave(_event: tk.Event) -> None:
            canvas.unbind_all("<MouseWheel>")
            if self._active_scroll_canvas is canvas:
                self._active_scroll_canvas = None

        canvas.bind("<Enter>", _enter)
        canvas.bind("<Leave>", _leave)

    def _on_combobox_scroll(self, event: tk.Event) -> str:
        """
        Class-level <MouseWheel> handler bound to "TCombobox" (see _build_ui).

        ttk.Combobox's own default binding cycles its selected value on
        mousewheel scroll — surprising and unwanted when the user is just
        scrolling past it to read the rest of a scrollable tab, not trying to
        change the field. This replaces that behavior: forward the scroll to
        whichever canvas is currently active (see _bind_mousewheel) instead,
        so a combobox behaves like any other static content while scrolling.
        Returning "break" stops ttk's own class binding from also firing.
        """
        if self._active_scroll_canvas is not None:
            self._active_scroll_canvas.yview_scroll(-1 * (event.delta // 120), "units")
        return "break"

    # ── Help window ──────────────────────────────────────────────────────

    def _open_help(self) -> None:
        """Open (or bring to front) the documentation window."""
        # Reuse existing window instead of opening duplicates
        if hasattr(self, "_help_win") and self._help_win.winfo_exists():
            self._help_win.lift()
            self._help_win.focus_set()
            return
        self._help_win = HelpWindow(self)

    # ── Update banner ────────────────────────────────────────────────────

    def _on_update_check_result(
        self, new_version: str | None, download_url: str | None
    ) -> None:
        """
        Callback invoked from the background update-checker thread.

        Schedules the banner display on the Tkinter main thread so it is
        always safe to call from any thread.
        """
        if new_version:
            self.root.after(
                0, lambda: self._show_update_banner(new_version, download_url or "")
            )

    def _show_update_banner(self, version: str, download_url: str) -> None:
        """
        Reveal the update banner below the header bar.

        Uses pack(before=self._body) so the banner is always positioned
        between the header and the main content area regardless of when it
        is called. (self._body, not self._paned: `before=` needs a sibling,
        and the paned area now lives inside the body frame next to the
        sidebar.)
        """
        self._lbl_banner_text.config(
            text=f"Nueva version {version} disponible — version instalada: {APP_VERSION}"
        )
        self._btn_banner_dl.config(
            command=lambda: self._open_update_download(download_url)
        )
        # self._frm_banner.pack(fill="x", before=self._paned)
        self._frm_banner.pack(fill="x", before=self._body)

    def _dismiss_update_banner(self) -> None:
        """Hide the update banner (user dismissed it)."""
        self._frm_banner.pack_forget()

    def _open_update_download(self, url: str) -> None:
        """Open the download URL in the system default browser."""
        import webbrowser
        if url:
            webbrowser.open(url)

    # ── Shared helper ────────────────────────────────────────────────────

    def _scrollable_tab(self, tab_text: str) -> ttk.Frame:
        """
        Create a scrollable tab and return its inner content frame.

        Wraps the tab in a Canvas + Scrollbar so the content survives small
        window sizes.  Scroll is isolated to this canvas via _bind_mousewheel
        so sibling canvas tabs are not affected.

        Args:
            tab_text: Label displayed on the notebook tab.

        Returns:
            A ttk.Frame with padding already applied.  Add child widgets to it.
        """
        outer = ttk.Frame(self.nb, padding=0)
        self.nb.add(outer, text=tab_text)

        canvas = tk.Canvas(outer, highlightthickness=0, bg=_C_BG)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        f = ttk.Frame(canvas, padding=_PAD * 2)
        win_id = canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(win_id, width=e.width),
        )
        self._bind_mousewheel(canvas)
        return f

    def _begin_operation(self) -> None:
        """Called at the start of any operation: reset cancel flag, toggle buttons."""
        self._cancel_event.clear()
        self._btn_stop_backup.config(state="normal")
        self._btn_stop_restore.config(state="normal")
        self._btn_stop_addons.config(state="normal")
        self._set_status_op("En curso...", color=_C_PURPLE)

    def _end_operation(self) -> None:
        """Called when any operation ends (success, error or cancel)."""
        self._btn_stop_backup.config(state="disabled")
        self._btn_stop_restore.config(state="disabled")
        self._btn_stop_addons.config(state="disabled")

    def _server_label_for(self, combo_value: str, host: str) -> str:
        """Profile name for the history table, falling back to the host
        when no saved profile is selected (e.g. ad-hoc connection)."""
        combo_value = (combo_value or "").strip()
        if combo_value and combo_value != _PROFILE_NEW:
            return combo_value
        return host or "?"

    def _history_begin(self, action_type: str, server_label: str, host: str) -> None:
        """
        Mark the start of a manual operation (backup/restore/addons sync) for
        the persistent history log. Every line subsequently appended via
        self._log()/_append_log() is captured until _history_end() flushes it.
        """
        self._current_op_meta = {
            "action_type": action_type,
            "server_label": server_label,
            "host": host,
            "started_at": time.time(),
        }
        self._current_op_log = []

    def _history_end(self, status: str, summary: str) -> None:
        """Flush the buffered operation (if any) to self._history."""
        meta = self._current_op_meta
        if meta is None:
            return
        self._history.record(
            action_type=meta["action_type"],
            server_label=meta["server_label"],
            host=meta["host"],
            status=status,
            summary=summary,
            log_text="\n".join(self._current_op_log),
            started_at=meta["started_at"],
        )
        self._current_op_meta = None
        self._current_op_log = []

    def _on_terminal_session_end(self, host: str, panel_title: str, commands: list[str]) -> None:
        """
        Log a closed SSH Terminal (Tab 9) session to the persistent history.
        Called from SshTerminalPanel.disconnect() — see gui/ssh_terminal_panel.py.
        Commands are the only thing captured (not full terminal output/scrollback,
        which is mostly prompts and command noise not useful for an audit trail).
        """
        summary = (
            f"Sesion de terminal — {len(commands)} comando(s) ejecutado(s)"
            if commands else "Sesion de terminal — sin comandos ejecutados"
        )
        log_text = "\n".join(f"$ {c}" for c in commands)
        self._history.record(
            action_type="terminal",
            server_label=panel_title,
            host=host,
            status="ok",
            summary=summary,
            log_text=log_text,
        )

    # ── Overwrite dialog (called from worker thread) ──────────────────────

    def _ask_overwrite(self, filename: str, dest_desc: str) -> tuple[str, str]:
        """
        Ask the user what to do when a destination file already exists.

        Blocks the calling worker thread until the user responds.
        Must be called from a background thread (never from the GUI thread).

        Returns:
            (action, final_filename)
            action: 'rename_ts' | 'rename_custom' | 'overwrite' | 'cancel'
        """
        event = threading.Event()
        result: dict = {}
        self._q.put(("ask_overwrite", (filename, dest_desc, event, result)))
        event.wait()
        return result.get("action", "cancel"), result.get("filename", filename)

    def _ask_confirm(self, title: str, message: str) -> bool:
        """
        Show a yes/no confirmation dialog from a background worker thread and
        block until the user answers. Same blocking pattern as _ask_overwrite()
        — the dialog itself must run on the GUI thread, so this posts a queue
        event and waits on a threading.Event the GUI thread sets after the
        user responds.
        """
        event = threading.Event()
        result: dict = {}
        self._q.put(("ask_confirm", (title, message, event, result)))
        event.wait()
        return result.get("confirmed", False)

    # ── Thread-safe GUI updates ───────────────────────────────────────────

    def _log(self, msg: str) -> None:
        """Queue a log message from any thread."""
        self._q.put(("log", msg))

    def _sweep_orphaned_temp_files(self) -> None:
        """
        Background-thread entry point: delete remote /tmp files this tool
        created in a previous run and never confirmed cleaning up (crash,
        force-close, or a job that failed before reaching its own cleanup).
        See core/temp_registry.py for why the manifest is safe to trust.
        """
        try:
            deleted = sweep_orphaned_files(self._temp_registry, log_callback=self._log)
            if deleted:
                self._log(f"[limpieza-huerfanos] {deleted} archivo(s) huerfano(s) eliminados del servidor.")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[limpieza-huerfanos] Error durante el barrido: {exc}")

    def _poll_queue(self) -> None:
        """Drain the message queue and update the GUI (called every 100 ms)."""
        try:
            while True:
                event, data = self._q.get_nowait()
                self._last_activity_ts = time.monotonic()

                if event == "log":
                    self._append_log(data)

                elif event == "conn_ok":
                    self._lbl_conn_status.config(text=f"  {data}", foreground="green")
                    self._btn_connect.config(state="normal")
                    self._btn_disconnect.config(state="normal")
                    self._set_status_conn(data.replace("Conectado a ", ""), ok=True)
                    self._append_log(data)
                    self.nb.tab(1, state="normal")
                    self.nb.select(1)
                    self._action_load_dbs()
                    # If explorer/terminal tab is already visible, auto-connect left panel
                    try:
                        cur = self.nb.index(self.nb.select())
                        if cur == 7:
                            self.root.after(200, self._auto_connect_explorer)
                        elif cur == 8:
                            self.root.after(200, self._auto_connect_terminal)
                    except Exception:
                        pass

                elif event == "conn_fail":
                    self._lbl_conn_status.config(text=f"  {data}", foreground="red")
                    self._btn_connect.config(state="normal")
                    self._btn_disconnect.config(state="disabled")
                    self._set_status_conn("Conexion fallida", ok=False)
                    self._append_log(f"Error de conexion: {data}")

                elif event == "db_list":
                    self._cb_db["values"] = data
                    if data:
                        self._cb_db.current(0)
                    self._append_log(f"Bases de datos disponibles: {', '.join(data)}")

                elif event == "fs_roots":
                    self._cb_fs_root["values"] = data
                    if data:
                        self._v_fs_root.set(data[0])
                        self._append_log(f"Filestore detectado en: {data[0]}")
                    else:
                        self._append_log("No se encontraron rutas de filestore conocidas. Ingresela manualmente.")

                elif event == "fs_folders":
                    self._cb_fs_db["values"] = data
                    db = self._v_db.get()
                    if db in data:
                        self._v_fs_db.set(db)
                    elif data:
                        self._cb_fs_db.current(0)

                elif event == "fs_tree":
                    self._fs_tree.delete(*self._fs_tree.get_children())
                    for entry in data:
                        icon = "[DIR]" if entry["type"] == "d" else "[FILE]"
                        self._fs_tree.insert(
                            "", "end",
                            text=f"{icon}  {entry['name']}",
                            values=(entry["size"],),
                        )

                elif event == "progress":
                    pct, label = data
                    self._v_progress.set(pct)
                    self._lbl_progress.config(text=label)

                elif event == "cancelled":
                    self._v_progress.set(0)
                    self._lbl_progress.config(text=f"  {data}")
                    self._append_log(f"DETENIDO: {data}")
                    self._btn_run.config(state="normal")
                    self._set_status_op("Detenido", color="#888888")
                    self._end_operation()
                    self._history_end("cancelled", data)
                    messagebox.showwarning(APP_TITLE, data)

                elif event == "done":
                    self._v_progress.set(100)
                    self._lbl_progress.config(text=f"  {data}")
                    self._append_log(f"COMPLETADO: {data}")
                    self._btn_run.config(state="normal")
                    self._set_status_op("✓ Completado", color="#2E8B57")
                    self._end_operation()
                    self._history_end("ok", data)
                    messagebox.showinfo(APP_TITLE, data)

                elif event == "error":
                    self._append_log(f"[ERROR] {data}")
                    self._set_status_op("✗ Error (ver log)", color="#C0392B")
                    self._end_operation()
                    self._history_end("error", data)
                    messagebox.showerror(APP_TITLE, data)

                elif event == "btn_enable":
                    self._btn_run.config(state="normal")

                elif event == "transfer_failed":
                    self._show_retry_panel(data)

                elif event == "ask_overwrite":
                    filename, dest_desc, ev, result = data
                    action, final_name = _OverwriteDialog(self.root, filename, dest_desc).show()
                    result["action"] = action
                    result["filename"] = final_name
                    ev.set()   # unblock the worker thread

                elif event == "ask_confirm":
                    title, message, ev, result = data
                    result["confirmed"] = messagebox.askyesno(title, message, parent=self.root)
                    ev.set()   # unblock the worker thread

                # ── Restore events ────────────────────────────────────────
                elif event == "r_conn_ok":
                    self._lbl_b_conn_status.config(text=f"  {data}", foreground="green")
                    self._btn_r_connect.config(state="normal")
                    self._btn_r_disconnect.config(state="normal")
                    # Refresh Tab 6 label if it shows Servidor B
                    if self._v_r_conn_type.get() == "receptor":
                        self._toggle_restore_conn()
                    self._append_log(data)
                    # If explorer/terminal tab is already visible, auto-connect right panel
                    try:
                        cur = self.nb.index(self.nb.select())
                        if cur == 7:
                            self.root.after(200, self._auto_connect_explorer)
                        elif cur == 8:
                            self.root.after(200, self._auto_connect_terminal)
                    except Exception:
                        pass

                elif event == "r_conn_fail":
                    self._lbl_b_conn_status.config(text=f"  {data}", foreground="red")
                    self._btn_r_connect.config(state="normal")
                    self._btn_r_disconnect.config(state="disabled")
                    self._append_log(f"Error conexion Servidor B: {data}")

                elif event == "r_fs_roots":
                    self._r_fs_root_combo["values"] = data
                    if data:
                        self._v_r_fs_root.set(data[0])
                        self._append_log(f"Filestore destino detectado en: {data[0]}")
                    else:
                        self._append_log("No se detectaron rutas de filestore en el destino. Ingresela manualmente.")

                elif event == "r_progress":
                    pct, label = data
                    self._v_r_progress.set(pct)
                    self._lbl_r_progress.config(text=label)

                elif event == "r_cancelled":
                    self._v_r_progress.set(0)
                    self._lbl_r_progress.config(text=f"  {data}")
                    self._append_log(f"DETENIDO: {data}")
                    self._btn_restore.config(state="normal")
                    self._set_status_op("Detenido", color="#888888")
                    self._end_operation()
                    self._history_end("cancelled", data)
                    messagebox.showwarning(APP_TITLE, data)

                elif event == "r_done":
                    self._v_r_progress.set(100)
                    self._lbl_r_progress.config(text=f"  {data}")
                    self._append_log(f"COMPLETADO: {data}")
                    self._btn_restore.config(state="normal")
                    self._set_status_op("✓ Completado", color="#2E8B57")
                    self._end_operation()
                    self._history_end("ok", data)
                    messagebox.showinfo(APP_TITLE, data)

                elif event == "btn_r_enable":
                    self._btn_restore.config(state="normal")

                elif event == "r_check_failed":
                    # Restore finished but post-restore checks found blocking issues
                    d = data
                    db  = d["db"]
                    err_text  = d["errors"]
                    warn_text = d["warnings"]

                    # Mark progress as 100% — the files ARE there, the issues need attention
                    self._v_r_progress.set(100)
                    self._lbl_r_progress.config(
                        text=f"  Restauracion completada con errores — revise el log"
                    )
                    self._btn_restore.config(state="normal")
                    self._set_status_op("⚠ Verificacion fallida — ver log", color="#E67E22")
                    self._end_operation()

                    body = f"La restauracion de '{db}' finalizo pero las verificaciones detectaron problemas.\n\n"
                    if err_text:
                        body += f"ERRORES CRITICOS (explican por que no se puede iniciar sesion):\n{err_text}\n\n"
                    if warn_text:
                        body += f"AVISOS:\n{warn_text}\n\n"
                    body += "Consulte el log completo para el detalle de cada verificacion."

                    self._append_log(f"[ERROR] Verificacion post-restauracion fallo para '{db}'.")
                    self._history_end(
                        "warning",
                        f"Restauracion de '{db}' completada con problemas de verificacion.",
                    )
                    messagebox.showerror(APP_TITLE, body)

                # ── Addons sync events ────────────────────────────────────
                elif event == "addons_progress":
                    pct, label = data
                    self._v_a_progress.set(pct)
                    self._lbl_a_progress.config(text=label)

                elif event == "addons_server_keys":
                    # Result of scanning server's ~/.ssh/ for private keys
                    keys: list[str] = data
                    self._btn_scan_server.config(state="normal")
                    if keys:
                        self._cb_a_server_key["values"] = keys
                        if not self._v_a_server_key.get():
                            self._v_a_server_key.set(keys[0])
                        self._lbl_a_server_key_hint.config(
                            text=f"  {len(keys)} llave(s) encontrada(s) en el servidor.",
                            foreground="#2E8B57",
                        )
                        self._append_log(
                            f"Llaves del servidor escaneadas: {len(keys)} encontrada(s)."
                        )
                    else:
                        self._cb_a_server_key["values"] = []
                        self._lbl_a_server_key_hint.config(
                            text="  No se encontraron llaves en ~/.ssh/ del servidor."
                            "  Use el modo 'Llave de esta maquina'.",
                            foreground="#CC4444",
                        )
                        self._append_log(
                            "No se encontraron llaves SSH en el servidor."
                        )

                elif event == "addons_service":
                    # Auto-detected service name from the remote server
                    self._v_a_service.set(data)
                    self._append_log(f"Servicio Odoo detectado: {data}")

                elif event == "addons_done":
                    self._v_a_progress.set(100)
                    self._lbl_a_progress.config(text=f"  {data}")
                    self._append_log(f"COMPLETADO: {data}")
                    self._btn_sync_addons.config(state="normal")
                    self._set_status_op("✓ Completado", color="#2E8B57")
                    self._end_operation()
                    self._history_end("ok", data)
                    messagebox.showinfo(APP_TITLE, data)

                elif event == "addons_cancelled":
                    self._v_a_progress.set(0)
                    self._lbl_a_progress.config(text=f"  {data}")
                    self._append_log(f"DETENIDO: {data}")
                    self._btn_sync_addons.config(state="normal")
                    self._set_status_op("Detenido", color="#888888")
                    self._end_operation()
                    self._history_end("cancelled", data)
                    messagebox.showwarning(APP_TITLE, data)

                elif event == "btn_addons_enable":
                    self._btn_sync_addons.config(state="normal")

                # ── Scheduler events ──────────────────────────────────────
                elif event == "sched_refresh":
                    self._sched_refresh_tree(data)

                elif event == "sched_log":
                    rule_id, message = data
                    self._append_log(message)

                # ── Monitoring events ─────────────────────────────────────
                elif event == "health_refresh":
                    self._on_health_refresh(data)

                elif event == "ui_call":
                    # Generic "run this on the GUI thread" for panels that
                    # live outside this module (gui/health_panel.py, ...).
                    data()

        except queue.Empty:
            pass

        self.root.after(100, self._poll_queue)

    def _append_log(self, msg: str) -> None:
        """Append a color-coded, timestamped line to the log panel (GUI thread only)."""
        if self._current_op_meta is not None:
            self._current_op_log.append(msg)

        ts = datetime.datetime.now().strftime("%H:%M:%S")

        # ── Line-level tag selection ───────────────────────────────────────
        m = msg.strip()
        if m.startswith("[ERROR]") or m.startswith("Rollback:"):
            tag = "error"
        elif (
            m.startswith("COMPLETADO:")
            or m.startswith("Dump restaurado")
            or m.startswith("Filestore restaurado")
            or m.startswith("Permisos de")
            or "correctamente" in m.lower()
            or "completado" in m.lower()
        ):
            tag = "success"
        elif (
            m.startswith("DETENIDO:")
            or "[aviso]" in m.lower()
            or "advertencia" in m.lower()
            or "INCOMPATIBILIDAD" in m
            or m.startswith("Rollback")
        ):
            tag = "warning"
        elif m.startswith("  [") and (
            "en curso" in m or "en proceso" in m
        ):
            # In-progress heartbeat lines like "  [pg_dump en curso] ..."
            tag = "process"
        else:
            tag = "normal"

        self.log_widget.config(state="normal")
        self.log_widget.insert("end", f"[{ts}]  ", "timestamp")
        # Save position just before the message text so we can highlight keywords
        msg_start = self.log_widget.index("end")
        self.log_widget.insert("end", f"{msg}\n", tag)
        # Apply per-word keyword highlighting on top of the line tag
        self._highlight_log_keywords(msg_start, msg)
        if self._v_autoscroll.get():
            self.log_widget.see("end")
        self.log_widget.config(state="disabled")

    def _highlight_log_keywords(self, base_idx: str, msg: str) -> None:
        """
        Apply kw_info / kw_warning / kw_error tags on keyword matches
        within the just-inserted message text.

        base_idx: the tk.Text index where msg starts (right after the timestamp).
        """
        for match in _KW_LOG_RE.finditer(msg):
            word = match.group(0).lower().strip("[]")
            if "error" in word:
                tag_name = "kw_error"
            elif "warn" in word:
                tag_name = "kw_warning"
            else:
                tag_name = "kw_info"
            start = f"{base_idx}+{match.start()}c"
            end   = f"{base_idx}+{match.end()}c"
            self.log_widget.tag_add(tag_name, start, end)

    # ── Window close ─────────────────────────────────────────────────────

    def _on_close(self) -> None:
        """
        Confirm close if a backup is genuinely active, then clean up SSH.

        "Active" vs "frozen": a manual backup (Tab 5) or a scheduled rule
        (Tab automatizacion) can be marked as running while actually being
        stuck — e.g. a stalled network read that hasn't yet hit the socket
        inactivity timeout in ssh_client.py. Interrupting a HEALTHY transfer
        deserves a confirmation (nohup keeps the remote side going, but the
        user should know); interrupting one that's already dead in the water
        does not — there is nothing real left to lose, so closing proceeds
        without asking. The distinguishing signal is recency of queue
        activity (self._last_activity_ts, refreshed on every log/progress
        event in _poll_queue): if nothing has been reported in over
        _FROZEN_ACTIVITY_THRESHOLD_SECS, treat it as frozen.

        Because long commands run via nohup on the server, they survive
        the SSH disconnect — the user can safely close and the operation
        continues. We still warn (when active) so they don't assume it was
        cancelled.

        The active/frozen check and (if active) the confirmation dialog run
        before any window feedback appears, and afterwards closing every SSH
        connection is bounded but can still take a few seconds — from the
        user's point of view that's a silent gap after clicking the X where
        the window doesn't visibly react, easy to mistake for the app having
        missed the click or frozen. A small always-shown status window
        ("Validando tareas pendientes...") is opened first and forced to
        paint immediately, before any of that work runs, purely as feedback
        that the click registered — it carries no decision of its own.
        """
        status = tk.Toplevel(self.root)
        status.title(APP_TITLE)
        status.resizable(False, False)
        status.transient(self.root)
        tk.Label(
            status,
            text="Validando tareas pendientes...",
            padx=24,
            pady=16,
        ).pack()
        status.update_idletasks()
        w, h = status.winfo_reqwidth(), status.winfo_reqheight()
        x = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        status.geometry(f"{w}x{h}+{x}+{y}")
        status.update()

        manual_running = str(self._btn_run.cget("state")) == "disabled"
        scheduled_running = self._scheduler.has_active_jobs()

        if manual_running or scheduled_running:
            idle_secs = time.monotonic() - self._last_activity_ts
            is_frozen = idle_secs > _FROZEN_ACTIVITY_THRESHOLD_SECS

            if not is_frozen:
                parts = []
                if manual_running:
                    parts.append("un backup manual (Tab 5)")
                if scheduled_running:
                    parts.append("una regla programada (Automatizacion)")
                running_desc = " y ".join(parts)
                status.withdraw()  # hide behind the modal confirm dialog
                answer = messagebox.askyesno(
                    APP_TITLE,
                    f"Hay {running_desc} en ejecucion activa.\n\n"
                    "Los procesos en el servidor (pg_dump / tar) continuaran "
                    "corriendo aunque cierres esta ventana, porque se lanzaron "
                    "con nohup.\n\n"
                    "Puedes reconectarte mas tarde para descargar los archivos "
                    "de /tmp/ cuando terminen.\n\n"
                    "Cerrar de todos modos?",
                )
                if not answer:
                    status.destroy()
                    return
                status.deiconify()
            # else: marked as running but no progress in a while — treat as
            # frozen/stuck and close without prompting, no confirmation needed.

        status.title(APP_TITLE)
        for child in status.winfo_children():
            child.configure(text="Validando procesos antes de cerrar...")
        status.update()

        self._save_geometry()

        # Close every connection in parallel (each internally bounded to a
        # few seconds — see SSHClient.close() / SshTerminalPanel.disconnect())
        # so a single stuck connection can't make closing the app take
        # 3-4x as long by waiting on them one at a time.
        closers = [
            getattr(self, "_term_l", None),
            getattr(self, "_term_r", None),
            self._ssh,
            self._ssh_restore,
            self._ssh_dest,
        ]

        def _close_one(obj) -> None:
            try:
                if hasattr(obj, "disconnect"):
                    obj.disconnect()
                else:
                    obj.close()
            except Exception:
                pass

        threads = []
        for obj in closers:
            if obj is None:
                continue
            t = threading.Thread(target=_close_one, args=(obj,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=6)

        # Stop the background scheduler daemon before destroying the window
        try:
            self._scheduler.stop()
        except Exception:
            pass
        # Hand the schedule back explicitly so a waiting headless monitor
        # can take over right away (the OS would release it on exit anyway).
        self._sched_lock.release()
        status.destroy()
        self.root.destroy()
