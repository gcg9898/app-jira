"""
JiraBoard Launcher - Panel de control principal.
Levanta Flask, Tray App y muestra estado en una ventana.
"""
import subprocess
import threading
import tkinter as tk
from tkinter import ttk
import sqlite3
import sys
import os
import webbrowser
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

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
    flask_proc = subprocess.Popen(
        [UV_CMD, "run", "--with", "flask", "--with", "requests", "--with", "selenium",
         "--with", "webdriver-manager", "--link-mode=copy", "python", "app.py"],
        cwd=str(BASE_DIR),
        stdout=log_fh, stderr=log_fh,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def start_tray():
    global tray_proc
    tray_proc = subprocess.Popen(
        [UV_CMD, "run", "--with", "keyboard", "--with", "pystray", "--with", "pillow",
         "--with", "requests", "--link-mode=copy", "python", "tray_app.py"],
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


class LauncherApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("JiraBoard - Panel de Control")
        self.root.configure(bg="#1a1a2e")
        self.root.resizable(False, True)

        w, h = 420, 620
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Title
        tk.Label(self.root, text="JiraBoard", bg="#1a1a2e", fg="#16c79a",
                 font=("Segoe UI", 18, "bold")).pack(pady=(16, 4))
        tk.Label(self.root, text="Panel de Control", bg="#1a1a2e", fg="#888",
                 font=("Segoe UI", 10)).pack(pady=(0, 16))

        # Status frame
        status_frame = tk.Frame(self.root, bg="#0f0f23", padx=16, pady=12,
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
        db_frame = tk.Frame(self.root, bg="#0f0f23", padx=16, pady=12,
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
        cred_frame = tk.Frame(self.root, bg="#0f0f23", padx=16, pady=12,
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

        # Buttons
        btn_frame = tk.Frame(self.root, bg="#1a1a2e")
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
        # Also kill any remaining python processes spawned by us
        try:
            subprocess.run(["taskkill", "/F", "/IM", "python.exe"],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           capture_output=True)
        except Exception:
            pass
        self.root.destroy()
        os._exit(0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    LauncherApp()
