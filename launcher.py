"""
JiraBoard Launcher - Panel de control principal.
Levanta Flask, Tray App y muestra estado en una ventana.
"""
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import sqlite3
import sys
import os
import webbrowser
import hashlib
import tempfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "board.db"
ENV_PATH = BASE_DIR / ".env"
UV_CMD = "uv"


def load_env():
    """Load .env file as dict."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def save_env(env_dict):
    """Write dict to .env file."""
    lines = [f"{k}={v}" for k, v in env_dict.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

# Processes
flask_proc = None
tray_proc = None
FLASK_LOG = BASE_DIR / "flask.log"


def start_flask():
    global flask_proc
    log_fh = open(FLASK_LOG, "a", encoding="utf-8", errors="replace")
    if getattr(sys, 'frozen', False):
        cmd = [sys.executable, "--mode=flask"]
    else:
        cmd = [UV_CMD, "run", "--with", "flask", "--with", "requests", "--with", "selenium",
               "--with", "webdriver-manager", "--link-mode=copy", "python", "app.py"]
    flask_proc = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=log_fh, stderr=log_fh,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def start_tray():
    global tray_proc
    if getattr(sys, 'frozen', False):
        cmd = [sys.executable, "--mode=tray"]
    else:
        cmd = [UV_CMD, "run", "--with", "keyboard", "--with", "pystray", "--with", "pillow",
               "--with", "requests", "--link-mode=copy", "python", "tray_app.py"]
    tray_proc = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def get_db_stats():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=1)
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        jira_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE jira_key != ''").fetchone()[0]
        manual_tasks = tasks - jira_tasks
        columns = conn.execute("SELECT COUNT(*) FROM columns").fetchone()[0]
        conn.close()
        return {"tasks": tasks, "jira": jira_tasks, "manual": manual_tasks, "columns": columns}
    except Exception:
        return {"tasks": 0, "jira": 0, "manual": 0, "columns": 0}


def check_flask():
    try:
        urlopen("http://127.0.0.1:5000/api/columns", timeout=1)
        return True
    except (URLError, OSError):
        return False


def check_tray():
    if tray_proc and tray_proc.poll() is None:
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# AUTO-UPDATE desde GitHub (sin token, descarga directa del repo)
# ═══════════════════════════════════════════════════════════════
GITHUB_REPO = "gcg9898/app-jira"
GITHUB_BRANCH = "master"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
GITHUB_EXE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/contents/dist/JiraBoard.exe?ref={GITHUB_BRANCH}"

if getattr(sys, 'frozen', False):
    _BUNDLE_DIR = Path(sys._MEIPASS)
else:
    _BUNDLE_DIR = Path(__file__).parent


def get_local_version():
    """Read bundled version.txt (generated at build time)."""
    vf = _BUNDLE_DIR / "version.txt"
    if vf.exists():
        try:
            return vf.read_text(encoding="utf-8").strip()
        except (UnicodeDecodeError, ValueError):
            try:
                return vf.read_text(encoding="utf-8-sig").strip()
            except Exception:
                return None
    return None


def get_remote_version():
    """Get latest commit short SHA from GitHub API."""
    import json
    try:
        req = Request(GITHUB_API_URL, headers={
            "User-Agent": "JiraBoard-Updater",
            "Accept": "application/vnd.github.v3+json",
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["sha"][:7]  # short hash to match version.txt
    except (URLError, HTTPError, OSError, KeyError):
        return None


def _get_exe_download_url():
    """Get the download_url for dist/JiraBoard.exe from GitHub Contents API."""
    import json
    try:
        req = Request(GITHUB_EXE_URL, headers={
            "User-Agent": "JiraBoard-Updater",
            "Accept": "application/vnd.github.v3+json",
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("download_url")
    except (URLError, HTTPError, OSError, KeyError):
        return None


def download_update(progress_cb=None):
    """Download new exe from GitHub repo. Returns path or None."""
    if not getattr(sys, 'frozen', False):
        return None

    exe_url = _get_exe_download_url()
    if not exe_url:
        return None

    exe_dir = Path(sys.executable).parent
    update_path = exe_dir / "JiraBoard_update.exe"
    log_path = exe_dir / "_update.log"

    def _log(msg):
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                from datetime import datetime
                lf.write(f"[{datetime.now().isoformat()}] {msg}\n")
        except Exception:
            pass

    _log(f"=== INICIO DESCARGA ===")
    _log(f"URL: {exe_url}")
    _log(f"Destino: {update_path}")

    try:
        req = Request(exe_url, headers={"User-Agent": "JiraBoard-Updater"})
        with urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            _log(f"Content-Length: {total}")
            downloaded = 0
            with open(update_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total > 0:
                        progress_cb(downloaded, total)
            _log(f"Descargados: {downloaded} bytes")

        # Validate downloaded file is a real PE executable
        with open(update_path, "rb") as f:
            header = f.read(2)
        _log(f"Header: {header}")
        if header != b"MZ":
            _log("ERROR: Header no es MZ — archivo corrupto")
            update_path.unlink()
            return None

        file_size = update_path.stat().st_size
        _log(f"Archivo guardado OK: {file_size} bytes")

        # Remove Zone.Identifier IMMEDIATELY after download
        try:
            subprocess.run(
                ["powershell", "-Command", f'Unblock-File -Path "{update_path}"'],
                creationflags=subprocess.CREATE_NO_WINDOW,
                capture_output=True, timeout=10
            )
            _log("Unblock-File ejecutado OK")
        except Exception as e:
            _log(f"Unblock-File fallo: {e}")

        return update_path
    except (URLError, HTTPError, OSError) as e:
        _log(f"ERROR descarga: {e}")
        if update_path.exists():
            update_path.unlink()
        return None


def apply_update_and_restart():
    """Create a batch script that replaces the exe and restarts."""
    if not getattr(sys, 'frozen', False):
        return
    exe_path = Path(sys.executable)
    update_path = exe_path.parent / "JiraBoard_update.exe"
    if not update_path.exists():
        return

    # Remove Windows "downloaded from internet" block before anything else
    try:
        subprocess.run(
            ["powershell", "-Command", f'Unblock-File -Path "{update_path}"'],
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True, timeout=10
        )
    except Exception:
        pass

    bat_path = exe_path.parent / "_update.bat"
    log_path = exe_path.parent / "_update.log"
    bat_content = f'''@echo off
echo === INICIO ACTUALIZACION === > "{log_path}"
echo Fecha: %date% %time% >> "{log_path}"
echo Exe actual: {exe_path} >> "{log_path}"
echo Update: {update_path} >> "{log_path}"

REM Wait for old process to fully exit (up to 30 seconds)
set RETRIES=0
:waitloop
tasklist /FI "IMAGENAME eq {exe_path.name}" 2>NUL | find /I "{exe_path.name}" >NUL
if %errorlevel%==0 (
    set /a RETRIES+=1
    if %RETRIES% GEQ 30 (
        echo ERROR: Proceso no cerro tras 30s >> "{log_path}"
        goto cleanup
    )
    ping 127.0.0.1 -n 2 > nul
    goto waitloop
)
echo Proceso cerrado OK (intentos: %RETRIES%) >> "{log_path}"

REM Clean leftover _MEI temp folders from PyInstaller
echo Limpiando carpetas _MEI... >> "{log_path}"
for /d %%D in ("%TEMP%\\_MEI*") do (
    echo   Eliminando: %%D >> "{log_path}"
    rmdir /s /q "%%D" 2>nul
)
echo Limpieza _MEI completada >> "{log_path}"

REM Unblock downloaded file using PowerShell
echo Desbloqueando archivo descargado... >> "{log_path}"
powershell -Command "Unblock-File -Path '{update_path}'" >> "{log_path}" 2>&1

REM Try to delete old exe (retry up to 15 times for OneDrive locks)
set RETRIES=0
:delloop
del /F "{exe_path}" 2>nul
if exist "{exe_path}" (
    set /a RETRIES+=1
    if %RETRIES% GEQ 15 (
        echo ERROR: No se pudo eliminar exe antiguo tras 15 intentos >> "{log_path}"
        goto cleanup
    )
    ping 127.0.0.1 -n 2 > nul
    goto delloop
)
echo Exe antiguo eliminado OK (intentos: %RETRIES%) >> "{log_path}"

REM Copy new exe using binary copy (more reliable than move on OneDrive)
echo Copiando update a destino... >> "{log_path}"
copy /B /Y "{update_path}" "{exe_path}" >nul 2>>"{log_path}"
if errorlevel 1 (
    echo ERROR: copy fallo >> "{log_path}"
    goto cleanup
)
echo Copy completado >> "{log_path}"

REM Verify sizes match
for %%A in ("{update_path}") do set SRC_SIZE=%%~zA
for %%A in ("{exe_path}") do set DST_SIZE=%%~zA
echo Tamano origen: %SRC_SIZE% destino: %DST_SIZE% >> "{log_path}"
if not "%SRC_SIZE%"=="%DST_SIZE%" (
    echo ERROR: Tamanos no coinciden >> "{log_path}"
    goto cleanup
)

REM Delete the update copy
del /F "{update_path}" 2>nul

REM Unblock final exe too
echo Desbloqueando exe final... >> "{log_path}"
powershell -Command "Unblock-File -Path '{exe_path}'" >> "{log_path}" 2>&1

echo Lanzando exe actualizado... >> "{log_path}"

REM Launch updated exe
start "" "{exe_path}"
echo Exe lanzado OK >> "{log_path}"

:cleanup
del "%~f0"
'''
    bat_path.write_text(bat_content, encoding="utf-8")
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat_path)],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    os._exit(0)


class LauncherApp:
    def __init__(self):
        self.root = tk.Tk()
        local_ver = get_local_version() or "dev"
        self.root.title(f"JiraBoard - Panel de Control (v{local_ver})")
        self.root.configure(bg="#1a1a2e")
        self.root.resizable(False, True)

        w = 440
        # Get usable work area (excludes taskbar) for the primary monitor
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
            work_h = rect.bottom - rect.top
            work_w = rect.right - rect.left
        except Exception:
            work_h = self.root.winfo_screenheight() - 80
            work_w = self.root.winfo_screenwidth()

        h = min(620, work_h - 20)
        x = (work_w - w) // 2
        y = max(0, (work_h - h) // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(440, min(400, work_h - 20))
        self.root.maxsize(w, work_h)

        # ── Scrollable container ──────────────────────────────────────
        outer = tk.Frame(self.root, bg="#1a1a2e")
        outer.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(outer, bg="#1a1a2e", highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        # Inner frame that holds all widgets
        inner = tk.Frame(self._canvas, bg="#1a1a2e")
        self._canvas_window = self._canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(event):
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))

        def _on_canvas_resize(event):
            self._canvas.itemconfig(self._canvas_window, width=event.width)

        inner.bind("<Configure>", _on_inner_configure)
        self._canvas.bind("<Configure>", _on_canvas_resize)

        # Mouse wheel scroll (Windows)
        def _on_mousewheel(event):
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # Alias so all .pack() calls below go into inner
        root = inner

        # Title
        tk.Label(root, text="JiraBoard", bg="#1a1a2e", fg="#16c79a",
                 font=("Segoe UI", 18, "bold")).pack(pady=(16, 4))
        tk.Label(root, text="Panel de Control", bg="#1a1a2e", fg="#888",
                 font=("Segoe UI", 10)).pack(pady=(0, 16))

        # Status frame
        status_frame = tk.Frame(root, bg="#0f0f23", padx=16, pady=12,
                                highlightbackground="#2a2a4a", highlightthickness=1)
        status_frame.pack(fill="x", padx=20, pady=(0, 12))

        tk.Label(status_frame, text="Estado de servicios", bg="#0f0f23", fg="#e0e0e0",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        tk.Label(status_frame, text="Flask Server:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w")
        self.flask_status = tk.Label(status_frame, text="...", bg="#0f0f23",
                                     font=("Segoe UI", 9, "bold"))
        self.flask_status.grid(row=1, column=1, sticky="w", padx=(8, 0))
        tk.Button(status_frame, text="\u21bb", bg="#2a2a4a", fg="#e0e0e0",
                  font=("Segoe UI", 9), relief="flat", padx=6, cursor="hand2",
                  command=self.restart_flask).grid(row=1, column=2, padx=(8, 0))
        tk.Button(status_frame, text="Logs", bg="#2a2a4a", fg="#4A90D9",
                  font=("Segoe UI", 8), relief="flat", padx=4, cursor="hand2",
                  command=self.open_flask_logs).grid(row=1, column=3, padx=(4, 0))

        tk.Label(status_frame, text="Tray App:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w")
        self.tray_status = tk.Label(status_frame, text="...", bg="#0f0f23",
                                    font=("Segoe UI", 9, "bold"))
        self.tray_status.grid(row=2, column=1, sticky="w", padx=(8, 0))
        tk.Button(status_frame, text="\u21bb", bg="#2a2a4a", fg="#e0e0e0",
                  font=("Segoe UI", 9), relief="flat", padx=6, cursor="hand2",
                  command=self.restart_tray).grid(row=2, column=2, padx=(8, 0))

        tk.Label(status_frame, text="Hotkey crear tarea: Ctrl+Alt+N", bg="#0f0f23", fg="#4A90D9",
                 font=("Segoe UI", 8)).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # DB stats frame
        db_frame = tk.Frame(root, bg="#0f0f23", padx=16, pady=12,
                            highlightbackground="#2a2a4a", highlightthickness=1)
        db_frame.pack(fill="x", padx=20, pady=(0, 12))

        tk.Label(db_frame, text="Base de datos", bg="#0f0f23", fg="#e0e0e0",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        tk.Label(db_frame, text="Total tareas:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w")
        self.db_total = tk.Label(db_frame, text="...", bg="#0f0f23", fg="#16c79a",
                                 font=("Segoe UI", 9, "bold"))
        self.db_total.grid(row=1, column=1, sticky="w", padx=(8, 0))

        tk.Label(db_frame, text="Tareas Jira:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w")
        self.db_jira = tk.Label(db_frame, text="...", bg="#0f0f23", fg="#4A90D9",
                                font=("Segoe UI", 9, "bold"))
        self.db_jira.grid(row=2, column=1, sticky="w", padx=(8, 0))

        tk.Label(db_frame, text="Tareas manuales:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=3, column=0, sticky="w")
        self.db_manual = tk.Label(db_frame, text="...", bg="#0f0f23", fg="#f5a623",
                                  font=("Segoe UI", 9, "bold"))
        self.db_manual.grid(row=3, column=1, sticky="w", padx=(8, 0))

        tk.Label(db_frame, text="Columnas:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=4, column=0, sticky="w")
        self.db_cols = tk.Label(db_frame, text="...", bg="#0f0f23", fg="#e0e0e0",
                                font=("Segoe UI", 9, "bold"))
        self.db_cols.grid(row=4, column=1, sticky="w", padx=(8, 0))

        # Credentials frame
        cred_frame = tk.Frame(root, bg="#0f0f23", padx=16, pady=12,
                              highlightbackground="#2a2a4a", highlightthickness=1)
        cred_frame.pack(fill="x", padx=20, pady=(0, 12))

        tk.Label(cred_frame, text="Credenciales Jira", bg="#0f0f23", fg="#e0e0e0",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        env_data = load_env()

        tk.Label(cred_frame, text="Usuario:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w")
        self.user_entry = tk.Entry(cred_frame, bg="#0f0f23", fg="#e0e0e0", insertbackground="#16c79a",
                                   font=("Segoe UI", 9), relief="flat", highlightthickness=1,
                                   highlightcolor="#16c79a", highlightbackground="#2a2a4a", width=30)
        self.user_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=2)
        self.user_entry.insert(0, env_data.get("JIRA_USER", ""))

        tk.Label(cred_frame, text="Password:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w")
        self.pass_entry = tk.Entry(cred_frame, bg="#0f0f23", fg="#e0e0e0", insertbackground="#16c79a",
                                   font=("Segoe UI", 9), relief="flat", highlightthickness=1,
                                   highlightcolor="#16c79a", highlightbackground="#2a2a4a", width=30,
                                   show="*")
        self.pass_entry.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=2)
        self.pass_entry.insert(0, env_data.get("JIRA_PASS", ""))

        self.pass_visible = False
        self.eye_btn = tk.Button(cred_frame, text="\U0001F441", bg="#0f0f23", fg="#888",
                                 font=("Segoe UI", 10), relief="flat", borderwidth=0,
                                 cursor="hand2", command=self.toggle_password)
        self.eye_btn.grid(row=2, column=2, padx=(4, 0))

        tk.Label(cred_frame, text="URL Jira:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=3, column=0, sticky="w")
        self.url_entry = tk.Entry(cred_frame, bg="#0f0f23", fg="#e0e0e0", insertbackground="#16c79a",
                                  font=("Segoe UI", 9), relief="flat", highlightthickness=1,
                                  highlightcolor="#16c79a", highlightbackground="#2a2a4a", width=30)
        self.url_entry.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=2)
        self.url_entry.insert(0, env_data.get("JIRA_BASE_URL", ""))

        tk.Label(cred_frame, text="Filtro ID:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=4, column=0, sticky="w")
        self.filter_entry = tk.Entry(cred_frame, bg="#0f0f23", fg="#e0e0e0", insertbackground="#16c79a",
                                     font=("Segoe UI", 9), relief="flat", highlightthickness=1,
                                     highlightcolor="#16c79a", highlightbackground="#2a2a4a", width=12)
        self.filter_entry.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=2)
        self.filter_entry.insert(0, env_data.get("JIRA_FILTER_ID", ""))

        save_cred_btn = tk.Button(cred_frame, text="Guardar", bg="#16c79a", fg="#0f0f23",
                                  font=("Segoe UI", 9, "bold"), relief="flat", padx=10, pady=2,
                                  cursor="hand2", command=self.save_credentials)
        save_cred_btn.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # Update frame
        upd_frame = tk.Frame(root, bg="#0f0f23", padx=16, pady=12,
                             highlightbackground="#2a2a4a", highlightthickness=1)
        upd_frame.pack(fill="x", padx=20, pady=(0, 12))

        tk.Label(upd_frame, text="Actualizaciones", bg="#0f0f23", fg="#e0e0e0",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        local_ver = get_local_version() or "dev"
        tk.Label(upd_frame, text="Versión actual:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w")
        tk.Label(upd_frame, text=local_ver, bg="#0f0f23", fg="#16c79a",
                 font=("Segoe UI", 9, "bold")).grid(row=1, column=1, sticky="w", padx=(8, 0))

        self.update_status_label = tk.Label(upd_frame, text="", bg="#0f0f23", fg="#888",
                                            font=("Segoe UI", 8))
        self.update_status_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        self.update_progress = ttk.Progressbar(upd_frame, length=200, mode="determinate")
        self.update_progress.grid(row=3, column=0, columnspan=3, sticky="we", pady=(4, 0))
        self.update_progress.grid_remove()

        self.update_btn = tk.Button(upd_frame, text="\U0001F504 Comprobar actualizaciones",
                                    bg="#2a2a4a", fg="#e0e0e0",
                                    font=("Segoe UI", 9), relief="flat", padx=10, pady=4,
                                    cursor="hand2", command=self.check_for_updates)
        self.update_btn.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # Buttons
        btn_frame = tk.Frame(root, bg="#1a1a2e")
        btn_frame.pack(fill="x", padx=20, pady=(4, 16))

        tk.Button(btn_frame, text="Abrir Board Web", bg="#4A90D9", fg="#fff",
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=14, pady=6,
                  cursor="hand2",
                  command=lambda: webbrowser.open("http://localhost:5000")
                  ).pack(side="left", padx=(0, 8))

        tk.Button(btn_frame, text="Nueva Tarea (Ctrl+Alt+N)", bg="#16c79a", fg="#0f0f23",
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=self.open_new_task
                  ).pack(side="left", padx=(0, 8))

        tk.Button(btn_frame, text="Salir", bg="#e74c3c", fg="#fff",
                  font=("Segoe UI", 10), relief="flat", padx=14, pady=6,
                  cursor="hand2", command=self.quit_all
                  ).pack(side="right")

        # Start services
        threading.Thread(target=start_flask, daemon=True).start()
        threading.Thread(target=start_tray, daemon=True).start()

        # Update loop
        self.update_status()
        self.root.protocol("WM_DELETE_WINDOW", self.minimize)
        self.root.mainloop()

    def open_new_task(self):
        self.root.iconify()
        if getattr(sys, 'frozen', False):
            subprocess.Popen([sys.executable, "--mode=newtask"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.Popen([UV_CMD, "run", "--with", "requests", "--with", "keyboard",
                              "--with", "pystray", "--with", "Pillow",
                              "python", str(BASE_DIR / "tray_app.py"), "--popup"],
                             creationflags=subprocess.CREATE_NO_WINDOW)

    def update_status(self):
        threading.Thread(target=self._poll_status, daemon=True).start()

    def _poll_status(self):
        flask_ok = check_flask()
        tray_ok = check_tray()
        stats = get_db_stats()
        self.root.after(0, self._apply_status, flask_ok, tray_ok, stats)

    def _apply_status(self, flask_ok, tray_ok, stats):
        # Flask
        if flask_ok:
            self.flask_status.config(text="Corriendo", fg="#16c79a")
        else:
            self.flask_status.config(text="Detenido", fg="#e74c3c")

        # Tray
        if tray_ok:
            self.tray_status.config(text="Corriendo", fg="#16c79a")
        else:
            self.tray_status.config(text="Detenido", fg="#e74c3c")

        # DB
        self.db_total.config(text=str(stats["tasks"]))
        self.db_jira.config(text=str(stats["jira"]))
        self.db_manual.config(text=str(stats["manual"]))
        self.db_cols.config(text=str(stats["columns"]))

        self.root.after(5000, self.update_status)

    def restart_flask(self):
        global flask_proc
        if flask_proc and flask_proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(flask_proc.pid)],
                               creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
            except Exception:
                flask_proc.kill()
        threading.Thread(target=start_flask, daemon=True).start()

    def open_flask_logs(self):
        log_path = str(FLASK_LOG).replace("\\", "\\\\")
        cmd = f'start "Flask Logs" powershell -NoExit -Command "Get-Content -Path \'{log_path}\' -Tail 80 -Wait"'
        subprocess.Popen(cmd, shell=True)

    def restart_tray(self):
        global tray_proc
        if tray_proc and tray_proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(tray_proc.pid)],
                               creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
            except Exception:
                tray_proc.kill()
        threading.Thread(target=start_tray, daemon=True).start()

    def toggle_password(self):
        if self.pass_visible:
            self.pass_entry.config(show="*")
            self.eye_btn.config(fg="#888")
            self.pass_visible = False
        else:
            self.pass_entry.config(show="")
            self.eye_btn.config(fg="#16c79a")
            self.pass_visible = True

    def save_credentials(self):
        env_data = load_env()
        env_data["JIRA_USER"] = self.user_entry.get().strip()
        env_data["JIRA_PASS"] = self.pass_entry.get().strip()
        env_data["JIRA_BASE_URL"] = self.url_entry.get().strip() or "https://jiraitsm.eulen.com"
        env_data["JIRA_FILTER_ID"] = self.filter_entry.get().strip() or "30004"
        save_env(env_data)

    def check_for_updates(self):
        """Check GitHub for a newer version and offer to update."""
        self.update_btn.config(state="disabled", text="Comprobando...")
        self.update_status_label.config(text="Conectando con GitHub...", fg="#888")
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        local_ver = get_local_version()
        remote_ver = get_remote_version()
        self.root.after(0, self._handle_update_result, local_ver, remote_ver)

    def _handle_update_result(self, local_ver, remote_ver):
        self.update_btn.config(state="normal", text="\U0001F504 Comprobar actualizaciones")

        if remote_ver is None:
            self.update_status_label.config(
                text="No se pudo conectar con GitHub. Comprueba tu conexión.",
                fg="#e74c3c")
            return

        is_new = local_ver is None or local_ver != remote_ver

        if not getattr(sys, 'frozen', False):
            tag = "Nueva versión" if is_new else "Versión actual"
            self.update_status_label.config(
                text=f"{tag} {remote_ver} (actualización solo disponible en .exe)",
                fg="#f5a623" if is_new else "#16c79a")
            return

        if is_new:
            self.update_status_label.config(
                text=f"Nueva versión disponible: {remote_ver}",
                fg="#f5a623")
            if messagebox.askyesno("Actualización disponible",
                                   f"Hay una nueva versión ({remote_ver}).\n\n"
                                   "¿Descargar e instalar ahora?\n"
                                   "La aplicación se reiniciará automáticamente."):
                self._start_download()
        else:
            self.update_status_label.config(
                text=f"Ya tienes la última versión ({local_ver})",
                fg="#16c79a")
            if messagebox.askyesno("Reinstalar versión actual",
                                   f"Ya tienes la última versión ({local_ver}).\n\n"
                                   "¿Quieres descargarla de nuevo e instalarla?\n"
                                   "Esto puede solucionar errores de ejecución."):
                self._start_download()

    def _start_download(self):
        self.update_btn.config(state="disabled", text="Descargando...")
        self.update_progress.grid()
        self.update_progress["value"] = 0
        self.update_status_label.config(text="Descargando actualización...", fg="#4A90D9")
        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        def progress_cb(downloaded, total):
            pct = int(downloaded * 100 / total)
            self.root.after(0, self._update_download_progress, pct, downloaded, total)

        result = download_update(progress_cb)
        self.root.after(0, self._download_done, result)

    def _update_download_progress(self, pct, downloaded, total):
        self.update_progress["value"] = pct
        mb_down = downloaded / (1024 * 1024)
        mb_total = total / (1024 * 1024)
        self.update_status_label.config(
            text=f"Descargando... {mb_down:.1f} / {mb_total:.1f} MB ({pct}%)")

    def _download_done(self, update_path):
        self.update_progress.grid_remove()
        self.update_btn.config(state="normal", text="\U0001F504 Comprobar actualizaciones")

        if update_path is None:
            self.update_status_label.config(
                text="Error al descargar la actualización.", fg="#e74c3c")
            return

        self.update_status_label.config(text="Reiniciando...", fg="#16c79a")
        # Kill Flask and Tray before restarting
        for proc in [flask_proc, tray_proc]:
            if proc and proc.poll() is None:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   creationflags=subprocess.CREATE_NO_WINDOW,
                                   capture_output=True)
                except Exception:
                    pass
        apply_update_and_restart()

    def minimize(self):
        self.root.iconify()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_all(self):
        for proc in [flask_proc, tray_proc]:
            if proc and proc.poll() is None:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   creationflags=subprocess.CREATE_NO_WINDOW,
                                   capture_output=True)
                except Exception:
                    pass
        # Only kill loose python.exe when not running as compiled .exe
        if not getattr(sys, 'frozen', False):
            try:
                subprocess.run(["taskkill", "/F", "/IM", "python.exe"],
                               creationflags=subprocess.CREATE_NO_WINDOW,
                               capture_output=True)
            except Exception:
                pass
        self.root.destroy()
        os._exit(0)


def main():
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    LauncherApp()


if __name__ == "__main__":
    main()
