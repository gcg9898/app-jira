"""
Mini menu de sistema para crear tareas en Jira Board.
Hotkey global: Ctrl+Alt+N
"""
import threading
import tkinter as tk
from tkinter import ttk, filedialog
import requests
import keyboard
import pystray
from PIL import Image, ImageDraw, ImageGrab
import uuid
import os
import sys
import shutil
from pathlib import Path

import sqlite3

API_URL = "http://localhost:5000"
DB_PATH = Path(__file__).parent / "board.db"
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)


def get_columns():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=1)
        rows = conn.execute("SELECT id, name FROM columns ORDER BY position").fetchall()
        conn.close()
        return [{"id": r[0], "name": r[1]} for r in rows]
    except Exception:
        return []


def get_labels():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=1)
        rows = conn.execute("SELECT DISTINCT labels FROM tasks WHERE labels != ''").fetchall()
        conn.close()
        labels = set()
        for r in rows:
            for lbl in r[0].split(","):
                lbl = lbl.strip()
                if lbl:
                    labels.add(lbl)
        return sorted(labels)
    except Exception:
        return []


def create_task_api(column_id, title, description, priority, labels="", link="", screenshot=""):
    try:
        requests.post(f"{API_URL}/api/tasks", json={
            "column_id": column_id,
            "title": title,
            "description": description,
            "priority": priority,
            "labels": labels,
            "link": link,
            "screenshot": screenshot,
        }, timeout=3)
        return True
    except Exception:
        return False


class ScreenshotSelector:
    """Overlay fullscreen para recortar una zona de pantalla."""

    def __init__(self, callback):
        self.callback = callback
        self.start_x = 0
        self.start_y = 0
        self.rect = None

        self.root = tk.Tk()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-alpha", 0.3)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        self.root.config(cursor="crosshair")

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<Escape>", lambda e: self.cancel())

        self.root.mainloop()

    def on_press(self, event):
        self.start_x = event.x
        self.start_y = event.y
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y,
            outline="#16c79a", width=2
        )

    def on_drag(self, event):
        self.canvas.coords(self.rect, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event):
        x1 = min(self.start_x, event.x)
        y1 = min(self.start_y, event.y)
        x2 = max(self.start_x, event.x)
        y2 = max(self.start_y, event.y)
        self.root.destroy()

        if (x2 - x1) > 10 and (y2 - y1) > 10:
            import time
            time.sleep(0.3)
            img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
            filename = f"capture_{uuid.uuid4().hex[:8]}.png"
            filepath = SCREENSHOTS_DIR / filename
            img.save(str(filepath))
            self.callback(filename)
        else:
            self.callback(None)

    def cancel(self):
        self.root.destroy()
        self.callback(None)


class TaskPopup:
    def __init__(self):
        self.window = None
        self.attached_screenshot = ""

    def show(self):
        if self.window:
            try:
                if self.window.winfo_exists():
                    self.window.lift()
                    self.window.focus_force()
                    return
            except Exception:
                self.window = None

        self.attached_screenshot = ""
        self.window = tk.Tk()
        self.window.title("Nueva Tarea - Jira Board")
        self.window.configure(bg="#1a1a2e")
        self.window.attributes("-topmost", True)
        self.window.resizable(False, False)

        w, h = 440, 570
        x = (self.window.winfo_screenwidth() - w) // 2
        y = (self.window.winfo_screenheight() - h) // 2
        self.window.geometry(f"{w}x{h}+{x}+{y}")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", background="#1a1a2e", foreground="#e0e0e0", font=("Segoe UI", 9))
        style.configure("TCombobox", fieldbackground="#0f0f23", foreground="#e0e0e0",
                        background="#2a2a4a", selectbackground="#16c79a", selectforeground="#0f0f23",
                        arrowcolor="#e0e0e0")
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#0f0f23")],
                  foreground=[("readonly", "#e0e0e0")],
                  selectbackground=[("readonly", "#0f0f23")],
                  selectforeground=[("readonly", "#e0e0e0")])
        self.window.option_add("*TCombobox*Listbox.background", "#0f0f23")
        self.window.option_add("*TCombobox*Listbox.foreground", "#e0e0e0")
        self.window.option_add("*TCombobox*Listbox.selectBackground", "#16c79a")
        self.window.option_add("*TCombobox*Listbox.selectForeground", "#0f0f23")

        frame = tk.Frame(self.window, bg="#1a1a2e", padx=20, pady=12)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="Nueva Tarea", bg="#1a1a2e", fg="#16c79a",
                 font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 10))

        ttk.Label(frame, text="Columna:").pack(anchor="w")
        self.col_var = tk.StringVar()
        self.columns_data = get_columns()
        col_names = [c["name"] for c in self.columns_data] if self.columns_data else ["Por Hacer"]
        ttk.Combobox(frame, textvariable=self.col_var, values=col_names,
                     state="readonly", width=42).pack(fill="x", pady=(2, 6))
        self.col_var.set(col_names[0] if col_names else "")

        ttk.Label(frame, text="Titulo:").pack(anchor="w")
        self.title_entry = tk.Entry(frame, bg="#0f0f23", fg="#e0e0e0", insertbackground="#16c79a",
                                    font=("Segoe UI", 10), relief="flat", highlightthickness=1,
                                    highlightcolor="#16c79a", highlightbackground="#2a2a4a")
        self.title_entry.pack(fill="x", pady=(2, 6), ipady=3)

        ttk.Label(frame, text="Descripcion:").pack(anchor="w")
        self.desc_text = tk.Text(frame, bg="#0f0f23", fg="#e0e0e0", insertbackground="#16c79a",
                                 font=("Segoe UI", 9), relief="flat", height=3,
                                 highlightthickness=1, highlightcolor="#16c79a", highlightbackground="#2a2a4a")
        self.desc_text.pack(fill="x", pady=(2, 6))

        ttk.Label(frame, text="Prioridad:").pack(anchor="w")
        self.prio_var = tk.StringVar(value="Normal")
        ttk.Combobox(frame, textvariable=self.prio_var,
                     values=["Most Important", "Critical", "High", "Normal", "Low"],
                     state="readonly", width=42).pack(fill="x", pady=(2, 6))

        ttk.Label(frame, text="Etiqueta (existente o nueva):").pack(anchor="w")
        self.label_var = tk.StringVar()
        existing_labels = get_labels()
        ttk.Combobox(frame, textvariable=self.label_var,
                     values=existing_labels, width=42).pack(fill="x", pady=(2, 6))

        ttk.Label(frame, text="Link (opcional):").pack(anchor="w")
        self.link_entry = tk.Entry(frame, bg="#0f0f23", fg="#e0e0e0", insertbackground="#16c79a",
                                   font=("Segoe UI", 9), relief="flat", highlightthickness=1,
                                   highlightcolor="#16c79a", highlightbackground="#2a2a4a")
        self.link_entry.pack(fill="x", pady=(2, 6), ipady=3)

        ttk.Label(frame, text="Foto:").pack(anchor="w")
        photo_frame = tk.Frame(frame, bg="#1a1a2e")
        photo_frame.pack(fill="x", pady=(2, 6))
        tk.Button(photo_frame, text="Recortar pantalla", bg="#4A90D9", fg="#fff",
                  font=("Segoe UI", 9), relief="flat", padx=10, pady=3,
                  cursor="hand2", command=self.take_screenshot).pack(side="left", padx=(0, 8))
        tk.Button(photo_frame, text="Elegir archivo", bg="#2a2a4a", fg="#e0e0e0",
                  font=("Segoe UI", 9), relief="flat", padx=10, pady=3,
                  cursor="hand2", command=self.pick_file).pack(side="left", padx=(0, 8))
        self.photo_label = tk.Label(photo_frame, text="Sin foto", bg="#1a1a2e", fg="#666",
                                    font=("Segoe UI", 8))
        self.photo_label.pack(side="left")

        btn_frame = tk.Frame(frame, bg="#1a1a2e")
        btn_frame.pack(fill="x", pady=(12, 0))
        tk.Button(btn_frame, text="Cancelar", bg="#2a2a4a", fg="#e0e0e0",
                  font=("Segoe UI", 10), relief="flat", padx=16, pady=5,
                  cursor="hand2", command=self.close).pack(side="right", padx=(8, 0))
        tk.Button(btn_frame, text="Crear Tarea", bg="#16c79a", fg="#0f0f23",
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=16, pady=5,
                  cursor="hand2", command=self.save).pack(side="right")

        # Quick-access links
        links_frame = tk.Frame(frame, bg="#1a1a2e")
        links_frame.pack(fill="x", pady=(10, 0))
        tk.Button(links_frame, text="Abrir Board", bg="#1a1a2e", fg="#4A90D9",
                  font=("Segoe UI", 9, "underline"), relief="flat", padx=0, pady=2,
                  cursor="hand2", borderwidth=0,
                  command=lambda: __import__("webbrowser").open("http://localhost:5000")
                  ).pack(side="left", padx=(0, 16))
        tk.Button(links_frame, text="Abrir Jira", bg="#1a1a2e", fg="#4A90D9",
                  font=("Segoe UI", 9, "underline"), relief="flat", padx=0, pady=2,
                  cursor="hand2", borderwidth=0,
                  command=lambda: __import__("webbrowser").open("https://jiraitsm.eulen.com")
                  ).pack(side="left")

        self.window.bind("<Escape>", lambda e: self.close())
        self.window.after(150, self._force_focus)
        self.window.mainloop()

    def _force_focus(self):
        import ctypes
        hwnd = int(self.window.frame(), 16)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        self.window.focus_force()
        self.title_entry.focus_set()

    def take_screenshot(self):
        self.window.withdraw()
        self.window.after(400, self._do_screenshot)

    def _do_screenshot(self):
        def on_done(filename):
            if filename:
                self.attached_screenshot = filename
            if self.window:
                self.window.deiconify()
                self.window.attributes("-topmost", True)
                if filename:
                    self.photo_label.config(text=filename, fg="#16c79a")
        ScreenshotSelector(on_done)

    def pick_file(self):
        filepath = filedialog.askopenfilename(
            title="Seleccionar imagen",
            filetypes=[("Imagenes", "*.png *.jpg *.jpeg *.gif *.bmp"), ("Todos", "*.*")]
        )
        if filepath:
            filename = f"upload_{uuid.uuid4().hex[:8]}{Path(filepath).suffix}"
            dest = SCREENSHOTS_DIR / filename
            shutil.copy2(filepath, dest)
            self.attached_screenshot = filename
            self.photo_label.config(text=Path(filepath).name, fg="#16c79a")

    def save(self):
        title = self.title_entry.get().strip()
        if not title:
            self.title_entry.configure(highlightcolor="#e74c3c", highlightbackground="#e74c3c")
            return
        desc = self.desc_text.get("1.0", "end").strip()
        priority = self.prio_var.get()
        labels = self.label_var.get().strip()
        link = self.link_entry.get().strip()
        col_name = self.col_var.get()
        col_id = 1
        for c in self.columns_data:
            if c["name"] == col_name:
                col_id = c["id"]
                break
        success = create_task_api(col_id, title, desc, priority, labels, link, self.attached_screenshot)
        if success:
            self.close()
        else:
            self.photo_label.config(text="Error conexion!", fg="#e74c3c")

    def close(self):
        if self.window:
            self.window.destroy()
            self.window = None


def create_tray_icon():
    # Create a simple icon
    img = Image.new("RGB", (64, 64), "#1a1a2e")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([8, 8, 56, 56], radius=8, fill="#16c79a")
    draw.text((22, 18), "JB", fill="#0f0f23")
    return img


popup = TaskPopup()

# Queue to signal main thread to show popup
import queue
import time as _time
_show_queue = queue.Queue()
_last_request = 0


def request_popup():
    global _last_request
    now = _time.time()
    if now - _last_request < 1.5:
        return
    _last_request = now
    _show_queue.put(True)


def on_quit(icon, item):
    keyboard.unhook_all()
    icon.stop()
    os._exit(0)


def on_new_task(icon, item):
    request_popup()


def setup_tray():
    icon_image = create_tray_icon()
    menu = pystray.Menu(
        pystray.MenuItem("Nueva Tarea (Ctrl+Alt+N)", on_new_task, default=True),
        pystray.MenuItem("Abrir Board", lambda i, it: __import__("webbrowser").open("http://localhost:5000")),
        pystray.MenuItem("Salir", on_quit),
    )
    icon = pystray.Icon("JiraBoard", icon_image, "Jira Board", menu)
    return icon


def main():
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Register global hotkey (suppress=True evita que se propague y cambie idioma)
    keyboard.add_hotkey("ctrl+alt+n", request_popup, suppress=True)

    print("  Jira Board Tray - Activo")
    print("  Hotkey: Ctrl+Alt+N para crear tarea")
    print("  Click derecho en icono de bandeja para mas opciones")

    # Run tray icon in a background thread so main thread handles tkinter
    icon = setup_tray()
    tray_thread = threading.Thread(target=icon.run, daemon=True)
    tray_thread.start()

    # Main thread: poll queue and open tkinter popup when requested
    while True:
        try:
            _show_queue.get(timeout=0.3)
            popup.show()
        except queue.Empty:
            pass


if __name__ == "__main__":
    main()
