"""
Ventana de ayuda y documentacion de Odoo Backup Tool.

Muestra documentacion detallada de cada pestaña y concepto de la herramienta.
Incluye captura automatica de pantallas de cada tab para ilustrar la interfaz.

Layout:
    ┌─────────────────────────────────────────────────────────┐
    │  [Toolbar: Capturar pantallas]                          │
    ├──────────────┬──────────────────────────────────────────┤
    │  Sidebar     │  Contenido (texto enriquecido + imágenes)│
    │  (secciones) │                                          │
    └──────────────┴──────────────────────────────────────────┘
"""
from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

try:
    from PIL import Image, ImageGrab, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

if TYPE_CHECKING:
    pass  # avoid circular import with app.py

# ── Constants ────────────────────────────────────────────────────────────────

_HELP_IMG_DIR_NAME = os.path.join("help", "img")

# Section IDs map to their display label and the bound content renderer
_SECTIONS: list[tuple[str, str]] = [
    ("intro",       "Introduccion"),
    ("tab1",        "Tab 1 — Conexion SSH"),
    ("tab2",        "Tab 2 — Backup"),
    ("tab3",        "Tab 3 — Filestore"),
    ("tab4",        "Tab 4 — Servidor Destino"),
    ("tab5",        "Tab 5 — Verificacion"),
    ("tab6",        "Tab 6 — Restaurar"),
    ("tab7",        "Tab 7 — Addons / Git"),
    ("tab8",        "Tab 8 — Explorador"),
    ("concepts",    "Conceptos Clave"),
    ("full_flow",   "Flujo Completo"),
    ("faq",         "Preguntas Frecuentes"),
]

# Mapping section ID → tab index in the main notebook (for screenshot capture)
_SECTION_TAB_INDEX: dict[str, int] = {
    "tab1": 0, "tab2": 1, "tab3": 2,
    "tab4": 3, "tab5": 4, "tab6": 5,
    "tab7": 6, "tab8": 7,
}

# Image filenames per section
_SECTION_IMG: dict[str, str] = {
    f"tab{i+1}": f"tab_{i+1:02d}.png" for i in range(8)
}

# Text colours / fonts used by content tags
_H1   = ("Segoe UI", 16, "bold")
_H2   = ("Segoe UI", 12, "bold")
_H3   = ("Segoe UI", 10, "bold")
_BODY = ("Segoe UI", 10)
_CODE = ("Consolas",  9)

_COL_H1      = "#714B67"
_COL_H2      = "#00A09D"
_COL_ACCENT  = "#5A3A52"
_COL_CODE_BG = "#F0EBF0"
_COL_NOTE    = "#6B7280"
_COL_WARN    = "#B45309"
_COL_OK      = "#15803D"


# ── HelpWindow ───────────────────────────────────────────────────────────────

class HelpWindow(tk.Toplevel):
    """
    Ventana de ayuda flotante.

    Parametros:
        parent_app : instancia de BackupApp — se usa para acceder al Notebook
                     y a la ventana raiz durante la captura de pantallas.
    """

    def __init__(self, parent_app) -> None:
        super().__init__(parent_app.root)
        self._app      = parent_app
        self._photos: list = []          # references to keep PhotoImage alive
        self._img_dir  = self._resolve_img_dir()
        self._current_section = "intro"

        self._configure()
        self._build_ui()
        self._load_section("intro")

        self.focus_set()

    # ── Setup ────────────────────────────────────────────────────────────────

    def _configure(self) -> None:
        self.title("Ayuda — Odoo Backup Tool")
        self.geometry("1150x740")
        self.minsize(800, 500)
        self.configure(bg="#F5F3F0")
        # Center over parent
        self.update_idletasks()
        pw = self._app.root.winfo_x()
        py = self._app.root.winfo_y()
        self.geometry(f"+{pw + 40}+{py + 30}")

    @staticmethod
    def _resolve_img_dir() -> str:
        """Return the screenshots directory next to the executable (or script)."""
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, _HELP_IMG_DIR_NAME)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Toolbar ──────────────────────────────────────────────────────────
        toolbar = tk.Frame(self, bg="#714B67", height=40)
        toolbar.pack(side="top", fill="x")
        toolbar.pack_propagate(False)

        tk.Label(
            toolbar, text="  Ayuda  ",
            font=("Segoe UI", 11, "bold"),
            bg="#714B67", fg="white",
        ).pack(side="left", pady=6)

        if _PIL_AVAILABLE:
            tk.Button(
                toolbar,
                text="Capturar pantallas",
                command=self._start_capture,
                bg="#00A09D", fg="white",
                font=("Segoe UI", 9, "bold"),
                relief="flat", cursor="hand2",
                padx=10, pady=4,
            ).pack(side="right", padx=8, pady=6)
        else:
            tk.Label(
                toolbar,
                text="Instale Pillow para capturas de pantalla",
                bg="#714B67", fg="#FFCCAA",
                font=("Segoe UI", 8),
            ).pack(side="right", padx=8)

        # ── Main pane ─────────────────────────────────────────────────────────
        pane = tk.PanedWindow(
            self, orient="horizontal",
            sashwidth=5, sashrelief="flat",
            bg="#C8BEC5",
        )
        pane.pack(fill="both", expand=True, padx=0, pady=0)

        # ── Sidebar ───────────────────────────────────────────────────────────
        sidebar = tk.Frame(pane, bg="#F0EBF0", width=200)
        pane.add(sidebar, minsize=160)

        tk.Label(
            sidebar, text="Secciones",
            font=("Segoe UI", 9, "bold"),
            bg="#F0EBF0", fg="#714B67",
            anchor="w", padx=10,
        ).pack(fill="x", pady=(10, 4))

        self._listbox = tk.Listbox(
            sidebar,
            font=("Segoe UI", 9),
            bg="#F0EBF0", fg="#3D2B39",
            selectbackground="#714B67",
            selectforeground="white",
            relief="flat", bd=0,
            activestyle="none",
            cursor="hand2",
        )
        self._listbox.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        for _, label in _SECTIONS:
            self._listbox.insert("end", f"  {label}")

        self._listbox.bind("<<ListboxSelect>>", self._on_section_select)
        # NOTE: selection_set is deferred until after _text is created below
        # to avoid firing <<ListboxSelect>> before the text widget exists.

        # ── Content area ──────────────────────────────────────────────────────
        content_frame = tk.Frame(pane, bg="#FAFAFA")
        pane.add(content_frame, minsize=500)

        self._text = tk.Text(
            content_frame,
            wrap="word",
            padx=28, pady=18,
            font=_BODY,
            bg="#FAFAFA",
            fg="#1F1B1E",
            relief="flat",
            state="disabled",
            cursor="arrow",
        )
        vsb = ttk.Scrollbar(content_frame, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._text.pack(fill="both", expand=True)

        self._setup_tags()

        # Mousewheel scroll
        self._text.bind("<Enter>", lambda e: self._text.bind_all(
            "<MouseWheel>", lambda ev: self._text.yview_scroll(-1 * (ev.delta // 120), "units")
        ))
        self._text.bind("<Leave>", lambda e: self._text.unbind_all("<MouseWheel>"))

        # Mark first item selected — deferred here so _text already exists
        # when the <<ListboxSelect>> event fires on Windows.
        self._listbox.selection_set(0)

    def _setup_tags(self) -> None:
        t = self._text
        t.tag_configure("h1",      font=_H1,   foreground=_COL_H1,     spacing1=14, spacing3=6)
        t.tag_configure("h2",      font=_H2,   foreground=_COL_H2,     spacing1=12, spacing3=4)
        t.tag_configure("h3",      font=_H3,   foreground=_COL_ACCENT, spacing1=10, spacing3=2)
        t.tag_configure("body",    font=_BODY,  spacing1=2,  spacing3=2)
        t.tag_configure("bold",    font=("Segoe UI", 10, "bold"))
        t.tag_configure("code",    font=_CODE,  background=_COL_CODE_BG, foreground="#4B1E6D", relief="flat")
        t.tag_configure("note",    font=_BODY,  foreground=_COL_NOTE, lmargin1=20, lmargin2=20)
        t.tag_configure("warn",    font=_BODY,  foreground=_COL_WARN, lmargin1=20, lmargin2=20)
        t.tag_configure("ok",      font=_BODY,  foreground=_COL_OK,   lmargin1=20, lmargin2=20)
        t.tag_configure("bullet",  font=_BODY,  lmargin1=20, lmargin2=30, spacing1=2)
        t.tag_configure("sep",     font=("Segoe UI", 4), foreground="#E0D8E0")
        t.tag_configure("img_cap", font=("Segoe UI", 8, "italic"), foreground=_COL_NOTE, justify="center", spacing1=2, spacing3=10)
        t.tag_configure("center",  justify="center")

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_section_select(self, _event=None) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        section_id = _SECTIONS[sel[0]][0]
        self._load_section(section_id)

    def _load_section(self, section_id: str) -> None:
        self._current_section = section_id
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._photos.clear()

        renderer = getattr(self, f"_section_{section_id}", None)
        if renderer:
            renderer()
        else:
            self._w("Seccion no disponible.", "note")

        self._text.configure(state="disabled")
        self._text.yview_moveto(0)

    # ── Write helpers ─────────────────────────────────────────────────────────

    def _w(self, text: str, *tags) -> None:
        self._text.insert("end", text, tags)

    def _wl(self, text: str = "", *tags) -> None:
        self._text.insert("end", text + "\n", tags)

    def _h1(self, text: str) -> None:
        self._wl(text, "h1")

    def _h2(self, text: str) -> None:
        self._wl("\n" + text, "h2")

    def _h3(self, text: str) -> None:
        self._wl(text, "h3")

    def _p(self, text: str) -> None:
        self._wl(text, "body")

    def _bullet(self, items: list[str]) -> None:
        for item in items:
            self._wl(f"  •  {item}", "bullet")
        self._wl()

    def _code(self, text: str) -> None:
        self._text.insert("end", f"  {text}  ", "code")
        self._wl()

    def _note(self, text: str) -> None:
        self._wl(f"ℹ  {text}", "note")

    def _warn(self, text: str) -> None:
        self._wl(f"⚠  {text}", "warn")

    def _ok(self, text: str) -> None:
        self._wl(f"✔  {text}", "ok")

    def _sep(self) -> None:
        self._wl("─" * 80, "sep")

    def _embed_image(self, filename: str, caption: str = "") -> None:
        """Insert a screenshot image inline if it exists on disk."""
        path = os.path.join(self._img_dir, filename)
        if not os.path.isfile(path):
            self._note(f"(Captura no disponible — use 'Capturar pantallas' para generarla)")
            return
        if not _PIL_AVAILABLE:
            return
        try:
            img = Image.open(path)
            # Scale to max 740 px wide keeping aspect ratio
            max_w = 740
            if img.width > max_w:
                ratio = max_w / img.width
                img = img.resize(
                    (max_w, int(img.height * ratio)),
                    Image.LANCZOS,
                )
            photo = ImageTk.PhotoImage(img)
            self._photos.append(photo)
            self._text.insert("end", "\n")
            self._text.image_create("end", image=photo, padx=0, pady=4)
            self._text.insert("end", "\n")
            if caption:
                self._wl(caption, "img_cap", "center")
        except Exception:
            self._note("(Error cargando imagen)")

    # ── Section content ──────────────────────────────────────────────────────

    def _section_intro(self) -> None:
        self._h1("Odoo Backup Tool")
        self._p(
            "Herramienta de escritorio (Python + tkinter) para hacer respaldo y restauracion "
            "de bases de datos e instancias Odoo Enterprise en servidores remotos via SSH. "
            "Soporta tres destinos de backup: maquina local, servidor remoto y Google Drive."
        )
        self._wl()
        self._h2("Que hace esta herramienta")
        self._bullet([
            "Conectarse a un servidor Odoo via SSH y crear un dump de la base de datos (SQL o pg_custom).",
            "Comprimir el filestore (adjuntos e imagenes) en un archivo ZIP.",
            "Transferir el backup a la maquina local, a otro servidor remoto, o a Google Drive.",
            "Generar un inventario JSON con el estado exacto del backup (tablas, archivos, modulos).",
            "Restaurar una base de datos e importar el filestore en un servidor de destino.",
            "Verificar la integridad del backup comparando el inventario con la instancia restaurada.",
            "Sincronizar addons desde repositorios Git privados (con soporte de submodulos).",
            "Explorar y administrar archivos en servidores remotos via SFTP.",
            "Terminal SSH interactiva integrada (xterm-256color, historial de comandos).",
        ])

        self._h2("Requisitos previos")
        self._h3("Maquina local (donde corre la herramienta)")
        self._bullet([
            "Python 3.10+ con los paquetes: paramiko, pillow.",
            "Para destino Google Drive: google-api-python-client, google-auth, google-auth-httplib2.",
            "Archivo JSON de Service Account de Google (solo si usa el destino Drive).",
        ])
        self._h3("Servidor remoto (origen del backup)")
        self._bullet([
            "Acceso SSH con usuario que pueda ejecutar pg_dump y leer el filestore.",
            "PostgreSQL cliente (pg_dump / pg_restore / psql) instalado.",
            "zip / unzip instalado (o sudo para instalarlo automaticamente).",
            "sshpass instalado en el origen si se usa destino 'Servidor remoto' (lo instala automaticamente).",
        ])

        self._h2("Flujo tipico de uso")
        steps = [
            ("1", "Conexion",      "Tab 1: conectarse al servidor origen via SSH."),
            ("2", "BD",            "Tab 2: seleccionar la base de datos a respaldar."),
            ("3", "Filestore",     "Tab 3: verificar la ruta del filestore."),
            ("4", "Destino",       "Tab 4: elegir donde guardar el backup (local, remoto o Drive)."),
            ("5", "Ejecutar",      "Tab 5: lanzar el backup — dump + filestore + transferencia."),
            ("6", "Inventario",    "Se genera automaticamente un inventario JSON junto al backup."),
            ("7", "Restauracion",  "Tab 6: seleccionar dump e inventario para restaurar."),
            ("8", "Verificacion",  "Tab 5 compara el inventario con la instancia restaurada."),
        ]
        for num, title, desc in steps:
            self._text.insert("end", f"  {num}. ", "bold")
            self._text.insert("end", f"{title}: ", ("bold",))
            self._wl(desc, "body")
        self._wl()
        self._note(
            "Cada operacion larga (dump, compresion, transferencia, restauracion) corre en "
            "segundo plano con un boton 'Cancelar' activo. Los procesos en el servidor usan "
            "nohup y continuan aunque se cierre la herramienta."
        )

    # ── Tab 1 ────────────────────────────────────────────────────────────────

    def _section_tab1(self) -> None:
        self._h1("Tab 1 — Conexion SSH al servidor origen")
        self._embed_image("tab_01.png", "Tab 1: pantalla de conexion SSH")

        self._h2("Proposito")
        self._p(
            "Establece la conexion SSH al servidor donde corre Odoo. Esta conexion es el punto "
            "de partida de todas las operaciones: backup, verificacion de filestore, consulta de bases de datos."
        )

        self._h2("Campos")
        self._bullet([
            "Host / IP — Direccion del servidor (ej. 62.84.178.182).",
            "Puerto — Puerto SSH (defecto 22; Bancasa usa 4322).",
            "Usuario — Usuario SSH con permisos sobre los datos de Odoo.",
            "Contrasena / Clave SSH — Autenticacion: contrasena directa o ruta a clave privada local (.pem, .key, ed25519).",
            "Passphrase — Si la clave privada esta cifrada, se solicita una sola vez y se guarda en memoria (nunca en disco).",
        ])

        self._h2("Comportamiento de la clave SSH")
        self._p("Al usar clave privada, la herramienta intenta cargarla con y sin passphrase.")
        self._bullet([
            "Si la clave no requiere passphrase, se conecta directamente.",
            "Si requiere passphrase, abre un dialogo para ingresarla.",
            "La passphrase se guarda en cache de sesion (variable en memoria) para no pedirla en cada operacion.",
            "Al cerrar la aplicacion, el cache se limpia automaticamente.",
        ])
        self._warn("La passphrase NUNCA se escribe en disco ni en archivos de configuracion.")

        self._h2("Bases de datos detectadas")
        self._p(
            "Al conectarse exitosamente, la herramienta lista todas las bases de datos PostgreSQL "
            "disponibles en el servidor. Se filtran las de sistema (postgres, template0, template1)."
        )

        self._h2("Que pasa internamente")
        self._bullet([
            "Se crea un cliente paramiko.SSHClient con autenticacion por clave o contrasena.",
            "Se verifica la conexion ejecutando 'echo ok' en el servidor.",
            "Se consulta PostgreSQL con 'psql -l' para listar las BDs.",
            "La conexion se mantiene activa durante toda la sesion de backup/restauracion.",
        ])

    # ── Tab 2 ────────────────────────────────────────────────────────────────

    def _section_tab2(self) -> None:
        self._h1("Tab 2 — Backup de base de datos")
        self._embed_image("tab_02.png", "Tab 2: configuracion del backup")

        self._h2("Proposito")
        self._p(
            "Configura y ejecuta el proceso completo de backup: dump de BD, compresion del filestore "
            "y transferencia al destino."
        )

        self._h2("Opciones de backup")
        self._bullet([
            "Base de datos — Selecciona cual BD respaldar (lista del servidor conectado en Tab 1).",
            "Incluir BD — Activa/desactiva el dump de la base de datos.",
            "Incluir Filestore — Activa/desactiva la compresion del filestore.",
            "Formato dump — SQL plano (psql) o Custom (pg_restore con paralelismo).",
        ])

        self._h2("Formatos de dump")
        self._h3("SQL plano (.sql)")
        self._bullet([
            "Genera un archivo .sql legible con todas las instrucciones CREATE y INSERT.",
            "Restauracion via psql — mas lento pero compatible con cualquier version de psql.",
            "Util para inspeccion manual del contenido.",
            "Tamano: mayor que el formato custom (sin compresion interna).",
        ])
        self._h3("Custom pg_dump (.dump)")
        self._bullet([
            "Formato binario comprimido propio de PostgreSQL.",
            "Restauracion via pg_restore — soporta restauracion en paralelo (-j N workers).",
            "Hasta 3-4x mas rapido en restauracion que SQL plano en BDs grandes.",
            "Tamano: significativamente menor gracias a la compresion interna.",
        ])
        self._note("Para producciones con BDs > 500 MB se recomienda el formato Custom.")

        self._h2("Verificacion de espacio previa")
        self._p(
            "Antes de iniciar el dump, la herramienta verifica que haya espacio suficiente en /tmp "
            "del servidor para el dump (estimado del tamano de la BD) y para el ZIP del filestore "
            "(estimado del tamano real del filestore en disco)."
        )
        self._warn(
            "Si /tmp no tiene suficiente espacio, la operacion se cancela con un mensaje claro "
            "indicando cuanto hay disponible y cuanto se necesita."
        )

        self._h2("Proceso paso a paso")
        steps = [
            "Verificacion de espacio en /tmp del servidor.",
            "pg_dump ejecutado en el servidor → /tmp/{bd}.sql o /tmp/{bd}.dump",
            "zip -1 -r del directorio filestore → /tmp/filestore_{bd}.zip",
            "Transferencia de ambos archivos al destino (Tab 4).",
            "Inventario JSON generado y guardado junto al dump.",
            "Limpieza de /tmp en el servidor (archivos temporales eliminados).",
        ]
        for i, step in enumerate(steps, 1):
            self._text.insert("end", f"  {i}. ", "bold")
            self._wl(step, "body")

        self._h2("Inventario generado")
        self._p(
            "Al finalizar el backup exitosamente, se genera un archivo {nombre_dump}_inventory.json "
            "que contiene el estado exacto de la instancia en ese momento:"
        )
        self._bullet([
            "Lista de tablas y conteo de registros por tabla.",
            "Tamano total de la BD.",
            "Lista de modulos Odoo instalados.",
            "Conteo y tamano total del filestore.",
            "Fecha, hora y servidor de origen.",
        ])
        self._ok(
            "Este inventario se usa en Tab 5 para verificar la integridad de la restauracion."
        )

    # ── Tab 3 ────────────────────────────────────────────────────────────────

    def _section_tab3(self) -> None:
        self._h1("Tab 3 — Explorador de Filestore")
        self._embed_image("tab_03.png", "Tab 3: explorador del filestore en el servidor")

        self._h2("Proposito")
        self._p(
            "Permite explorar el arbol de directorios del filestore en el servidor origen "
            "antes de hacer el backup. Util para verificar la ubicacion correcta y el "
            "tamano antes de iniciar la compresion."
        )

        self._h2("Deteccion automatica")
        self._p(
            "Al conectarse (Tab 1) y abrir este tab, la herramienta busca automaticamente "
            "el filestore en las rutas estandar de Odoo:"
        )
        self._bullet([
            "/opt/odoo/.local/share/Odoo/filestore",
            "/home/odoo/.local/share/Odoo/filestore",
            "/var/lib/odoo/filestore",
        ])
        self._note(
            "Si el filestore no esta en ninguna ruta estandar, puede ingresarlo manualmente "
            "y presionar 'Detectar' para verificar su existencia."
        )

        self._h2("Vista de arbol")
        self._p(
            "El arbol muestra los subdirectorios del filestore (uno por base de datos). "
            "Al expandir un nodo, se calcula el tamano de ese directorio via 'du -sh' "
            "en el servidor."
        )
        self._warn(
            "El calculo de tamano con du -sh puede tardar varios segundos en filestores grandes (>5 GB). "
            "Se ejecuta en segundo plano sin bloquear la interfaz."
        )

    # ── Tab 4 ────────────────────────────────────────────────────────────────

    def _section_tab4(self) -> None:
        self._h1("Tab 4 — Destino del Backup")
        self._embed_image("tab_04.png", "Tab 4: seleccion del destino del backup")

        self._h2("Proposito")
        self._p(
            "Define adonde se transfieren los archivos de backup (dump de BD y ZIP del filestore) "
            "una vez generados en /tmp del servidor origen. Hay tres opciones de destino."
        )

        # ── Destino 1: Local ──────────────────────────────────────────────────
        self._h2("Opcion 1 — Esta maquina (local)")
        self._p(
            "Los archivos se descargan a un directorio en la maquina donde corre la herramienta "
            "usando SFTP (paramiko)."
        )
        self._bullet([
            "Seleccionar carpeta destino con el boton 'Examinar...'.",
            "El inventario JSON se guarda en la misma carpeta junto al dump.",
            "Progreso de descarga visible en la barra de progreso de Tab 5.",
            "Si el archivo ya existe en la carpeta local, aparece el dialogo de sobreescritura.",
        ])
        self._ok("Destino recomendado para backups puntuales y acceso inmediato al archivo.")

        # ── Destino 2: Servidor remoto ────────────────────────────────────────
        self._h2("Opcion 2 — Otro servidor remoto")
        self._p(
            "Los archivos se transfieren directamente entre el servidor origen y el servidor destino "
            "usando sshpass + scp ejecutado en el propio servidor origen. La maquina local "
            "no necesita tener acceso de red directo al servidor destino."
        )
        self._h3("Campos requeridos")
        self._bullet([
            "IP / Hostname — direccion del servidor destino.",
            "Puerto SSH — puerto del servidor destino (defecto 22).",
            "Usuario — usuario SSH con permisos de escritura en el directorio destino.",
            "Contrasena — contrasena SSH del usuario en el servidor destino.",
            "Ruta remota — directorio en el servidor destino donde guardar los archivos (ej. /opt/backups).",
        ])
        self._h3("Perfiles guardados")
        self._p(
            "Las credenciales del servidor destino se pueden guardar como perfil con nombre "
            "para reutilizarlas sin volver a escribirlas. Los perfiles se comparten entre "
            "Tab 1 (origen), Tab 4 (destino) y Tab 6 (restauracion)."
        )
        self._note(
            "La transferencia usa sshpass + scp ejecutado en el servidor ORIGEN, no desde la maquina local. "
            "Esto permite topologias donde la maquina del usuario no puede alcanzar directamente el servidor destino."
        )
        self._warn(
            "Las contrasenas de perfil se guardan en texto plano en "
            "~/.odoo_backup_tool/servers.json. No use perfiles en maquinas compartidas."
        )
        self._p("Antes de transferir, la herramienta verifica en el servidor destino:")
        self._bullet([
            "Que el directorio exista (lo crea con mkdir -p si no existe).",
            "Que el usuario tenga permisos de escritura.",
            "Que haya espacio suficiente para el archivo (df -B1).",
        ])

        # ── Destino 3: Google Drive ───────────────────────────────────────────
        self._h2("Opcion 3 — Google Drive (Service Account)")
        self._p(
            "Los archivos se suben a una carpeta en Google Drive usando autenticacion de "
            "Service Account de Google Cloud. No requiere interaccion con el navegador ni "
            "autenticacion OAuth interactiva."
        )
        self._h3("Que es una Service Account")
        self._p(
            "Una Service Account es una cuenta de servicio de Google Cloud que permite que "
            "aplicaciones accedan a APIs de Google de forma automatica, sin que un usuario "
            "humano tenga que iniciar sesion. Se identifica con un archivo JSON que contiene "
            "sus credenciales (clave privada RSA)."
        )
        self._h3("Campos requeridos")
        self._bullet([
            "Archivo JSON — ruta al archivo de clave de la Service Account "
            "(descargado desde Google Cloud Console > IAM > Cuentas de servicio > Claves).",
            "ID de carpeta — ID de la carpeta en Google Drive donde guardar los backups. "
            "Se copia del final de la URL de la carpeta: "
            "drive.google.com/drive/folders/<ID_AQUI>",
        ])
        self._h3("Como preparar la Service Account")
        steps_sa = [
            "Ir a Google Cloud Console (console.cloud.google.com).",
            "Crear un proyecto (o usar uno existente) y habilitar la API de Google Drive.",
            "IAM y administracion > Cuentas de servicio > Crear cuenta de servicio.",
            "Dar un nombre (ej. odoo-backup-sa) y crear.",
            "En la cuenta de servicio: pestana 'Claves' > Agregar clave > JSON.",
            "Descargar el archivo JSON — es el que se ingresa en el campo 'Archivo JSON'.",
            "Compartir la carpeta de Drive con el email de la Service Account "
            "(client_email en el JSON) dando permiso 'Editor'.",
        ]
        for i, s in enumerate(steps_sa, 1):
            self._text.insert("end", f"  {i}. ", "bold")
            self._wl(s, "body")

        self._h3("Boton Verificar conexion")
        self._p(
            "Prueba las credenciales y el acceso a la carpeta sin subir ningun archivo. "
            "Muestra el nombre de la carpeta de destino si la conexion es exitosa."
        )
        self._h3("Flujo interno de la transferencia a Drive")
        steps_drive = [
            "El dump y el filestore ZIP se generan en /tmp del servidor origen (igual que siempre).",
            "Cada archivo se descarga del servidor a un directorio temporal local (via SFTP).",
            "El archivo temporal se sube a Drive con subida resumable (chunked, 5 MB por chunk).",
            "El archivo temporal local se elimina automaticamente al terminar la subida.",
            "El inventario JSON se sube a Drive junto con el backup.",
            "El inventario tambien se guarda localmente en ~/.odoo_backup_tool/inventories/.",
        ]
        for i, s in enumerate(steps_drive, 1):
            self._text.insert("end", f"  {i}. ", "bold")
            self._wl(s, "body")

        self._note(
            "La subida a Drive es resumable: si se corta la conexion a internet, la libreria "
            "de Google retoma el chunk donde se quedo automaticamente."
        )
        self._warn(
            "La clave privada del JSON de Service Account da acceso total a Google Drive. "
            "Guardela en un lugar seguro y no la suba a repositorios de codigo."
        )
        self._h3("Shared Drives (Unidades compartidas)")
        self._p(
            "Si el ID ingresado es el de una Shared Drive (unidad compartida de Google Workspace), "
            "la herramienta busca o crea automaticamente una subcarpeta 'Odoo Backups' dentro "
            "de esa unidad."
        )

        # ── Sobreescritura ────────────────────────────────────────────────────
        self._h2("Manejo de sobreescritura")
        self._p(
            "Para los destinos Local y Servidor remoto, si ya existe un archivo con el mismo "
            "nombre, aparece un dialogo con tres opciones:"
        )
        self._bullet([
            "Sobreescribir — el archivo existente se reemplaza completamente.",
            "Renombrar — se agrega un timestamp al nombre del nuevo archivo "
            "(ej. odoo_bancasa_prod_2026-06-30_10-37.sql). El archivo original NO se toca.",
            "Cancelar — se aborta el backup completo.",
        ])
        self._note(
            "En destino Drive no se verifica sobreescritura de nombre: Drive permite multiples "
            "archivos con el mismo nombre. Si se hacen backups diarios, cada uno crea un archivo nuevo."
        )

    # ── Tab 5 ────────────────────────────────────────────────────────────────

    def _section_tab5(self) -> None:
        self._h1("Tab 5 — Verificacion de Backup")
        self._embed_image("tab_05.png", "Tab 5: checklist de verificacion post-restauracion")

        self._h2("Proposito")
        self._p(
            "Compara el inventario generado en el momento del backup con el estado actual "
            "de la instancia restaurada. Produce un checklist con el resultado de cada verificacion."
        )

        self._h2("Que se verifica")
        self._bullet([
            "Conteo de tablas — numero de tablas en la BD restaurada vs el inventario.",
            "Tamano de la BD — comparacion de MB de la BD origen vs restaurada.",
            "Modulos instalados — lista de modulos Odoo instalados en origen vs restaurada.",
            "Filestore — conteo de archivos y tamano total del filestore restaurado.",
            "Conectividad — que la BD sea accesible y no este corrompida.",
        ])

        self._h2("Resultado por item")
        self._ok("CORRECTO — el valor restaurado coincide con el inventario (o diferencia < 5%).")
        self._warn("ADVERTENCIA — diferencia menor pero dentro de rango aceptable.")
        self._text.insert("end", "  ✖  ERROR — diferencia significativa que requiere atencion.\n", ("warn",))
        self._wl()

        self._note(
            "Diferencias pequenas son normales: la BD puede tener tablas de sesion temporales, "
            "y el filestore puede variar por archivos de cache regenerados automaticamente por Odoo."
        )

        self._h2("Como usar")
        self._bullet([
            "Completar una restauracion en Tab 6.",
            "Asegurarse de que el inventario este cargado (campo 'Inventario' en Tab 6).",
            "Ir a Tab 5 y presionar 'Verificar'.",
            "Revisar el checklist generado.",
        ])

    # ── Tab 6 ────────────────────────────────────────────────────────────────

    def _section_tab6(self) -> None:
        self._h1("Tab 6 — Restauracion")
        self._embed_image("tab_06.png", "Tab 6: configuracion de la restauracion")

        self._h2("Proposito")
        self._p(
            "Restaura una base de datos Odoo e importa el filestore en un servidor destino. "
            "Puede restaurar desde un dump local o desde archivos ya presentes en el servidor."
        )

        self._h2("Flujo de restauracion")
        steps = [
            "Seleccionar el dump (.sql o .dump) desde la maquina local.",
            "Seleccionar el ZIP del filestore (opcional).",
            "Cargar el inventario JSON para verificacion posterior (boton 'Auto' detecta automaticamente).",
            "Elegir el servidor destino: mismo servidor (origen) u 'Otro servidor'.",
            "Ingresar el nombre de la nueva base de datos.",
            "Presionar 'Restaurar'.",
        ]
        for i, step in enumerate(steps, 1):
            self._text.insert("end", f"  {i}. ", "bold")
            self._wl(step, "body")

        self._h2("Tipos de conexion destino")
        self._h3("Mismo servidor (origen)")
        self._p(
            "Usa la misma conexion SSH del Tab 1. Util para crear una BD de prueba en el "
            "mismo servidor de produccion."
        )
        self._h3("Otro servidor")
        self._p(
            "Abre campos adicionales para conectarse a un servidor diferente. "
            "El dump se transfiere al servidor destino antes de restaurar."
        )

        self._h2("Deteccion del inventario (boton Auto)")
        self._p("El boton 'Auto' busca el inventario en dos ubicaciones:")
        self._bullet([
            "Junto al dump — busca {nombre_dump}_inventory.json en el mismo directorio.",
            "Directorio de inventarios — C:\\Users\\{usuario}\\.odoo_backup_tool\\inventories\\",
        ])
        self._warn(
            "Si el dump fue renombrado durante la descarga (se agrego un timestamp), "
            "el inventario tiene el mismo timestamp en su nombre para que Auto lo encuentre."
        )

        self._h2("Proceso de restauracion del filestore")
        self._p("A partir de la version actual, el filestore se extrae directamente en la particion destino:")
        self._bullet([
            "Se crea un directorio de staging: {filestore_root}/{bd}_obt_tmp",
            "El ZIP se extrae en ese staging (misma particion que el destino final).",
            "Al terminar, un mv atomico renombra el staging al path definitivo.",
            "Si existe filestore previo, se guarda como {bd}_obt_prev antes del mv.",
            "Si el unzip falla, se limpia el staging y el filestore original queda intacto.",
        ])
        self._ok(
            "Esta estrategia requiere solo el espacio del filestore descomprimido, "
            "NO el doble (ya no se usa /var/tmp como intermediario)."
        )

        self._h2("Neutralizacion")
        self._p(
            "Opcionalmente, al restaurar en un servidor que NO sea produccion, "
            "se puede neutralizar la BD para evitar efectos secundarios:"
        )
        self._bullet([
            "Desactiva servidores de correo saliente.",
            "Desactiva acciones programadas (crons).",
            "Desactiva servidores de correo entrante (fetchmail).",
            "Intenta via 'odoo neutralize' (Enterprise v14+); si falla, aplica SQL directo.",
        ])
        self._warn(
            "Usar siempre neutralizacion al restaurar en entornos de prueba o desarrollo "
            "para evitar que la instancia restaurada envie correos reales o ejecute crons."
        )

    # ── Tab 7 ────────────────────────────────────────────────────────────────

    def _section_tab7(self) -> None:
        self._h1("Tab 7 — Sincronizacion de Addons / Git")
        self._embed_image("tab_07.png", "Tab 7: sincronizacion de addons desde repositorio Git")

        self._h2("Proposito")
        self._p(
            "Clona o actualiza el repositorio de addons personalizados en el servidor de destino "
            "usando Git. Maneja autenticacion SSH para repositorios privados (GitHub, GitLab, etc.)."
        )

        self._h2("Parametros principales")
        self._bullet([
            "URL del repositorio — URL SSH del repo (git@github.com:org/repo.git).",
            "Rama — branch a usar (main, develop, etc.).",
            "Directorio destino — ruta en el servidor donde clonar/actualizar.",
            "Usuario Odoo — para ajustar permisos al finalizar.",
        ])

        self._h2("Cascada de prioridad de clave SSH")
        self._p(
            "La autenticacion Git en el servidor sigue una cascada de tres modos. "
            "Se elige el primero disponible:"
        )

        self._h3("Modo 1 — Clave del servidor (prioridad alta)")
        self._bullet([
            "Busca claves SSH en ~/.ssh/ del SERVIDOR DESTINO.",
            "Lista las claves disponibles para seleccionar.",
            "No sube ninguna clave al servidor — usa la existente.",
            "Ideal para servidores que ya tienen una deploy key configurada en GitHub.",
        ])

        self._h3("Modo 2 — Clave local (subida temporal)")
        self._bullet([
            "Selecciona una clave privada de la MAQUINA LOCAL (donde corre la herramienta).",
            "La clave se descifra localmente (la passphrase nunca sale de la maquina local).",
            "Se sube cifrada a /tmp/.obt_gh_key en el servidor solo durante la operacion.",
            "Al finalizar (exitosa o no), la clave temporal se elimina del servidor.",
        ])

        self._h3("Modo 3 — Generar nueva clave")
        self._bullet([
            "Genera un nuevo par de claves Ed25519 en la MAQUINA LOCAL.",
            "La clave publica se muestra para que la agregues como deploy key en GitHub/GitLab.",
            "La clave privada se guarda en ~/.ssh/{nombre} de la maquina local.",
            "En el proximo uso, aparecera en la lista del Modo 2.",
        ])

        self._h2("Soporte de Git Submodules")
        self._p(
            "Si el repositorio usa submoudulos Git, activar la opcion 'Sincronizar submodulos' "
            "ejecuta la secuencia completa:"
        )
        self._code("git submodule sync --recursive")
        self._code("git submodule update --remote --recursive --merge")
        self._code("git submodule update --init --recursive")
        self._note(
            "El wrapper GIT_SSH se mantiene activo durante toda la secuencia de submodulos "
            "para que cada submodulo pueda autenticarse con la misma clave."
        )

        self._h2("Reinicio del servicio Odoo")
        self._p(
            "Si se activa la opcion 'Reiniciar servicio', al terminar la sincronizacion "
            "se ejecuta:"
        )
        self._code("sudo systemctl restart {nombre_servicio}")
        self._note("El nombre del servicio es configurable (por defecto: odoo).")

    # ── Tab 8 ────────────────────────────────────────────────────────────────

    def _section_tab8(self) -> None:
        self._h1("Tab 8 — Explorador de Archivos Remoto")
        self._embed_image("tab_08.png", "Tab 8: explorador dual de archivos en servidores remotos")

        self._h2("Proposito")
        self._p(
            "Explorador de archivos dual para navegar, inspeccionar y administrar directorios "
            "en uno o dos servidores remotos simultaneamente."
        )

        self._h2("Paneles")
        self._h3("Panel izquierdo — Servidor origen")
        self._bullet([
            "Siempre conectado al servidor configurado en Tab 1.",
            "Requiere que haya una conexion activa (Tab 1 conectado).",
        ])
        self._h3("Panel derecho — Servidor diferente")
        self._bullet([
            "Se conecta al servidor configurado en Tab 6 (Otro servidor) o Tab 4 (Destino remoto).",
            "NO puede mostrar el mismo servidor que el panel izquierdo (los paneles son independientes).",
            "Requiere que Tab 6 o Tab 4 tengan una conexion activa.",
        ])

        self._h2("Operaciones disponibles por panel")
        self._bullet([
            "Navegar — doble clic en directorio para entrar; boton '↑ Subir' para retroceder.",
            "Inicio — va al directorio home del usuario SSH.",
            "Actualizar — recarga el listado del directorio actual.",
            "Nueva carpeta — crea un directorio en la ruta actual.",
            "Eliminar — elimina el elemento seleccionado (archivo o directorio).",
            "Copiar nombre — copia al portapapeles el nombre del elemento seleccionado.",
            "Tamano — calcula el espacio de los directorios via 'du -sh' (puede tardar).",
        ])

        self._h2("Informacion mostrada por archivo/directorio")
        self._bullet([
            "Nombre del archivo o directorio.",
            "Tamano (para archivos: en tiempo real; para directorios: bajo demanda con 'Tamano').",
            "Fecha de ultima modificacion.",
            "Permisos unix (rwxrwxrwx).",
        ])

        self._warn(
            "La eliminacion de directorios es recursiva (equivalente a rm -rf). "
            "No hay confirmacion adicional. Asegurese de seleccionar el elemento correcto."
        )

        self._note(
            "El explorador es util para liberar espacio en el servidor antes de una restauracion, "
            "verificar que los archivos de backup llegaron correctamente, o inspeccionar "
            "el filestore de una instancia restaurada."
        )

    # ── Concepts ─────────────────────────────────────────────────────────────

    def _section_concepts(self) -> None:
        self._h1("Conceptos Clave")

        self._h2("Inventario de Backup")
        self._p(
            "El inventario es un archivo JSON generado automaticamente al final de cada backup. "
            "Su nombre sigue el patron: {nombre_dump}_inventory.json"
        )
        self._p("Contiene:")
        self._bullet([
            "server: IP del servidor origen.",
            "database: nombre de la BD.",
            "timestamp: fecha y hora exacta del backup.",
            "db_tables: diccionario {nombre_tabla: conteo_registros}.",
            "db_size_bytes: tamano total de la BD en bytes.",
            "installed_modules: lista de modulos Odoo instalados.",
            "filestore_file_count: numero total de archivos en el filestore.",
            "filestore_size_bytes: tamano total del filestore en bytes.",
        ])
        self._note(
            "El inventario es la base del checklist de verificacion (Tab 5). "
            "Sin inventario, la verificacion no puede realizarse."
        )

        self._sep()
        self._h2("Sobreescritura de archivos")
        self._p(
            "Cuando se transfiere un backup y ya existe un archivo con el mismo nombre, "
            "la herramienta ofrece tres opciones:"
        )
        self._bullet([
            "Sobreescribir — el archivo destino se REEMPLAZA completamente. "
            "No es una fusion: el contenido anterior se pierde.",
            "Renombrar — se conserva el archivo original y el nuevo recibe un sufijo de timestamp.",
            "Cancelar — se aborta toda la operacion de backup.",
        ])
        self._warn(
            "La sobreescritura no es reversible. Si necesitas conservar el backup anterior, "
            "usa 'Renombrar' o mueve el archivo manualmente antes de hacer el nuevo backup."
        )
        self._note(
            "En el caso de addons (Git), no aplica sobreescritura: Git maneja versiones "
            "por hash de commit. Un git pull actualiza el arbol de trabajo al estado del "
            "commit remoto, pero el historial se conserva intacto."
        )

        self._sep()
        self._h2("Formato de dump SQL plano vs Custom")
        self._bullet([
            "SQL plano (.sql): texto legible, restauracion con psql, compatible con cualquier version.",
            "Custom (.dump): binario comprimido, restauracion con pg_restore, soporte paralelo (-j).",
        ])
        self._note(
            "BDs < 100 MB: cualquier formato es igual de rapido. "
            "BDs > 500 MB: formato Custom puede ser 3-4x mas rapido en restauracion."
        )

        self._sep()
        self._h2("Cache de passphrase (seguridad)")
        self._p(
            "Cuando una clave SSH privada tiene passphrase, la herramienta la solicita una sola vez "
            "y la guarda en memoria (diccionario Python en la instancia de la app)."
        )
        self._bullet([
            "La passphrase NUNCA se escribe en disco.",
            "Se limpia automaticamente al cerrar la aplicacion.",
            "La clave privada se descifra localmente; el archivo que se sube al servidor "
            "(Modo 2 de Tab 7) ya no tiene passphrase (es el bytestream descifrado de paramiko).",
        ])
        self._ok("Este diseno garantiza que la passphrase no queda expuesta en archivos de configuracion ni logs.")

        self._sep()
        self._h2("nohup y sesiones SSH largas")
        self._p(
            "Los comandos largos (pg_dump, zip, unzip, pg_restore, git clone) se ejecutan "
            "envueltos en nohup en el servidor:"
        )
        self._code("nohup bash -c '{comando}' > /tmp/.obt_nohup.log 2>&1 &")
        self._p(
            "Esto permite que el proceso continue en el servidor aunque la conexion SSH se corte. "
            "La herramienta detecta la finalizacion verificando archivos sentinel:"
        )
        self._bullet([
            "/tmp/.obt_done_ok — creado cuando el comando termina exitosamente.",
            "/tmp/.obt_done_err — creado si el comando falla.",
            "/tmp/.obt_nohup.log — captura stdout y stderr del comando.",
        ])
        self._note(
            "Si la herramienta se cierra mientras un proceso corre, el proceso en el servidor "
            "continua (nohup lo desacopla del terminal). Al reconectar y ejecutar de nuevo, "
            "los archivos sentinel del proceso anterior se limpian antes de iniciar el nuevo."
        )

    # ── Full flow ─────────────────────────────────────────────────────────────

    def _section_full_flow(self) -> None:
        self._h1("Flujo Completo — Backup y Restauracion")

        self._h2("Escenario: Backup de produccion a servidor de pruebas")
        self._p(
            "Este es el flujo mas comun: respaldar bancasa_prod y restaurarla como "
            "bancasa_test en un servidor diferente."
        )

        self._h3("Fase 1 — Preparacion")
        steps = [
            "Abrir la herramienta.",
            "Tab 1: ingresar host, puerto y credenciales del servidor de PRODUCCION.",
            "Clic 'Conectar' — verificar que aparecen las BDs disponibles.",
            "Tab 4: elegir el destino del backup:",
        ]
        for i, s in enumerate(steps, 1):
            self._text.insert("end", f"  {i}. ", "bold")
            self._wl(s, "body")
        self._bullet([
            "Servidor remoto: ingresar datos del servidor de PRUEBAS.",
            "Esta maquina: seleccionar carpeta local con 'Examinar...'.",
            "Google Drive: seleccionar el JSON de Service Account e ID de carpeta.",
        ])

        self._h3("Fase 2 — Backup")
        steps = [
            "Tab 2: seleccionar 'bancasa_prod' en la lista de BDs.",
            "Marcar 'Incluir BD' y 'Incluir Filestore'.",
            "Elegir formato Custom (.dump) para maxima eficiencia.",
            "Clic 'Iniciar Backup'.",
            "Esperar a que aparezca 'COMPLETADO: Backup completado exitosamente'.",
            "El inventario se guarda automaticamente.",
        ]
        for i, s in enumerate(steps, 1):
            self._text.insert("end", f"  {i}. ", "bold")
            self._wl(s, "body")

        self._warn(
            "Para BDs grandes (> 5 GB), el proceso puede tardar 30-90 minutos. "
            "No cerrar la herramienta; el boton Cancelar esta disponible si es necesario."
        )

        self._h3("Fase 3 — Restauracion")
        steps = [
            "Tab 6: en 'Archivo dump' seleccionar el .dump descargado o la ruta en el servidor.",
            "En 'Filestore ZIP' seleccionar el .zip.",
            "Clic 'Auto' en Inventario — deberia detectarlo automaticamente.",
            "Ingresar nombre de la nueva BD: 'bancasa_test_YYYY_MM_DD'.",
            "Elegir 'Otro servidor' y conectarse al servidor de pruebas.",
            "Activar 'Neutralizar' para evitar crons y mails en el entorno de pruebas.",
            "Clic 'Restaurar'.",
        ]
        for i, s in enumerate(steps, 1):
            self._text.insert("end", f"  {i}. ", "bold")
            self._wl(s, "body")

        self._h3("Fase 4 — Verificacion")
        steps = [
            "Tab 5: clic 'Verificar'.",
            "Revisar el checklist: tablas, tamano BD, modulos, filestore.",
            "Si todos los items son CORRECTO, la restauracion fue exitosa.",
            "Si hay ERRORES, revisar el log para identificar el problema.",
        ]
        for i, s in enumerate(steps, 1):
            self._text.insert("end", f"  {i}. ", "bold")
            self._wl(s, "body")

        self._ok("Restauracion verificada y lista para usar.")

        self._h3("Fase 5 — Addons (opcional)")
        steps = [
            "Tab 7: ingresar URL SSH del repositorio de addons.",
            "Seleccionar modo de clave SSH (servidor/local/nueva).",
            "Activar 'Sincronizar submodulos' si el repo los usa.",
            "Ingresar directorio destino en el servidor de pruebas.",
            "Clic 'Sincronizar'.",
            "Activar 'Reiniciar servicio' si se quiere que Odoo recargue los modulos.",
        ]
        for i, s in enumerate(steps, 1):
            self._text.insert("end", f"  {i}. ", "bold")
            self._wl(s, "body")

    # ── FAQ ───────────────────────────────────────────────────────────────────

    def _section_faq(self) -> None:
        self._h1("Preguntas Frecuentes")

        qa_list = [
            (
                "El boton 'Auto' del inventario no encuentra el archivo",
                "Ocurre cuando el dump fue renombrado con timestamp durante la descarga y el "
                "inventario tiene el nombre original. Desde la version actual esto se corrigio: "
                "el inventario ahora toma el nombre del archivo realmente descargado. "
                "Si el problema persiste, usa el boton '...' para seleccionar el inventario manualmente."
            ),
            (
                "El unzip del filestore falla con error vacio",
                "La causa mas comun es falta de espacio en la particion. La herramienta ahora "
                "hace un chequeo previo y muestra cuanto espacio hay vs cuanto necesita. "
                "Usa Tab 8 (explorador) para liberar espacio en el servidor de restauracion "
                "y vuelve a intentar."
            ),
            (
                "La restauracion falla a mitad del proceso. Tengo que empezar de cero?",
                "Depende del paso donde fallo:\n"
                "  • Si fallo en el dump o la transferencia: los archivos de backup ya estan en el destino. "
                "Solo necesitas volver a Tab 6 y restaurar.\n"
                "  • Si fallo en el unzip del filestore: el staging se limpio automaticamente. "
                "La BD se hizo rollback (se elimino). Puedes re-restaurar desde el mismo dump.\n"
                "  • El dump y ZIP en el servidor NO se eliminan al hacer rollback — siguen disponibles."
            ),
            (
                "La conexion SSH se corta durante el backup/restauracion",
                "Los procesos largos usan nohup en el servidor, por lo que continuan aunque "
                "se corte la conexion. Al reconectar y reintentar la operacion, la herramienta "
                "limpia los archivos sentinel anteriores y lanza el proceso de nuevo. "
                "Los archivos ya transferidos en /tmp permanecen hasta que se limpian explicitamente."
            ),
            (
                "Como agrego una deploy key de GitHub desde la herramienta?",
                "Tab 7 → Modo 3 'Generar nueva clave':\n"
                "  1. Escribe un nombre para la clave (ej. odoo_github_key).\n"
                "  2. Clic 'Generar' — aparece la clave publica en pantalla.\n"
                "  3. Copia esa clave publica.\n"
                "  4. En GitHub: Settings → Deploy keys → Add deploy key → pegar.\n"
                "  5. En el proximo backup, la clave aparece en el Modo 2 (local)."
            ),
            (
                "El panel derecho del explorador muestra el mismo contenido que el izquierdo",
                "El panel derecho se conecta al servidor de Tab 6 (Otro servidor) o Tab 4 (Destino). "
                "Ambos deben estar conectados ANTES de abrir el explorador. "
                "Si Tab 6 tiene 'Mismo servidor', el panel derecho no tendra un servidor diferente "
                "al que conectarse y mostrara error."
            ),
            (
                "Puedo hacer backup de varias BDs al mismo tiempo?",
                "No. La herramienta procesa una BD por vez. Para varias BDs, "
                "completa un backup, espera a que termine, y lanza el siguiente."
            ),
            (
                "Donde se guardan los inventarios cuando el destino es un servidor remoto?",
                "En el directorio local de inventarios de la herramienta:\n"
                "  C:\\Users\\{usuario}\\.odoo_backup_tool\\inventories\\\n"
                "Esto permite que el boton 'Auto' en Tab 6 los encuentre aunque el dump "
                "este en el servidor remoto."
            ),
            (
                "Como configuro el destino Google Drive?",
                "Necesitas tres cosas:\n"
                "  1. Una Service Account en Google Cloud Console con la API de Drive habilitada.\n"
                "  2. El archivo JSON de clave de esa Service Account (descargado desde "
                "     IAM > Cuentas de servicio > Claves > Agregar clave > JSON).\n"
                "  3. Una carpeta en Google Drive compartida con el email de la Service Account "
                "     (campo client_email en el JSON) con permiso 'Editor'.\n"
                "En Tab 4: seleccionar 'Google Drive (Service Account)', cargar el JSON con "
                "'Examinar...', pegar el ID de la carpeta (de la URL de Drive), "
                "y usar 'Verificar conexion' para confirmar antes del backup."
            ),
            (
                "La subida a Google Drive se corto a mitad. Se perdio el backup?",
                "No. La herramienta usa subida resumable (chunked) de la API de Google Drive. "
                "Si la conexion se interrumpe, la libreria reintenta automaticamente el chunk "
                "donde se quedo. Sin embargo, si la aplicacion se cierra completamente durante "
                "la subida, la sesion resumable expira y hay que volver a subir el archivo. "
                "El archivo en el servidor origen (/tmp) sigue disponible hasta que se limpie."
            ),
            (
                "El destino Google Drive no aparece en Tab 4",
                "Las librerias de Google no estan instaladas. Ejecuta en el entorno virtual:\n"
                "  pip install google-api-python-client google-auth google-auth-httplib2\n"
                "Si usas el ejecutable compilado (.exe), asegurate de recompilar con la "
                "version que incluye los hidden-imports de Google."
            ),
            (
                "El boton 'Verificar conexion' de Drive da error 404",
                "El ID de carpeta no existe o la Service Account no tiene acceso. Verifica:\n"
                "  • Que el ID fue copiado correctamente de la URL de Drive.\n"
                "  • Que la carpeta en Drive esta compartida con el email de la Service Account "
                "    (client_email en el JSON) con rol 'Editor' o superior.\n"
                "  • Que la API de Google Drive esta habilitada en el proyecto de Google Cloud."
            ),
        ]

        for q, a in qa_list:
            self._text.insert("end", f"\n  Q: {q}\n", ("bold", "h3"))
            self._text.insert("end", f"  A: {a}\n", "body")
            self._sep()

    # ── Screenshot capture ───────────────────────────────────────────────────

    def _start_capture(self) -> None:
        """Launch screenshot capture in a background thread."""
        if not _PIL_AVAILABLE:
            messagebox.showerror(
                "Pillow no disponible",
                "Instale Pillow para capturar pantallas:\n  pip install pillow",
                parent=self,
            )
            return
        os.makedirs(self._img_dir, exist_ok=True)
        threading.Thread(target=self._capture_all_tabs, daemon=True).start()

    def _capture_all_tabs(self) -> None:
        """
        Iterate through each notebook tab, take a screenshot of the main window,
        and save it to the help/img directory.

        Runs in a background thread; schedules UI updates via after().
        """
        nb   = self._app.nb
        root = self._app.root

        captured = 0
        errors: list[str] = []

        for section_id, tab_idx in _SECTION_TAB_INDEX.items():
            try:
                # Switch to the tab in the main window
                root.after(0, lambda idx=tab_idx: nb.select(idx))
                time.sleep(0.6)   # allow tab to fully render

                # Capture the full main window
                root.update_idletasks()
                x = root.winfo_rootx()
                y = root.winfo_rooty()
                w = root.winfo_width()
                h = root.winfo_height()

                img = ImageGrab.grab(bbox=(x, y, x + w, y + h))

                filename = _SECTION_IMG[section_id]
                path = os.path.join(self._img_dir, filename)
                img.save(path, "PNG")
                captured += 1
            except Exception as exc:
                errors.append(f"{section_id}: {exc}")

        # Reload current section on the main thread to show new screenshots
        def _finish():
            if errors:
                messagebox.showwarning(
                    "Capturas con errores",
                    f"Se capturaron {captured} pantallas con {len(errors)} errores:\n"
                    + "\n".join(errors),
                    parent=self,
                )
            else:
                messagebox.showinfo(
                    "Capturas completadas",
                    f"Se guardaron {captured} capturas en:\n{self._img_dir}",
                    parent=self,
                )
            # Bring help window to front and reload
            self.lift()
            self.focus_set()
            self._load_section(self._current_section)

        root.after(0, _finish)
