"""
SshTerminalPanel — terminal SSH embebida estilo PuTTY.

Abre una sesion de shell interactiva real con paramiko.invoke_shell() y
renderiza la salida en un widget tk.Text de tema oscuro.  El usuario
escribe en la barra de entrada inferior y presiona Enter (o el boton
Enviar) para mandar el comando al servidor.

Notas de implementacion:
- invoke_shell() abre un PTY completo en el servidor; el servidor hace
  eco de lo que enviamos, incluye el prompt y control characters ANSI.
- Un hilo lector consume la salida del canal sin bloquear el GUI.
- Un poller de 50 ms en el hilo principal vacia la cola y actualiza Text.
- Los codigos de escape ANSI y los caracteres de control comunes se
  filtran con una expresion regular para una salida legible.
- El estado del shell persiste entre comandos (cd, variables, etc.)
  porque es la misma sesion PTY durante toda la vida del panel.
"""
from __future__ import annotations

import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable

# ── Paleta terminal ───────────────────────────────────────────────────────────
_BG        = "#0D1117"   # fondo muy oscuro (GitHub dark)
_FG        = "#C9D1D9"   # texto normal (blanco suavizado)
_FG_PROMPT = "#58A6FF"   # prompt / info (azul)
_FG_ERR    = "#FF7B72"   # error (rojo suave)
_FG_OK     = "#3FB950"   # exito (verde)
_CURSOR    = "#E6EDF3"   # cursor de texto
_SEL_BG    = "#264F78"   # seleccion
_FONT      = ("Consolas", 10)

# Regex que captura la mayoria de secuencias de escape ANSI/VT100,
# carriage returns, bell characters y OSC (set-title sequences).
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[mKHJABCDEFGhlisuTStl]"   # CSI sequences
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"      # OSC sequences
    r"|\x1b[>=\(\)NO78MH]"                      # single-char escapes
    r"|\r"                                       # carriage return
    r"|\x07"                                     # BEL
    r"|\x08",                                    # BS (backspace)
    re.ASCII,
)

# Tamano del PTY virtual (columnas x filas).
_PTY_COLS = 220
_PTY_ROWS = 50

# Keyword pattern for per-word terminal highlighting: INFO / WARNING / WARN / ERROR
_TERM_KW_RE = re.compile(
    r'\[(?:INFO|WARNING|WARN|ERROR)\]|\b(?:INFO|WARNING|WARN|ERROR)\b',
    re.IGNORECASE,
)


class SshTerminalPanel(ttk.Frame):
    """
    Panel de terminal SSH interactiva embebida.

    Parametros
    ----------
    parent     : widget padre tkinter
    get_ssh    : callable() -> SSHClient | None
    title      : texto del encabezado del panel
    on_status  : callable(str) -> muestra mensajes externos (barra de estado)
    """

    def __init__(
        self,
        parent,
        get_ssh: Callable,
        title: str = "",
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent, style="TFrame")
        self._get_ssh    = get_ssh
        self._title      = title
        self._on_status  = on_status or (lambda m: None)

        self._channel    = None           # paramiko.Channel
        self._connected  = False
        self._q: queue.Queue[str] = queue.Queue()

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Encabezado ───────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg="#714B67", height=28)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)

        tk.Label(
            hdr, text=f"  {self._title}",
            font=("Segoe UI", 9, "bold"),
            bg="#714B67", fg="#FFFFFF",
        ).pack(side="left", pady=4)

        # Botones de control en el encabezado (derecha → izquierda)
        tk.Button(
            hdr, text="Limpiar",
            font=("Segoe UI", 8),
            bg="#714B67", fg="#FFFFFF",
            relief="flat", cursor="hand2",
            activebackground="#8B5E80", activeforeground="#FFFFFF",
            command=self._clear_output,
        ).pack(side="right", padx=(0, 6), pady=2)

        tk.Button(
            hdr, text="Desconectar",
            font=("Segoe UI", 8),
            bg="#9B3030", fg="#FFFFFF",
            relief="flat", cursor="hand2",
            activebackground="#7A2020", activeforeground="#FFFFFF",
            command=self.disconnect,
        ).pack(side="right", padx=(0, 4), pady=2)

        tk.Button(
            hdr, text="Reconectar",
            font=("Segoe UI", 8),
            bg="#2D6A4F", fg="#FFFFFF",
            relief="flat", cursor="hand2",
            activebackground="#1B4332", activeforeground="#FFFFFF",
            command=self.connect,
        ).pack(side="right", padx=(0, 4), pady=2)

        # ── Area de salida (Text oscuro) ─────────────────────────────────────
        txt_frame = tk.Frame(self, bg=_BG)
        txt_frame.grid(row=1, column=0, sticky="nsew")
        txt_frame.columnconfigure(0, weight=1)
        txt_frame.rowconfigure(0, weight=1)

        self._text = tk.Text(
            txt_frame,
            bg=_BG, fg=_FG,
            font=_FONT,
            wrap="word",
            state="disabled",
            relief="flat",
            bd=0,
            cursor="arrow",
            insertbackground=_CURSOR,
            selectbackground=_SEL_BG,
            selectforeground=_FG,
        )
        vsb = ttk.Scrollbar(txt_frame, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=vsb.set)

        self._text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        # Tags de color para mensajes locales (no del servidor)
        self._text.tag_configure("info",   foreground=_FG_PROMPT)
        self._text.tag_configure("ok",     foreground=_FG_OK)
        self._text.tag_configure("error",  foreground=_FG_ERR)
        # Keyword highlight tags for log-level words in server output
        self._text.tag_configure("kw_info",    foreground="#58A6FF",
                                               font=("Consolas", 10, "bold"))
        self._text.tag_configure("kw_warn",    foreground="#FFD700",
                                               font=("Consolas", 10, "bold"))
        self._text.tag_configure("kw_error",   foreground="#FF7B72",
                                               font=("Consolas", 10, "bold"))
        self._text.tag_raise("kw_info")
        self._text.tag_raise("kw_warn")
        self._text.tag_raise("kw_error")

        # ── Barra de entrada ─────────────────────────────────────────────────
        input_bar = tk.Frame(self, bg="#1C2128")
        input_bar.grid(row=2, column=0, sticky="ew")
        input_bar.columnconfigure(1, weight=1)

        tk.Label(
            input_bar, text=" $ ",
            font=_FONT, bg="#1C2128", fg=_FG_PROMPT,
        ).grid(row=0, column=0)

        self._input_var = tk.StringVar()
        self._entry = tk.Entry(
            input_bar,
            textvariable=self._input_var,
            font=_FONT,
            bg="#1C2128", fg=_FG,
            insertbackground=_CURSOR,
            relief="flat",
            bd=4,
        )
        self._entry.grid(row=0, column=1, sticky="ew")
        self._entry.bind("<Return>",    self._send)
        self._entry.bind("<Up>",        self._history_prev)
        self._entry.bind("<Down>",      self._history_next)

        tk.Button(
            input_bar, text="Enviar",
            font=("Segoe UI", 8),
            bg="#00A09D", fg="#FFFFFF",
            relief="flat", cursor="hand2",
            activebackground="#007B78", activeforeground="#FFFFFF",
            command=self._send,
        ).grid(row=0, column=2, padx=(4, 2), pady=2)

        tk.Button(
            input_bar, text="Ctrl+C",
            font=("Segoe UI", 8),
            bg="#CC4444", fg="#FFFFFF",
            relief="flat", cursor="hand2",
            activebackground="#992222", activeforeground="#FFFFFF",
            command=self._send_interrupt,
        ).grid(row=0, column=3, padx=(0, 4), pady=2)

        # Historial de comandos
        self._cmd_history: list[str] = []
        self._history_idx: int       = -1

    # ── Conexion ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """
        Abre una sesion PTY interactiva con el servidor asociado.

        Si ya hay una sesion activa la cierra primero para reconectar.
        """
        if self._connected:
            self.disconnect()

        ssh = self._get_ssh()
        if ssh is None or not ssh.connected:
            self._append_local(
                "[Sin conexion SSH] Conectese al servidor primero.\n", "error"
            )
            self._on_status("Sin conexion SSH — no se puede abrir terminal.")
            return

        self._append_local(
            f"Conectando a {ssh.host}:{ssh.port} ...\n", "info"
        )

        def _open() -> None:
            try:
                # Abrir canal PTY interactivo
                channel = ssh._client.invoke_shell(
                    term="xterm-256color",
                    width=_PTY_COLS,
                    height=_PTY_ROWS,
                )
                channel.settimeout(0.0)   # recv no bloqueante
                self._channel   = channel
                self._connected = True
                self.after(0, lambda: self._append_local(
                    f"Conectado a {ssh.host}  (Ctrl+C para interrumpir un proceso)\n",
                    "ok",
                ))
                self.after(0, lambda: self._on_status(
                    f"Terminal conectada a {ssh.host}"
                ))
                self.after(0, self._entry.focus_set)
                # Iniciar hilo lector
                threading.Thread(
                    target=self._reader_thread, daemon=True
                ).start()
                # Iniciar poller de salida
                self.after(50, self._poll_output)
            except Exception as exc:
                self.after(0, lambda: self._append_local(
                    f"[Error al abrir terminal] {exc}\n", "error"
                ))

        threading.Thread(target=_open, daemon=True).start()

    def disconnect(self) -> None:
        """Cierra el canal PTY y detiene el hilo lector."""
        self._connected = False
        if self._channel:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None
        self._append_local("\n[Sesion cerrada]\n", "info")

    # ── Entrada ───────────────────────────────────────────────────────────────

    def _send(self, event=None) -> str:
        """Envia el contenido del Entry al canal SSH y actualiza historial."""
        cmd = self._input_var.get()
        self._input_var.set("")
        self._history_idx = -1

        if not self._connected or self._channel is None:
            self._append_local(
                "[No conectado] Use el boton 'Conectar' primero.\n", "error"
            )
            return "break"

        # Guardar en historial (evitar duplicados consecutivos)
        if cmd and (not self._cmd_history or self._cmd_history[-1] != cmd):
            self._cmd_history.append(cmd)

        try:
            self._channel.send(cmd + "\n")
        except Exception as exc:
            self._append_local(f"[Error de envio] {exc}\n", "error")
            self._connected = False

        return "break"

    def _send_interrupt(self) -> None:
        """Envia Ctrl+C (SIGINT) al proceso remoto en curso."""
        if self._connected and self._channel:
            try:
                self._channel.send("\x03")
            except Exception:
                pass

    def _history_prev(self, event=None) -> str:
        """Navega al comando anterior en el historial (flecha arriba)."""
        if not self._cmd_history:
            return "break"
        self._history_idx = max(0, self._history_idx - 1) \
            if self._history_idx >= 0 \
            else len(self._cmd_history) - 1
        self._input_var.set(self._cmd_history[self._history_idx])
        self._entry.icursor("end")
        return "break"

    def _history_next(self, event=None) -> str:
        """Navega al comando siguiente en el historial (flecha abajo)."""
        if self._history_idx < 0:
            return "break"
        self._history_idx += 1
        if self._history_idx >= len(self._cmd_history):
            self._history_idx = -1
            self._input_var.set("")
        else:
            self._input_var.set(self._cmd_history[self._history_idx])
            self._entry.icursor("end")
        return "break"

    # ── Lector de salida (hilo) ───────────────────────────────────────────────

    def _reader_thread(self) -> None:
        """
        Lee bytes del canal PTY en un hilo de fondo.

        Encola los fragmentos decodificados para que el poller GUI los
        consuma.  Sale cuando el canal se cierra o self._connected es False.
        """
        while self._connected and self._channel:
            try:
                if self._channel.recv_ready():
                    data = self._channel.recv(8192)
                    if data:
                        text = data.decode("utf-8", errors="replace")
                        self._q.put(text)
                    elif self._channel.closed:
                        break
                elif self._channel.closed:
                    break
                else:
                    time.sleep(0.02)
            except OSError:
                break
            except Exception:
                time.sleep(0.05)

        # Sesion cerrada por el servidor
        if self._connected:
            self._connected = False
            self._q.put("\n[Servidor cerro la conexion]\n")

    # ── Poller GUI ────────────────────────────────────────────────────────────

    def _poll_output(self) -> None:
        """
        Vacia la cola de salida y actualiza el widget Text (hilo GUI).

        Se reprograma a si mismo cada 50 ms mientras la sesion este activa.
        Continua vaciando la cola incluso despues de desconectarse para
        mostrar los ultimos fragmentos en transito.
        """
        batch: list[str] = []
        try:
            while True:
                batch.append(self._q.get_nowait())
        except queue.Empty:
            pass

        if batch:
            combined = _ANSI_RE.sub("", "".join(batch))
            if combined:
                self._text.config(state="normal")
                self._insert_highlighted(combined)
                # Limitar a 5000 lineas para no acumular memoria
                lines = int(self._text.index("end-1c").split(".")[0])
                if lines > 5000:
                    self._text.delete("1.0", f"{lines - 4800}.0")
                self._text.see("end")
                self._text.config(state="disabled")

        # Reprogramar si la sesion sigue activa (o si la cola no esta vacia)
        if self._connected or not self._q.empty():
            self.after(50, self._poll_output)

    # ── Utilidades ────────────────────────────────────────────────────────────

    def _insert_highlighted(self, text: str) -> None:
        """
        Inserta texto en el widget con INFO/WARNING/ERROR resaltados por palabra.
        Llamado con el widget en estado 'normal'.
        """
        pos = 0
        for m in _TERM_KW_RE.finditer(text):
            if m.start() > pos:
                self._text.insert("end", text[pos:m.start()])
            word = m.group(0).lower().strip("[]")
            if "error" in word:
                kw_tag = "kw_error"
            elif "warn" in word:
                kw_tag = "kw_warn"
            else:
                kw_tag = "kw_info"
            self._text.insert("end", m.group(0), kw_tag)
            pos = m.end()
        if pos < len(text):
            self._text.insert("end", text[pos:])

    def _append_local(self, msg: str, tag: str = "") -> None:
        """Inserta un mensaje local (no del servidor) en el area de salida."""
        self._text.config(state="normal")
        if tag:
            self._text.insert("end", msg, tag)
        else:
            self._text.insert("end", msg)
        self._text.see("end")
        self._text.config(state="disabled")

    def _clear_output(self) -> None:
        """Limpia el area de salida del terminal."""
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.config(state="disabled")
