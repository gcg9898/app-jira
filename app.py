"""
JIRA BOARD - Tablero tipo Trello con sincronización Jira
Ejecutar: uv run --with flask --with requests app.py
"""
import os
import sys
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from flask import Flask, render_template, request, jsonify, send_from_directory

import requests as req_lib
import urllib3
from jira_daily import collect_today_changes, summary_prompt
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Frozen (PyInstaller) vs normal execution path resolution
if getattr(sys, 'frozen', False):
    _BUNDLE_DIR = Path(sys._MEIPASS)          # bundled read-only assets
    # Allow overriding data dir via env var (for running from TEMP after update)
    _env_data = os.environ.get("JIRABOARD_DATA_DIR")
    _DATA_DIR = Path(_env_data) if _env_data else Path(sys.executable).parent
else:
    _BUNDLE_DIR = Path(__file__).parent
    _DATA_DIR = Path(os.environ.get("JIRABOARD_DATA_DIR") or Path(__file__).parent)

app = Flask(__name__, template_folder=str(_BUNDLE_DIR / 'templates'))
DB_PATH = _DATA_DIR / "board.db"
SCREENSHOTS_DIR = _DATA_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
SCREENSHOT_LOG = _DATA_DIR / "screenshot_errors.log"

# Sync & screenshot progress tracking
_sync_progress = {
    "phase": "idle",       # idle | fetching | processing | screenshots | done
    "phase_text": "",
    "total": 0,
    "done": 0,
    "running": False,
    "current": ""
}
_sync_start_lock = threading.Lock()
_daily_prepare_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN JIRA (desde .env)
# ═══════════════════════════════════════════════════════════════
ENV_PATH = _DATA_DIR / ".env"


def _load_env():
    """Load key=value pairs from .env file."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


_env = _load_env()
# Valores del .env: se usan solo como semilla inicial de la tabla jira_instances
# la primera vez que arranca la app (ver _seed_jira_config). A partir de ahí la
# configuración vive en base de datos y se gestiona desde la UI.
JIRA_BASE_URL = _env.get("JIRA_BASE_URL", "https://jiraitsm.eulen.com")
FILTER_ID = _env.get("JIRA_FILTER_ID", "30004")
JIRA_USER = _env.get("JIRA_USER", "")
JIRA_PASS = _env.get("JIRA_PASS", "")

# Paleta para asignar color automáticamente a instancias nuevas
INSTANCE_COLORS = ["#4A90D9", "#E5A33D", "#7BC67B", "#C97BD9", "#D96B6B",
                   "#5FBFC4", "#B0A16B", "#8E8ED9"]


# ═══════════════════════════════════════════════════════════════
# BASE DE DATOS
# ═══════════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS columns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            jira_key TEXT DEFAULT '',
            jira_status TEXT DEFAULT '',
            priority TEXT DEFAULT 'Normal',
            priority_override TEXT DEFAULT '',
            labels TEXT DEFAULT '',
            last_comment TEXT DEFAULT '',
            screenshot TEXT DEFAULT '',
            jira_updated TEXT DEFAULT '',
            jira_column_id INTEGER DEFAULT NULL,
            column_override INTEGER DEFAULT 0,
            link TEXT DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            FOREIGN KEY (column_id) REFERENCES columns(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS column_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            FOREIGN KEY (column_id) REFERENCES columns(id) ON DELETE CASCADE,
            UNIQUE(column_id, label)
        );

        CREATE TABLE IF NOT EXISTS jira_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            base_url TEXT NOT NULL,
            username TEXT DEFAULT '',
            password TEXT DEFAULT '',
            color TEXT DEFAULT '#4A90D9',
            enabled INTEGER NOT NULL DEFAULT 1,
            position INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS jira_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            filter_id TEXT DEFAULT '',
            jql TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            position INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (instance_id) REFERENCES jira_instances(id) ON DELETE CASCADE
        );
    """)
    # Crear columnas por defecto si no existen
    existing = conn.execute("SELECT COUNT(*) FROM columns").fetchone()[0]
    if existing == 0:
        conn.executemany("INSERT INTO columns (name, position) VALUES (?, ?)", [
            ("En Progreso", 0),
            ("Esperando Respuesta Usuario", 1),
            ("Hecho", 2),
        ])
    conn.commit()
    conn.close()


init_db()

# Default column-filter configuration
DEFAULT_COLUMN_FILTERS = {
    "En Progreso": ["Abierto", "Abierta", "Open", "To Do", "Nuevo", "En Progreso", "In Progress", "En Desarrollo", "Respondido"],
    "Esperando Respuesta Usuario": ["En Espera de Usuario", "Esperando", "Waiting", "En Espera", "En Revisión", "In Review", "Under Review", "Review"],
    "Hecho": ["Cerrado", "Finalizado", "Resuelto", "Closed", "Done", "Desaparecidas del filtro"],
    "Sin Asignación": [],
}


def _apply_default_filters(conn):
    """Apply default filters to default columns. Creates columns if they don't exist.
       Does NOT remove user-created columns. Moves orphaned tasks to Sin Asignación."""
    for col_name, filters in DEFAULT_COLUMN_FILTERS.items():
        col = conn.execute("SELECT id FROM columns WHERE name = ?", (col_name,)).fetchone()
        if not col:
            max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) FROM columns").fetchone()[0]
            conn.execute("INSERT INTO columns (name, position, is_default) VALUES (?, ?, 1)",
                         (col_name, max_pos + 1))
            col_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            col_id = col["id"]
            conn.execute("UPDATE columns SET is_default = 1 WHERE id = ?", (col_id,))
        # Clear existing filters for this column and set defaults
        conn.execute("DELETE FROM column_filters WHERE column_id = ?", (col_id,))
        for label in filters:
            # Remove this label from any other column to avoid conflicts
            conn.execute("DELETE FROM column_filters WHERE label = ? AND column_id != ?", (label, col_id))
            conn.execute("INSERT OR IGNORE INTO column_filters (column_id, label) VALUES (?, ?)",
                         (col_id, label))
    # Move orphaned tasks (column_id doesn't exist) to "Sin Asignación"
    sin_asig = conn.execute("SELECT id FROM columns WHERE name = 'Sin Asignación'").fetchone()
    if sin_asig:
        conn.execute("""UPDATE tasks SET column_id = ?, column_override = 0
                        WHERE column_id NOT IN (SELECT id FROM columns)""",
                     (sin_asig["id"],))


# Migration: add screenshot column if missing (existing DB)
def migrate_db():
    conn = get_db()
    cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    if "screenshot" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN screenshot TEXT DEFAULT ''")
    if "jira_updated" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN jira_updated TEXT DEFAULT ''")
    if "link" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN link TEXT DEFAULT ''")
    if "priority_override" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN priority_override TEXT DEFAULT ''")
    if "jira_column_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN jira_column_id INTEGER DEFAULT NULL")
    if "column_override" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN column_override INTEGER DEFAULT 0")
    if "deleted" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN deleted INTEGER DEFAULT 0")
    if "jira_created" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN jira_created TEXT DEFAULT ''")
    if "jira_due_date" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN jira_due_date TEXT DEFAULT ''")
    if "jira_start_date" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN jira_start_date TEXT DEFAULT ''")
    if "jira_oleada" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN jira_oleada TEXT DEFAULT ''")
    if "custom_title" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN custom_title TEXT DEFAULT ''")
    # Categoria con la que se agrupa el tablero. Sale del campo configurado en
    # el filtro; si no hay ninguno, se dejan las etiquetas de Jira.
    if "jira_category" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN jira_category TEXT DEFAULT ''")
        # Las tareas ya importadas heredan sus etiquetas como categoria, para
        # que la barra de filtros no aparezca vacia hasta la siguiente sync.
        conn.execute("UPDATE tasks SET jira_category = COALESCE(labels, '')")
    # Origen de la tarea: qué instancia de Jira y qué filtro la trajeron.
    # NULL en tareas manuales y en las que existían antes de la multi-instancia
    # (esas se reasignan a la instancia por defecto en _seed_jira_config).
    if "jira_instance_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN jira_instance_id INTEGER DEFAULT NULL")
    if "jira_filter_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN jira_filter_id INTEGER DEFAULT NULL")
    # Add is_default flag to columns
    col_cols = [row[1] for row in conn.execute("PRAGMA table_info(columns)").fetchall()]
    if "is_default" not in col_cols:
        conn.execute("ALTER TABLE columns ADD COLUMN is_default INTEGER DEFAULT 0")
        # Mark existing default columns
        default_names = ("En Progreso", "Esperando Respuesta Usuario", "Hecho")
        conn.execute(
            f"UPDATE columns SET is_default = 1 WHERE name IN ({','.join('?' * len(default_names))})",
            default_names
        )
    # Create column_filters table if not exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS column_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            FOREIGN KEY (column_id) REFERENCES columns(id) ON DELETE CASCADE,
            UNIQUE(column_id, label)
        )
    """)
    # Assign default filters to default columns if no filters exist at all
    any_filters = conn.execute("SELECT COUNT(*) FROM column_filters").fetchone()[0]
    if any_filters == 0:
        _apply_default_filters(conn)
    # Create environments table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS environments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            position INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Create task_environments junction table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_environments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            env_id INTEGER NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (env_id) REFERENCES environments(id) ON DELETE CASCADE,
            UNIQUE(task_id, env_id)
        )
    """)
    # Create ticket_environments junction table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticket_environments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            env_id INTEGER NOT NULL,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE,
            FOREIGN KEY (env_id) REFERENCES environments(id) ON DELETE CASCADE,
            UNIQUE(ticket_id, env_id)
        )
    """)
    # Insert default environments if none exist
    any_envs = conn.execute("SELECT COUNT(*) FROM environments").fetchone()[0]
    if any_envs == 0:
        conn.executemany("INSERT INTO environments (name, position) VALUES (?, ?)", [
            ("INT", 0), ("PRE", 1), ("PROD", 2)
        ])
    # Tablas de configuración multi-Jira (para DBs creadas antes de esta versión)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jira_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            base_url TEXT NOT NULL,
            username TEXT DEFAULT '',
            password TEXT DEFAULT '',
            color TEXT DEFAULT '#4A90D9',
            enabled INTEGER NOT NULL DEFAULT 1,
            position INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jira_filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            filter_id TEXT DEFAULT '',
            jql TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            position INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (instance_id) REFERENCES jira_instances(id) ON DELETE CASCADE
        )
    """)
    # Campo de la incidencia del que se lee el estado. Vacio = 'status' estandar.
    # Permite usar un custom field cuando el flujo real del proyecto no se
    # refleja en el status de Jira (se elige desde el panel de configuracion).
    # category_field: campo del que sale la categoria con la que se agrupa y
    # filtra el tablero (vacio = se usan las etiquetas de Jira).
    filt_cols = [row[1] for row in conn.execute("PRAGMA table_info(jira_filters)").fetchall()]
    if "status_field" not in filt_cols:
        conn.execute("ALTER TABLE jira_filters ADD COLUMN status_field TEXT DEFAULT ''")
    if "category_field" not in filt_cols:
        conn.execute("ALTER TABLE jira_filters ADD COLUMN category_field TEXT DEFAULT ''")
    # El contexto solo se copia; ya no hay un destino de chat configurado.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_preferences'").fetchone():
        conn.execute("DELETE FROM app_preferences WHERE key = 'summary_target'")
    _seed_jira_config(conn)
    conn.commit()
    conn.close()


def _seed_jira_config(conn):
    """Crea la instancia y el filtro iniciales a partir del .env.

    Solo se ejecuta la primera vez (cuando no hay ninguna instancia). Las tareas
    Jira que ya existían se reasignan a esa instancia para que los enlaces y la
    detección de 'desaparecidas del filtro' sigan funcionando igual que antes.
    """
    if conn.execute("SELECT COUNT(*) FROM jira_instances").fetchone()[0] > 0:
        return
    if not JIRA_BASE_URL:
        return
    name = JIRA_BASE_URL.replace("https://", "").replace("http://", "").split("/")[0] or "Jira"
    conn.execute(
        """INSERT INTO jira_instances (name, base_url, username, password, color, enabled, position)
           VALUES (?, ?, ?, ?, ?, 1, 0)""",
        (name, JIRA_BASE_URL.rstrip("/"), JIRA_USER, JIRA_PASS, INSTANCE_COLORS[0])
    )
    inst_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if FILTER_ID:
        conn.execute(
            """INSERT INTO jira_filters (instance_id, name, filter_id, enabled, position)
               VALUES (?, ?, ?, 1, 0)""",
            (inst_id, f"Filtro {FILTER_ID}", FILTER_ID)
        )
        filt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    else:
        filt_id = None
    conn.execute(
        "UPDATE tasks SET jira_instance_id = ?, jira_filter_id = ? WHERE jira_key != '' AND jira_instance_id IS NULL",
        (inst_id, filt_id)
    )

migrate_db()


def _jira_instance_map(conn):
    """{id: {name, base_url, color}} para poder adjuntar el origen a cada tarea."""
    return {r["id"]: {"name": r["name"], "base_url": r["base_url"], "color": r["color"]}
            for r in conn.execute("SELECT id, name, base_url, color FROM jira_instances").fetchall()}


def _attach_origin(task_dict, imap):
    """Añade jira_url, jira_instance_name y jira_instance_color a una tarea.

    La URL se construye en el backend a propósito: antes estaba hardcodeada en
    tres plantillas y con varias instancias dejaría de ser válida.
    """
    inst = imap.get(task_dict.get("jira_instance_id"))
    key = task_dict.get("jira_key") or ""
    task_dict["jira_instance_name"] = inst["name"] if inst else ""
    task_dict["jira_instance_color"] = inst["color"] if inst else ""
    task_dict["jira_url"] = ""
    if inst and key:
        base_url = (inst["base_url"] or "").rstrip("/")
        try:
            parsed = urlsplit(base_url)
            if parsed.scheme in ("https", "http") and parsed.hostname and not (parsed.username or parsed.password):
                task_dict["jira_url"] = f"{base_url}/browse/{quote(key, safe='')}"
        except ValueError:
            pass
    return task_dict


# ═══════════════════════════════════════════════════════════════
# RUTAS - VISTAS
# ═══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("board.html")


@app.route("/search")
def search_page():
    return render_template("search.html")


@app.route("/recent")
def recent_page():
    return render_template("recent.html")


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    conn = get_db()
    imap = _jira_instance_map(conn)
    pattern = f"%{q}%"
    tasks = conn.execute("""
        SELECT t.*, c.name as column_name
          FROM tasks t
          JOIN columns c ON t.column_id = c.id
         WHERE COALESCE(t.deleted, 0) = 0
           AND (t.title LIKE ? OR t.custom_title LIKE ? OR t.description LIKE ?
            OR t.jira_key LIKE ? OR t.labels LIKE ?
            OR t.last_comment LIKE ? OR t.jira_status LIKE ?
            OR t.id IN (
                SELECT tk.task_id FROM tickets tk WHERE tk.title LIKE ?
            ))
         ORDER BY t.updated_at DESC
    """, (pattern, pattern, pattern, pattern, pattern, pattern, pattern, pattern)).fetchall()
    result = []
    for t in tasks:
        td = dict(t)
        tickets = conn.execute(
            "SELECT * FROM tickets WHERE task_id = ? ORDER BY position", (t["id"],)
        ).fetchall()
        override = td.get("priority_override", "") or ""
        if override:
            td["priority"] = override
        td["tickets"] = [dict(tk) for tk in tickets]
        _attach_origin(td, imap)
        result.append(td)
    conn.close()
    return jsonify(result)


@app.route("/api/screenshot-progress")
def screenshot_progress():
    return jsonify(_sync_progress)


def _local_summary_access():
    """El contexto privado no es accesible desde la LAN ni desde otra web."""
    hosts = {"localhost", "127.0.0.1", "::1"}
    if (request.remote_addr not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
            or urlsplit(request.host_url).hostname not in hosts):
        return jsonify({"error": "Abre JiraBoard en localhost para obtener el contexto."}), 403
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return jsonify({"error": "Origen no permitido."}), 403
    if request.method == "POST" and not request.is_json:
        return jsonify({"error": "Se requiere una petición JSON local."}), 415
    return None


def _daily_summary_jobs():
    conn = get_db()
    try:
        jobs = []
        resolved_statuses = [r["label"] for r in conn.execute("""
            SELECT cf.label FROM column_filters cf, columns c
             WHERE cf.column_id = c.id AND LOWER(c.name) = 'hecho'
        """)]
        for instance, _filters in get_jira_sources(conn):
            tasks = conn.execute("""
                SELECT t.jira_key, f.status_field, f.category_field,
                       f.name AS filter_name, f.filter_id
                  FROM tasks t, jira_filters f
                 WHERE t.jira_instance_id = ?
                   AND f.id = t.jira_filter_id AND f.instance_id = t.jira_instance_id
                   AND f.enabled = 1 AND COALESCE(t.deleted, 0) = 0
                   AND COALESCE(t.jira_key, '') <> ''
            """, (instance["id"],)).fetchall()
            if tasks:
                jobs.append({"instance": dict(instance), "tasks": [dict(t) for t in tasks],
                             "resolved_statuses": resolved_statuses})
        return jobs
    finally:
        conn.close()


@app.route("/api/daily-summary", methods=["POST"])
def prepare_daily_summary():
    denied = _local_summary_access()
    if denied is not None:
        return denied
    if _sync_progress["running"] and _sync_progress["phase"] in ("fetching", "processing"):
        return jsonify({"error": "Espera a que termine la importación de incidencias."}), 409
    if not _daily_prepare_lock.acquire(blocking=False):
        return jsonify({"error": "Ya se está preparando un resumen. Espera a que termine."}), 409
    try:
        report = collect_today_changes(_daily_summary_jobs(), _fetch_issues,
                                       _adf_to_text, _field_status_text)
        prompt = summary_prompt(report)
        if len(prompt.encode("utf-8")) > 2_000_000:
            return jsonify({"error": "El contexto es demasiado grande. Selecciona menos filtros para el resumen."}), 413
        response = jsonify({"ok": True, "report": report, "prompt": prompt})
        response.headers["Cache-Control"] = "no-store"
        return response
    finally:
        _daily_prepare_lock.release()


@app.route("/screenshots/<path:filename>")
def serve_screenshot(filename):
    response = send_from_directory(SCREENSHOTS_DIR, filename)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/upload-screenshot", methods=["POST"])
def upload_screenshot():
    """Receive a screenshot image from the tray app."""
    import base64
    data = request.json
    filename = data.get("filename", "capture.png")
    img_data = data.get("image_b64", "")
    if not img_data:
        return jsonify({"error": "No image"}), 400
    filepath = SCREENSHOTS_DIR / filename
    filepath.write_bytes(base64.b64decode(img_data))
    return jsonify({"ok": True, "filename": filename})


@app.route("/api/labels", methods=["GET"])
def get_labels():
    """Return all unique labels used across tasks."""
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT labels FROM tasks WHERE labels != ''").fetchall()
    conn.close()
    all_labels = set()
    for row in rows:
        for l in row["labels"].split(","):
            lt = l.strip()
            if lt:
                all_labels.add(lt)
    return jsonify(sorted(all_labels))


# ═══════════════════════════════════════════════════════════════
# API - COLUMNAS
# ═══════════════════════════════════════════════════════════════
@app.route("/api/columns", methods=["GET"])
def get_columns():
    sort_by = request.args.get("sort", "priority")  # priority | updated
    conn = get_db()
    imap = _jira_instance_map(conn)
    cols = conn.execute("SELECT * FROM columns ORDER BY position").fetchall()
    result = []
    for col in cols:
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE column_id = ? AND COALESCE(deleted, 0) = 0 ORDER BY position",
            (col["id"],)
        ).fetchall()
        tasks_list = []
        for t in tasks:
            tickets = conn.execute(
                "SELECT * FROM tickets WHERE task_id = ? ORDER BY position",
                (t["id"],)
            ).fetchall()
            task_dict = dict(t)
            if "screenshot" not in task_dict:
                task_dict["screenshot"] = ""
            if "jira_updated" not in task_dict:
                task_dict["jira_updated"] = ""
            # Use priority_override if set, otherwise jira priority
            override = task_dict.get("priority_override", "") or ""
            if override:
                task_dict["priority"] = override
            task_dict["tickets"] = []
            for tk in tickets:
                tk_dict = dict(tk)
                tk_envs = conn.execute(
                    "SELECT env_id FROM ticket_environments WHERE ticket_id = ?", (tk["id"],)
                ).fetchall()
                tk_dict["environments"] = [row["env_id"] for row in tk_envs]
                task_dict["tickets"].append(tk_dict)
            # Get checked environments for this task
            task_envs = conn.execute(
                "SELECT env_id FROM task_environments WHERE task_id = ?", (t["id"],)
            ).fetchall()
            task_dict["environments"] = [row["env_id"] for row in task_envs]
            _attach_origin(task_dict, imap)
            tasks_list.append(task_dict)

        if sort_by == "updated":
            tasks_list.sort(key=lambda t: t.get("jira_updated", "") or "", reverse=True)
        elif sort_by == "created":
            tasks_list.sort(key=lambda t: t.get("jira_created", "") or "", reverse=True)
        elif sort_by == "created_asc":
            tasks_list.sort(key=lambda t: t.get("jira_created", "") or "")
        elif sort_by == "updated_asc":
            tasks_list.sort(key=lambda t: t.get("jira_updated", "") or "")
        elif sort_by == "due_date":
            tasks_list.sort(key=lambda t: t.get("jira_due_date", "") or "zzzz")
        elif sort_by == "due_date_desc":
            tasks_list.sort(key=lambda t: t.get("jira_due_date", "") or "", reverse=True)
        elif sort_by == "start_date":
            tasks_list.sort(key=lambda t: t.get("jira_start_date", "") or "zzzz")
        elif sort_by == "start_date_desc":
            tasks_list.sort(key=lambda t: t.get("jira_start_date", "") or "", reverse=True)
        elif sort_by == "oleada":
            tasks_list.sort(key=lambda t: t.get("jira_oleada", "") or "zzzz")
        elif sort_by == "oleada_desc":
            tasks_list.sort(key=lambda t: t.get("jira_oleada", "") or "", reverse=True)
        elif sort_by == "priority_asc":
            PRIO_ORDER = {"most important": -1,
                          "highest": 0, "blocker": 0, "critical": 0, "cr\u00edtica": 0,
                          "high": 1, "alta": 1, "medium": 2, "media": 2, "normal": 2,
                          "low": 3, "baja": 3, "lowest": 4, "muy baja": 4}
            tasks_list.sort(key=lambda t: PRIO_ORDER.get(t.get("priority", "Normal").lower().strip(), 5), reverse=True)
        else:
            PRIO_ORDER = {"most important": -1,
                          "highest": 0, "blocker": 0, "critical": 0, "cr\u00edtica": 0,
                          "high": 1, "alta": 1, "medium": 2, "media": 2, "normal": 2,
                          "low": 3, "baja": 3, "lowest": 4, "muy baja": 4}
            tasks_list.sort(key=lambda t: PRIO_ORDER.get(t.get("priority", "Normal").lower().strip(), 5))

        result.append({**dict(col), "tasks": tasks_list,
                       "filter_labels": [r["label"] for r in conn.execute(
                           "SELECT label FROM column_filters WHERE column_id = ? ORDER BY label",
                           (col["id"],)).fetchall()]})
    conn.close()
    return jsonify(result)


@app.route("/api/columns", methods=["POST"])
def create_column():
    data = request.json
    conn = get_db()
    max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) FROM columns").fetchone()[0]
    conn.execute("INSERT INTO columns (name, position) VALUES (?, ?)",
                 (data["name"], max_pos + 1))
    conn.commit()
    conn.close()
    return jsonify({"ok": True}), 201


@app.route("/api/columns/<int:col_id>", methods=["PUT"])
def update_column(col_id):
    data = request.json
    conn = get_db()
    conn.execute("UPDATE columns SET name = ? WHERE id = ?", (data["name"], col_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/columns/<int:col_id>", methods=["DELETE"])
def delete_column(col_id):
    conn = get_db()
    # Move tasks to "Sin Asignación" column
    sin_asig = conn.execute("SELECT id FROM columns WHERE name = 'Sin Asignación'").fetchone()
    if not sin_asig:
        max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) FROM columns").fetchone()[0]
        conn.execute("INSERT INTO columns (name, position, is_default) VALUES ('Sin Asignación', ?, 1)",
                     (max_pos + 1,))
        sin_asig_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    else:
        sin_asig_id = sin_asig["id"]
    # If deleting "Sin Asignación" itself, move tasks to first available column
    if col_id == sin_asig_id:
        alt = conn.execute("SELECT id FROM columns WHERE id != ? ORDER BY position LIMIT 1", (col_id,)).fetchone()
        if alt:
            sin_asig_id = alt["id"]
        else:
            conn.close()
            return jsonify({"error": "No se puede borrar la única columna"}), 403
    conn.execute("UPDATE tasks SET column_id = ?, column_override = 0 WHERE column_id = ?",
                 (sin_asig_id, col_id))
    conn.execute("DELETE FROM column_filters WHERE column_id = ?", (col_id,))
    conn.execute("DELETE FROM columns WHERE id = ?", (col_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/columns/reorder", methods=["PUT"])
def reorder_columns():
    data = request.json
    order = data.get("order", [])  # list of column ids in new order
    conn = get_db()
    for pos, col_id in enumerate(order):
        conn.execute("UPDATE columns SET position = ? WHERE id = ?", (pos, int(col_id)))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/columns/apply-defaults", methods=["POST"])
def apply_default_columns():
    """Apply default columns and filters without deleting user columns. Reassign tasks by status."""
    conn = get_db()
    _apply_default_filters(conn)
    # Reassign Jira tasks based on column_filters
    columns = conn.execute("SELECT id FROM columns").fetchall()
    for col in columns:
        filters = conn.execute("SELECT label FROM column_filters WHERE column_id = ?", (col["id"],)).fetchall()
        labels = [f["label"] for f in filters]
        if labels:
            placeholders = ",".join("?" * len(labels))
            conn.execute(
                f"""UPDATE tasks SET column_id = ?, column_override = 0
                    WHERE jira_key IS NOT NULL AND jira_key != ''
                    AND column_override = 0
                    AND deleted = 0
                    AND jira_status IN ({placeholders})""",
                [col["id"]] + labels
            )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════
# ENTORNOS
# ═══════════════════════════════════════════════════════════════
@app.route("/api/environments", methods=["GET"])
def get_environments():
    conn = get_db()
    envs = conn.execute("SELECT * FROM environments ORDER BY position").fetchall()
    conn.close()
    return jsonify([dict(e) for e in envs])


@app.route("/api/environments", methods=["POST"])
def add_environment():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Nombre requerido"}), 400
    conn = get_db()
    existing = conn.execute("SELECT id FROM environments WHERE name = ?", (name,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Ya existe un entorno con ese nombre"}), 409
    max_pos = conn.execute("SELECT COALESCE(MAX(position), -1) FROM environments").fetchone()[0]
    conn.execute("INSERT INTO environments (name, position) VALUES (?, ?)", (name, max_pos + 1))
    conn.commit()
    env_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return jsonify({"id": env_id, "name": name, "position": max_pos + 1})


@app.route("/api/environments/<int:env_id>", methods=["DELETE"])
def delete_environment(env_id):
    conn = get_db()
    conn.execute("DELETE FROM task_environments WHERE env_id = ?", (env_id,))
    conn.execute("DELETE FROM ticket_environments WHERE env_id = ?", (env_id,))
    conn.execute("DELETE FROM environments WHERE id = ?", (env_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>/environments/<int:env_id>", methods=["POST"])
def toggle_task_environment(task_id, env_id):
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM task_environments WHERE task_id = ? AND env_id = ?", (task_id, env_id)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM task_environments WHERE id = ?", (existing["id"],))
        checked = False
    else:
        conn.execute("INSERT INTO task_environments (task_id, env_id) VALUES (?, ?)", (task_id, env_id))
        checked = True
    conn.commit()
    conn.close()
    return jsonify({"checked": checked})


@app.route("/api/tickets/<int:ticket_id>/environments/<int:env_id>", methods=["POST"])
def toggle_ticket_environment(ticket_id, env_id):
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM ticket_environments WHERE ticket_id = ? AND env_id = ?", (ticket_id, env_id)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM ticket_environments WHERE id = ?", (existing["id"],))
        checked = False
    else:
        conn.execute("INSERT INTO ticket_environments (ticket_id, env_id) VALUES (?, ?)", (ticket_id, env_id))
        checked = True
    # Auto-update parent task: if ALL tickets have this env checked, check parent too
    ticket = conn.execute("SELECT task_id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if ticket:
        task_id = ticket["task_id"]
        all_tickets = conn.execute("SELECT id FROM tickets WHERE task_id = ?", (task_id,)).fetchall()
        all_checked = all(
            conn.execute("SELECT id FROM ticket_environments WHERE ticket_id = ? AND env_id = ?",
                         (tk["id"], env_id)).fetchone()
            for tk in all_tickets
        )
        if all_checked and all_tickets:
            conn.execute("INSERT OR IGNORE INTO task_environments (task_id, env_id) VALUES (?, ?)",
                         (task_id, env_id))
        else:
            conn.execute("DELETE FROM task_environments WHERE task_id = ? AND env_id = ?",
                         (task_id, env_id))
    conn.commit()
    conn.close()
    return jsonify({"checked": checked})


@app.route("/api/columns/<int:col_id>/filters", methods=["GET"])
def get_column_filters(col_id):
    conn = get_db()
    rows = conn.execute("SELECT label FROM column_filters WHERE column_id = ? ORDER BY label", (col_id,)).fetchall()
    conn.close()
    return jsonify([row["label"] for row in rows])


@app.route("/api/columns/<int:col_id>/filters", methods=["PUT"])
def set_column_filters(col_id):
    """Replace all filters for a column with the given list of jira statuses.
    Also reassign matching Jira tasks to this column."""
    data = request.json
    labels = data.get("labels", [])
    conn = get_db()
    # Check for duplicates: no status can be in more than one column
    for label in labels:
        label_stripped = label.strip()
        if not label_stripped:
            continue
        existing = conn.execute(
            "SELECT column_id FROM column_filters WHERE label = ? AND column_id != ?",
            (label_stripped, col_id)
        ).fetchone()
        if existing:
            col_name = conn.execute("SELECT name FROM columns WHERE id = ?", (existing["column_id"],)).fetchone()
            conn.close()
            return jsonify({"error": f"El estado '{label_stripped}' ya está asignado a la columna '{col_name['name']}'"}), 409
    conn.execute("DELETE FROM column_filters WHERE column_id = ?", (col_id,))
    clean_labels = []
    for label in labels:
        label = label.strip()
        if label:
            conn.execute("INSERT OR IGNORE INTO column_filters (column_id, label) VALUES (?, ?)", (col_id, label))
            clean_labels.append(label)
    # Reassign Jira tasks whose status matches the new filters to this column
    if clean_labels:
        placeholders = ",".join("?" * len(clean_labels))
        conn.execute(
            f"""UPDATE tasks SET column_id = ?, column_override = 0
                WHERE jira_key IS NOT NULL AND jira_key != ''
                AND column_override = 0
                AND deleted = 0
                AND jira_status IN ({placeholders})""",
            [col_id] + clean_labels
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/jira-statuses", methods=["GET"])
def get_jira_statuses():
    """Return all distinct jira_status values from tasks."""
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT jira_status FROM tasks WHERE jira_status != ''").fetchall()
    conn.close()
    return jsonify(sorted([row["jira_status"] for row in rows]))


# ═══════════════════════════════════════════════════════════════
# API - TAREAS
# ═══════════════════════════════════════════════════════════════
@app.route("/api/tasks", methods=["POST"])
def create_task():
    data = request.json
    conn = get_db()
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM tasks WHERE column_id = ?",
        (data["column_id"],)
    ).fetchone()[0]
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    conn.execute(
        """INSERT INTO tasks (column_id, title, description, priority, labels, link, screenshot, position, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (data["column_id"], data["title"], data.get("description", ""),
         data.get("priority", "Normal"), data.get("labels", ""),
         data.get("link", ""), data.get("screenshot", ""),
         max_pos + 1, now, now)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True}), 201


@app.route("/api/tasks/<int:task_id>", methods=["PUT"])
def update_task(task_id):
    data = request.json
    conn = get_db()
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    conn.execute(
        """UPDATE tasks SET custom_title=?, description=?, priority=?, priority_override=?, labels=?, column_id=?, position=?, updated_at=?
           WHERE id=?""",
        (data.get("title", ""), data.get("description", ""), data.get("priority", "Normal"),
         data.get("priority_override", ""), data.get("labels", ""), data.get("column_id"), data.get("position", 0), now, task_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>/priority", methods=["PUT"])
def update_task_priority(task_id):
    data = request.json
    conn = get_db()
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    conn.execute(
        "UPDATE tasks SET priority_override=?, updated_at=? WHERE id=?",
        (data.get("priority", ""), now, task_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>/move", methods=["PUT"])
def move_task(task_id):
    data = request.json
    conn = get_db()
    # Check if this is a Jira task being moved to a different column than jira_column_id
    task = conn.execute("SELECT jira_key, jira_column_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    new_col = data["column_id"]
    if task and task["jira_key"]:
        jira_col = task["jira_column_id"]
        override = 1 if (jira_col is not None and new_col != jira_col) else 0
        conn.execute("UPDATE tasks SET column_id=?, position=?, column_override=? WHERE id=?",
                     (new_col, data["position"], override, task_id))
    else:
        conn.execute("UPDATE tasks SET column_id=?, position=? WHERE id=?",
                     (new_col, data["position"], task_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>/restore-column", methods=["PUT"])
def restore_column(task_id):
    """Reset a Jira task back to its Jira-status-based column."""
    conn = get_db()
    task = conn.execute("SELECT jira_column_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task or task["jira_column_id"] is None:
        conn.close()
        return jsonify({"error": "No jira column info"}), 400
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    conn.execute(
        "UPDATE tasks SET column_id=?, column_override=0, updated_at=? WHERE id=?",
        (task["jira_column_id"], now, task_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    conn = get_db()
    conn.execute("UPDATE tasks SET deleted = 1 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/deleted")
def deleted_page():
    return render_template("deleted.html")


@app.route("/api/deleted")
def api_deleted():
    q = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "updated")
    conn = get_db()
    if q:
        pattern = f"%{q}%"
        tasks = conn.execute("""
            SELECT t.*, c.name as column_name FROM tasks t
            LEFT JOIN columns c ON t.column_id = c.id
            WHERE t.deleted = 1
              AND (t.title LIKE ? OR t.custom_title LIKE ? OR t.description LIKE ? OR t.jira_key LIKE ? OR t.labels LIKE ?)
            ORDER BY t.updated_at DESC
        """, (pattern, pattern, pattern, pattern, pattern)).fetchall()
    else:
        tasks = conn.execute("""
            SELECT t.*, c.name as column_name FROM tasks t
            LEFT JOIN columns c ON t.column_id = c.id
            WHERE t.deleted = 1
            ORDER BY t.updated_at DESC
        """).fetchall()
    imap = _jira_instance_map(conn)
    conn.close()
    result = [_attach_origin(dict(t), imap) for t in tasks]
    # Sort
    if sort_by == "title":
        result.sort(key=lambda t: (t.get("custom_title") or t.get("title") or "").lower())
    elif sort_by == "title_desc":
        result.sort(key=lambda t: (t.get("custom_title") or t.get("title") or "").lower(), reverse=True)
    elif sort_by == "created":
        result.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    elif sort_by == "created_asc":
        result.sort(key=lambda t: t.get("created_at") or "")
    # default: updated desc (already sorted by query)
    return jsonify(result)


@app.route("/api/tasks/<int:task_id>/restore", methods=["POST"])
def restore_task(task_id):
    data = request.json or {}
    column_id = data.get("column_id")
    conn = get_db()
    task = conn.execute("SELECT id FROM tasks WHERE id = ? AND deleted = 1", (task_id,)).fetchone()
    if not task:
        conn.close()
        return jsonify({"error": "Tarea no encontrada o no está borrada"}), 404
    if column_id:
        col = conn.execute("SELECT id FROM columns WHERE id = ?", (column_id,)).fetchone()
        if not col:
            conn.close()
            return jsonify({"error": "Columna no existe"}), 404
        conn.execute("UPDATE tasks SET deleted = 0, column_id = ?, column_override = 1 WHERE id = ?",
                     (column_id, task_id))
    else:
        # Restore to Sin Asignación
        sin_asig = conn.execute("SELECT id FROM columns WHERE name = 'Sin Asignación'").fetchone()
        if sin_asig:
            conn.execute("UPDATE tasks SET deleted = 0, column_id = ? WHERE id = ?",
                         (sin_asig["id"], task_id))
        else:
            conn.execute("UPDATE tasks SET deleted = 0 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/open-new-task", methods=["POST"])
def open_new_task_popup():
    """Trigger the tray app's Ctrl+Alt+N hotkey to open the tkinter popup."""
    try:
        import keyboard
        keyboard.send("ctrl+alt+n")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# API - TICKETS (subtareas dentro de una tarea)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/tasks/<int:task_id>/tickets", methods=["POST"])
def create_ticket(task_id):
    data = request.json
    conn = get_db()
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) FROM tickets WHERE task_id = ?",
        (task_id,)
    ).fetchone()[0]
    conn.execute("INSERT INTO tickets (task_id, title, position) VALUES (?, ?, ?)",
                 (task_id, data["title"], max_pos + 1))
    conn.commit()
    conn.close()
    return jsonify({"ok": True}), 201


@app.route("/api/tickets/<int:ticket_id>", methods=["PUT"])
def update_ticket(ticket_id):
    data = request.json
    conn = get_db()
    conn.execute("UPDATE tickets SET title=?, done=? WHERE id=?",
                 (data.get("title"), data.get("done", 0), ticket_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tickets/<int:ticket_id>", methods=["DELETE"])
def delete_ticket(ticket_id):
    conn = get_db()
    conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════
# API - INSTANCIAS Y FILTROS DE JIRA
# ═══════════════════════════════════════════════════════════════
def _instance_public(row):
    """Serializa una instancia sin exponer la contraseña."""
    d = dict(row)
    d["has_password"] = bool(d.pop("password", "") or "")
    d["is_cloud"] = _is_cloud(d.get("base_url"))
    return d


def _is_cloud(base_url):
    """Jira Cloud (*.atlassian.net) requiere email + API token y no tiene login.jsp."""
    return ".atlassian.net" in (base_url or "").lower()


def _normalize_jira_url(raw):
    """Deja solo la raíz del Jira a partir de lo que pegue el usuario.

    Pegar la URL del navegador (…/jira/software/c/projects/EUL/list?jql=…) hacía
    que al concatenar '/rest/api/2/…' saliera una ruta inexistente y el servidor
    devolviera el HTML de la SPA en lugar de JSON.
    """
    from urllib.parse import urlparse
    txt = (raw or "").strip()
    if not txt:
        return ""
    if not txt.startswith(("http://", "https://")):
        txt = "https://" + txt
    try:
        p = urlparse(txt)
    except Exception:
        return txt.rstrip("/")
    if not p.netloc:
        return txt.rstrip("/")
    root = f"{p.scheme}://{p.netloc}"
    if _is_cloud(root):
        return root
    ui_paths = {"browse", "secure", "issues", "projects", "jira", "plugins",
                "rest", "login.jsp", "servicedesk", "wiki", "software"}
    seg = [s for s in p.path.split("/") if s]
    if seg and seg[0].lower() not in ui_paths:
        return f"{root}/{seg[0]}"
    return root


def get_jira_sources(conn):
    """Devuelve [(instancia, [filtros])] de todo lo que está habilitado.

    Una instancia sin filtros habilitados no se sincroniza: no hay nada que pedir.
    """
    sources = []
    instances = conn.execute(
        "SELECT * FROM jira_instances WHERE enabled = 1 ORDER BY position, id"
    ).fetchall()
    for inst in instances:
        filters = conn.execute(
            "SELECT * FROM jira_filters WHERE instance_id = ? AND enabled = 1 ORDER BY position, id",
            (inst["id"],)
        ).fetchall()
        if filters:
            sources.append((inst, filters))
    return sources


@app.route("/api/jira-instances", methods=["GET"])
def list_jira_instances():
    conn = get_db()
    rows = conn.execute("SELECT * FROM jira_instances ORDER BY position, id").fetchall()
    result = []
    for r in rows:
        inst = _instance_public(r)
        inst["filters"] = [dict(f) for f in conn.execute(
            "SELECT * FROM jira_filters WHERE instance_id = ? ORDER BY position, id", (r["id"],)
        ).fetchall()]
        result.append(inst)
    conn.close()
    return jsonify(result)


@app.route("/api/jira-filters/<int:filt_id>", methods=["PUT"])
def update_jira_filter(filt_id):
    """Solo permite activar/desactivar el filtro.

    Toda la configuracion (instancias, credenciales, alta y borrado de filtros,
    campos de estado y categoria) vive unicamente en el panel de escritorio, que
    escribe directo en board.db. Desde la web solo se elige que se representa en
    el tablero y que no, asi que aqui se ignora cualquier otro campo que llegue.
    """
    data = request.json or {}
    conn = get_db()
    row = conn.execute("SELECT * FROM jira_filters WHERE id = ?", (filt_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "No existe"}), 404
    conn.execute("UPDATE jira_filters SET enabled=? WHERE id=?",
                 (1 if data.get("enabled", row["enabled"]) else 0, filt_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════
# API - SINCRONIZAR CON JIRA
# ═══════════════════════════════════════════════════════════════
def _filter_jql(filt):
    """JQL a lanzar para un filtro: el ID tiene prioridad sobre la JQL libre."""
    fid = (filt["filter_id"] or "").strip() if filt["filter_id"] is not None else ""
    if fid:
        return f"filter={fid}"
    return (filt["jql"] or "").strip()


# Campos que se piden a Jira. En Cloud es obligatorio enumerarlos: el endpoint
# nuevo devuelve solo el id si no se especifica nada.
SEARCH_FIELDS = ["status", "description", "priority", "summary",
                 "labels", "comment", "updated"]


def _jira_error(resp, base_url):
    """Mensaje legible: Jira explica el motivo real en errorMessages/errors."""
    detail = ""
    try:
        body = resp.json()
        msgs = list(body.get("errorMessages") or [])
        if isinstance(body.get("errors"), dict):
            msgs += [str(v) for v in body["errors"].values()]
        if msgs:
            detail = " - " + "; ".join(str(m) for m in msgs)
    except Exception:
        if resp.text:
            detail = " - " + resp.text[:200]
    return f"Error Jira {resp.status_code} en {base_url}{detail}"


def _adf_to_text(value):
    """Aplana un documento ADF a texto plano.

    La API v3 de Jira Cloud devuelve description y comment.body como un arbol
    JSON (Atlassian Document Format) en lugar de una cadena. El resto de la app
    (board, buscador, capturas) trabaja con texto, asi que se recorre el arbol
    quedandose con el texto y marcando los saltos de bloque.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_adf_to_text(v) for v in value)
    if not isinstance(value, dict):
        return str(value)
    node = value.get("type")
    if node == "text":
        return value.get("text", "")
    if node == "hardBreak":
        return "\n"
    attrs = value.get("attrs") or {}
    if node == "mention":
        return attrs.get("text", "")
    if node == "emoji":
        return attrs.get("shortName", "")
    if node == "inlineCard":
        return attrs.get("url", "")
    inner = _adf_to_text(value.get("content", []))
    if node in ("paragraph", "heading", "listItem", "blockquote",
                "codeBlock", "rule", "panel", "tableRow"):
        return inner + "\n"
    return inner


def _filter_status_field(filt):
    """Campo del que leer el estado en este filtro ('' = el 'status' estandar)."""
    return _filter_field(filt, "status_field")


def _filter_category_field(filt):
    """Campo del que leer la categoria ('' = se usan las etiquetas de Jira)."""
    return _filter_field(filt, "category_field")


def _filter_field(filt, nombre):
    """Lee una columna opcional del filtro tolerando BD sin migrar."""
    try:
        valor = filt[nombre]
    except (KeyError, IndexError):
        return ""
    return (valor or "").strip()


def _field_status_text(value):
    """Normaliza a texto el valor de un campo de Jira usado como estado.

    Segun el tipo, Jira lo devuelve como cadena, como objeto ({name} en los
    campos de sistema, {value} en los desplegables personalizados) o como lista
    (multiseleccion), asi que hay que aplanarlo para poder casarlo con las
    etiquetas de las columnas del tablero.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for clave in ("name", "value", "displayName", "key"):
            if value.get(clave):
                return str(value[clave]).strip()
        return ""
    if isinstance(value, list):
        partes = [_field_status_text(v) for v in value]
        return ", ".join(p for p in partes if p)
    return str(value).strip()


def _fetch_issues_cloud(session, base_url, jql, campos):
    """Descarga incidencias de Jira Cloud con el endpoint de busqueda nuevo.

    Atlassian retiro GET/POST /rest/api/2|3/search el 1 de mayo de 2025. El
    sustituto /rest/api/3/search/jql pagina con un cursor (nextPageToken) en vez
    de startAt y no devuelve 'total', asi que se itera hasta que deja de mandar
    token. Nunca se devuelve una lista parcial como si estuviera completa.
    """
    issues = []
    payload = {"jql": jql, "maxResults": 100, "fields": campos}
    seen_tokens = set()
    for _ in range(200):
        resp = session.post(f"{base_url}/rest/api/3/search/jql",
                            json=payload, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(_jira_error(resp, base_url))
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("issues"), list):
            raise RuntimeError("Jira Cloud devolvió una página de incidencias no válida")
        page = data["issues"]
        issues.extend(page)
        token = data.get("nextPageToken")
        if data.get("isLast") is True or not token:
            if data.get("isLast") is False:
                raise RuntimeError("Jira Cloud no devolvió el cursor de la siguiente página")
            return issues
        if not isinstance(token, str) or token in seen_tokens:
            raise RuntimeError("Jira Cloud repitió o devolvió un cursor no válido")
        seen_tokens.add(token)
        payload["nextPageToken"] = token
    raise RuntimeError("No se completó la paginación de Jira Cloud; no se guardarán resultados parciales")


def _fetch_issues(session, base_url, jql, is_cloud=False, extra_fields=None):
    """Descarga todas las incidencias de una JQL paginando de 50 en 50."""
    campos = list(SEARCH_FIELDS)
    for extra in (extra_fields or []):
        if extra and extra not in campos:
            campos.append(extra)
    if is_cloud:
        return _fetch_issues_cloud(session, base_url, jql, campos)
    issues = []
    start_at = 0
    while True:
        resp = session.get(
            f"{base_url}/rest/api/2/search",
            params={
                "jql": jql,
                "startAt": start_at,
                "maxResults": 50,
                "fields": ",".join(campos),
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(_jira_error(resp, base_url))
        data = resp.json()
        issues.extend(data.get("issues", []))
        if start_at + 50 >= data.get("total", 0):
            break
        start_at += 50
    return issues


@app.route("/api/sync-jira", methods=["POST"])
def sync_jira():
    # El board y Últimas actualizaciones comparten la misma sincronización.
    with _sync_start_lock:
        if _sync_progress["running"]:
            return jsonify({"error": "Ya hay una sincronización en curso.", "running": True}), 409
        _sync_progress.update(running=True, phase="fetching", phase_text="Conectando con Jira…",
                              total=0, done=0, current="")
    try:
        return _run_jira_sync()
    except Exception:
        _sync_progress.update(running=False, phase="error", phase_text="Error durante la sincronización")
        raise


def _run_jira_sync():
    conn = get_db()
    sources = get_jira_sources(conn)
    if not sources:
        conn.close()
        _sync_progress.update(running=False, phase="idle", phase_text="")
        return jsonify({"error": "No hay ninguna instancia de Jira con filtros activos. "
                                 "Configúralas en el panel de escritorio."}), 400

    # Phase 1: Fetching from Jira
    _sync_progress["phase"] = "fetching"
    _sync_progress["phase_text"] = "Obteniendo incidencias de Jira..."
    _sync_progress["running"] = True
    _sync_progress["total"] = 0
    _sync_progress["done"] = 0
    _sync_progress["current"] = ""

    # all_issues: lista de (issue, instancia, filtro) para saber de dónde vino cada una.
    # seen_by_instance: claves vistas por instancia, para la lógica de "desaparecidas".
    all_issues = []
    seen_by_instance = {}
    errors = []
    for inst, filters in sources:
        session = req_lib.Session()
        session.auth = (inst["username"], inst["password"])
        session.verify = False
        session.headers.update({"Content-Type": "application/json",
                                "Accept": "application/json"})
        is_cloud = _is_cloud(inst["base_url"])
        seen_by_instance.setdefault(inst["id"], set())
        for filt in filters:
            jql = _filter_jql(filt)
            if not jql:
                continue
            _sync_progress["phase_text"] = f"Obteniendo {inst['name']} / {filt['name']}..."
            try:
                issues = _fetch_issues(session, inst["base_url"], jql, is_cloud,
                                       [_filter_status_field(filt),
                                        _filter_category_field(filt)])
            except Exception as e:
                errors.append(f"{inst['name']} / {filt['name']}: {e}")
                continue
            for issue in issues:
                all_issues.append((issue, inst, filt))
                seen_by_instance[inst["id"]].add(issue["key"])

    if not all_issues and errors:
        _sync_progress["running"] = False
        _sync_progress["phase"] = "idle"
        conn.close()
        return jsonify({"error": " | ".join(errors)}), 500

    # Mapeo de estado Jira a columna del board usando column_filters
    columns = conn.execute("SELECT * FROM columns ORDER BY position").fetchall()
    col_map = {c["name"].lower(): c["id"] for c in columns}

    # Build status->column_id mapping from column_filters table
    status_to_col = {}
    for c in columns:
        filters = conn.execute("SELECT label FROM column_filters WHERE column_id = ?", (c["id"],)).fetchall()
        for f in filters:
            status_to_col[f["label"].lower()] = c["id"]

    def get_column_id(jira_status):
        s = jira_status.lower()
        # First try exact match from column_filters
        if s in status_to_col:
            return status_to_col[s]
        # Fallback: try partial match against configured filters
        for filter_label, col_id in status_to_col.items():
            if filter_label in s or s in filter_label:
                return col_id
        # Last fallback: first column
        return columns[0]["id"]

    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    imported = 0

    # Phase 2: Processing issues
    _sync_progress["phase"] = "processing"
    _sync_progress["phase_text"] = f"Procesando {len(all_issues)} incidencias..."
    _sync_progress["total"] = len(all_issues)
    _sync_progress["done"] = 0

    for issue, inst, filt in all_issues:
        key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", key)
        # Cada filtro puede leer el estado de un campo propio: hay proyectos
        # cuyo flujo real no se refleja en el 'status' de Jira.
        campo_estado = _filter_status_field(filt)
        if campo_estado and campo_estado != "status":
            status = _field_status_text(fields.get(campo_estado))
        else:
            status = fields.get("status", {}).get("name", "")
        priority = fields.get("priority", {}).get("name", "Normal") if fields.get("priority") else "Normal"
        labels = ", ".join(fields.get("labels", []))
        # Categoria del tablero: si el filtro no configura campo propio, se
        # siguen usando las etiquetas de Jira como hasta ahora.
        campo_categoria = _filter_category_field(filt)
        if campo_categoria:
            jira_category = _field_status_text(fields.get(campo_categoria))
        else:
            jira_category = labels
        desc = _adf_to_text(fields.get("description"))
        # Parse Jira updated date
        jira_updated_raw = fields.get("updated", "") or ""
        jira_updated = ""
        if jira_updated_raw:
            try:
                dt = datetime.fromisoformat(jira_updated_raw.replace("Z", "+00:00"))
                jira_updated = dt.strftime("%d/%m/%Y %H:%M")
            except Exception:
                jira_updated = jira_updated_raw[:16]
        # Parse Jira created date
        jira_created_raw = fields.get("created", "") or ""
        jira_created = ""
        if jira_created_raw:
            try:
                dt = datetime.fromisoformat(jira_created_raw.replace("Z", "+00:00"))
                jira_created = dt.strftime("%d/%m/%Y %H:%M")
            except Exception:
                jira_created = jira_created_raw[:16]
        # Parse due date (fecha estimada)
        jira_due_raw = fields.get("duedate", "") or ""
        jira_due_date = ""
        if jira_due_raw:
            try:
                dt = datetime.fromisoformat(jira_due_raw)
                jira_due_date = dt.strftime("%d/%m/%Y")
            except Exception:
                jira_due_date = jira_due_raw[:10]
        # Parse start date (fecha inicio estimada)
        jira_start_raw = fields.get("customfield_10015", "") or fields.get("customfield_10006", "") or ""
        jira_start_date = ""
        if jira_start_raw:
            try:
                dt = datetime.fromisoformat(str(jira_start_raw))
                jira_start_date = dt.strftime("%d/%m/%Y")
            except Exception:
                jira_start_date = str(jira_start_raw)[:10]
        # Parse oleada (custom field)
        jira_oleada_raw = fields.get("customfield_10100", None) or fields.get("customfield_10101", None) or ""
        jira_oleada = ""
        if jira_oleada_raw:
            if isinstance(jira_oleada_raw, dict):
                jira_oleada = jira_oleada_raw.get("value", "") or jira_oleada_raw.get("name", "")
            else:
                jira_oleada = str(jira_oleada_raw)
        comments = fields.get("comment", {}).get("comments", [])
        last_comment = ""
        if comments:
            lc = comments[-1]
            autor = lc.get("author", {}).get("displayName", "")
            body = _adf_to_text(lc.get("body"))[:200]
            last_comment = f"[{autor}] {body}"

        # Ver si ya existe. La clave sola no basta: dos instancias distintas
        # pueden usar el mismo prefijo de proyecto y colisionar (PROJ-1 en ambas).
        # Se admite también la fila antigua sin instancia asignada para poder
        # adoptarla en la primera sincronización tras la migración.
        existing = conn.execute(
            """SELECT id, column_override, priority_override, deleted FROM tasks
                WHERE jira_key = ? AND (jira_instance_id = ? OR jira_instance_id IS NULL)
                ORDER BY jira_instance_id IS NULL LIMIT 1""",
            (key, inst["id"])
        ).fetchone()
        col_id = get_column_id(status)

        if existing:
            # Skip deleted tasks — don't restore them on sync
            if existing["deleted"]:
                _sync_progress["done"] += 1
                _sync_progress["current"] = key
                continue
            # Respect manual priority override
            task_priority = priority if not existing["priority_override"] else None
            if existing["column_override"]:
                # User moved it manually — keep their column, only update jira_column_id
                if task_priority is not None:
                    conn.execute(
                        """UPDATE tasks SET title=?, description=?, jira_status=?, priority=?,
                           labels=?, last_comment=?, jira_column_id=?, jira_updated=?, jira_created=?,
                           jira_due_date=?, jira_start_date=?, jira_oleada=?, updated_at=? WHERE id=?""",
                        (summary, desc, status, task_priority, labels, last_comment, col_id, jira_updated, jira_created,
                         jira_due_date, jira_start_date, jira_oleada, now, existing["id"])
                    )
                else:
                    conn.execute(
                        """UPDATE tasks SET title=?, description=?, jira_status=?,
                           labels=?, last_comment=?, jira_column_id=?, jira_updated=?, jira_created=?,
                           jira_due_date=?, jira_start_date=?, jira_oleada=?, updated_at=? WHERE id=?""",
                        (summary, desc, status, labels, last_comment, col_id, jira_updated, jira_created,
                         jira_due_date, jira_start_date, jira_oleada, now, existing["id"])
                    )
            else:
                # No column override — sync column as usual
                if task_priority is not None:
                    conn.execute(
                        """UPDATE tasks SET title=?, description=?, jira_status=?, priority=?,
                           labels=?, last_comment=?, column_id=?, jira_column_id=?, jira_updated=?, jira_created=?,
                           jira_due_date=?, jira_start_date=?, jira_oleada=?, updated_at=? WHERE id=?""",
                        (summary, desc, status, task_priority, labels, last_comment, col_id, col_id, jira_updated, jira_created,
                         jira_due_date, jira_start_date, jira_oleada, now, existing["id"])
                    )
                else:
                    conn.execute(
                        """UPDATE tasks SET title=?, description=?, jira_status=?,
                           labels=?, last_comment=?, column_id=?, jira_column_id=?, jira_updated=?, jira_created=?,
                           jira_due_date=?, jira_start_date=?, jira_oleada=?, updated_at=? WHERE id=?""",
                        (summary, desc, status, labels, last_comment, col_id, col_id, jira_updated, jira_created,
                         jira_due_date, jira_start_date, jira_oleada, now, existing["id"])
                    )
            # El origen y la categoria se actualizan aparte para no duplicar las
            # cuatro variantes de UPDATE de arriba.
            conn.execute("UPDATE tasks SET jira_instance_id=?, jira_filter_id=?, jira_category=? WHERE id=?",
                         (inst["id"], filt["id"], jira_category, existing["id"]))
        else:
            max_pos = conn.execute(
                "SELECT COALESCE(MAX(position), -1) FROM tasks WHERE column_id = ?", (col_id,)
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO tasks (column_id, jira_column_id, title, description, jira_key, jira_status,
                   priority, labels, last_comment, jira_updated, jira_created, jira_due_date, jira_start_date, jira_oleada,
                   jira_instance_id, jira_filter_id, jira_category, position, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (col_id, col_id, summary, desc, key, status, priority, labels, last_comment,
                 jira_updated, jira_created, jira_due_date, jira_start_date, jira_oleada,
                 inst["id"], filt["id"], jira_category, max_pos + 1, now, now)
            )
            imported += 1

        _sync_progress["done"] += 1
        _sync_progress["current"] = key

    # Move Jira tasks not in filter to the column with "Desaparecidas del filtro"
    # Find the column that has the special filter
    disappeared_col_id = None
    for c in columns:
        filters = conn.execute("SELECT label FROM column_filters WHERE column_id = ?", (c["id"],)).fetchall()
        if any(f["label"] == "Desaparecidas del filtro" for f in filters):
            disappeared_col_id = c["id"]
            break
    # Fallback to "Sin Asignación" if no column has the special filter
    if disappeared_col_id is None:
        sin_asig = conn.execute("SELECT id FROM columns WHERE name = 'Sin Asignación'").fetchone()
        disappeared_col_id = sin_asig["id"] if sin_asig else columns[-1]["id"]
    # Solo se evalúan las instancias que se han sincronizado en esta pasada y que
    # no han dado error: si un Jira estaba caído o deshabilitado, sus tareas no
    # deben marcarse como desaparecidas.
    failed_instances = {inst["id"] for inst, _ in sources
                        if any(e.startswith(f"{inst['name']} /") for e in errors)}
    for inst_id, synced_keys in seen_by_instance.items():
        if inst_id in failed_instances:
            continue
        tasks_of_instance = conn.execute(
            "SELECT id, jira_key, column_id FROM tasks WHERE jira_key != '' AND deleted = 0 AND jira_instance_id = ?",
            (inst_id,)
        ).fetchall()
        for task in tasks_of_instance:
            if task["jira_key"] not in synced_keys and task["column_id"] != disappeared_col_id:
                conn.execute(
                    "UPDATE tasks SET column_id=?, jira_column_id=?, column_override=0, jira_status='Desaparecida del filtro', updated_at=? WHERE id=?",
                    (disappeared_col_id, disappeared_col_id, now, task["id"])
                )

    conn.commit()
    conn.close()

    # Phase 3: Screenshots — agrupadas por instancia para poder autenticarse en cada Jira
    by_instance = {}
    for issue, inst, _filt in all_issues:
        entry = by_instance.setdefault(inst["id"], {"instance": dict(inst), "keys": []})
        if issue["key"] not in entry["keys"]:
            entry["keys"].append(issue["key"])
    screenshot_jobs = list(by_instance.values())
    total_keys = sum(len(j["keys"]) for j in screenshot_jobs)
    _sync_progress["phase"] = "screenshots"
    _sync_progress["phase_text"] = "Capturando screenshots..."
    _sync_progress["total"] = total_keys
    _sync_progress["done"] = 0
    _sync_progress["current"] = ""
    import threading
    t = threading.Thread(target=_take_screenshots_background, args=(screenshot_jobs,), daemon=True)
    t.start()

    return jsonify({"ok": True, "total": len(all_issues), "imported": imported,
                    "instances": len(sources),
                    "filters": sum(len(f) for _, f in sources),
                    "errors": errors, "screenshots_async": True})


def _take_screenshots_background(jobs):
    """Take screenshots in background using multiple Selenium workers.

    `jobs` es una lista de {"instance": {...}, "keys": [...]}: cada worker se
    autentica en el Jira que le corresponde.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import math

    chunks = []
    cloud_jobs = []
    for job in jobs:
        keys = job["keys"]
        inst = job["instance"]
        if not keys:
            continue
        # Jira Cloud no tiene login.jsp y el acceso va por id.atlassian.com con
        # SSO/MFA, asi que el login por formulario de Selenium no sirve. En su
        # lugar se pide la incidencia ya renderizada a HTML por la propia API
        # (autenticada con el API token) y se captura ese HTML.
        if _is_cloud(inst.get("base_url")):
            cloud_jobs.append((inst, keys))
            continue
        n = min(4, math.ceil(len(keys) / 5)) or 1
        for i in range(n):
            part = keys[i::n]
            if part:
                chunks.append((inst, part))

    if not chunks and not cloud_jobs:
        _sync_progress["phase"] = "done"
        _sync_progress["phase_text"] = "Completado"
        _sync_progress["running"] = False
        return

    if chunks:
        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as executor:
            futures = [executor.submit(_screenshot_worker, inst, part) for inst, part in chunks]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    print(f"  Screenshot worker error: {e}")

    for inst, keys in cloud_jobs:
        try:
            _screenshot_worker_cloud(inst, keys)
        except Exception as e:
            print(f"  Screenshot cloud worker error: {e}")

    _sync_progress["phase"] = "done"
    _sync_progress["phase_text"] = "Completado"
    _sync_progress["running"] = False


def _screenshot_worker(instance, keys):
    """Single Selenium worker that processes a list of Jira keys for one instance."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import time as _time
    from datetime import datetime as _dt

    def _log_error(msg):
        try:
            with open(SCREENSHOT_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{_dt.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
        except Exception:
            pass
        print(f"  {msg}")

    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1200,2400")
    chrome_options.add_argument("--ignore-certificate-errors")

    driver = None
    base_url = instance["base_url"].rstrip("/")
    try:
        driver = webdriver.Chrome(options=chrome_options)
        driver.get(f"{base_url}/login.jsp")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "login-form-username")))
        driver.find_element(By.ID, "login-form-username").send_keys(instance["username"])
        driver.find_element(By.ID, "login-form-password").send_keys(instance["password"])
        driver.find_element(By.ID, "login-form-submit").click()
        WebDriverWait(driver, 10).until(lambda d: "login" not in d.current_url.lower())

        conn = get_db()
        for key in keys:
            try:
                # actionOrder=asc en la URL fuerza orden ascendente (más antiguo arriba,
                # más reciente al final) sólo para esta carga, sin tocar las preferencias del usuario
                driver.get(f"{base_url}/browse/{key}?focusedId=comments&page=com.atlassian.jira.plugin.system.issuetabpanels:comment-tabpanel&actionOrder=asc")
                # Wait for the activity section to load
                try:
                    WebDriverWait(driver, 8).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".activity-comment, .issue-data-block, #activitymodule"))
                    )
                except Exception:
                    _time.sleep(3)

                # Scroll al final para que el comentario más reciente quede visible abajo
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                _time.sleep(1)
                driver.execute_script("""
                    var comments = document.querySelectorAll('.activity-comment, .issue-data-block, .twixi-block');
                    if (comments.length > 0) {
                        comments[comments.length - 1].scrollIntoView({behavior: 'instant', block: 'end'});
                    }
                """)
                _time.sleep(1)

                screenshot_file = f"{key}.png"
                screenshot_path = SCREENSHOTS_DIR / screenshot_file
                driver.save_screenshot(str(screenshot_path))
                # Acotado a la instancia: dos Jiras distintos pueden usar el mismo
                # prefijo de proyecto y sin este filtro la captura de uno se
                # asignaria tambien a la tarea homonima del otro.
                conn.execute("UPDATE tasks SET screenshot=? WHERE jira_key=? AND jira_instance_id=?",
                             (screenshot_file, key, instance["id"]))
                conn.commit()
                _sync_progress["done"] += 1
                _sync_progress["current"] = key
            except Exception as e:
                _sync_progress["done"] += 1
                _log_error(f"Screenshot error for {key}: {e}")
        conn.close()
    except Exception as e:
        _log_error(f"Screenshot worker init error: {e}")
    finally:
        if driver:
            driver.quit()


_CLOUD_SHOT_CSS = """
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; width:1100px; background:#fff; color:#172b4d;
         font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif; font-size:14px; }
  .key { font-size:13px; color:#5e6c84; letter-spacing:.04em; }
  h1 { font-size:20px; margin:4px 0 14px; line-height:1.3; }
  .meta { margin-bottom:18px; }
  .chip { display:inline-block; background:#dfe1e6; color:#42526e; border-radius:3px;
          padding:3px 8px; font-size:12px; font-weight:600; margin-right:6px; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:#5e6c84;
       border-bottom:1px solid #dfe1e6; padding-bottom:6px; margin:22px 0 12px; }
  .body { line-height:1.55; word-wrap:break-word; }
  .body img { max-width:100%; }
  .body pre { background:#f4f5f7; padding:10px; border-radius:3px; overflow-x:auto; }
  .c { border-left:3px solid #dfe1e6; padding:2px 0 2px 12px; margin-bottom:16px; }
  .c .who { font-weight:600; }
  .c .when { color:#5e6c84; font-size:12px; margin-left:6px; }
  .vacio { color:#5e6c84; font-style:italic; }
"""


def _cloud_shot_html(key, fields, rendered):
    """Monta el HTML que se captura para una incidencia de Jira Cloud.

    Se usa `renderedFields`, que es el mismo contenido que se ve en el navegador
    pero ya convertido a HTML por Jira, de modo que no hay que interpretar ADF
    ni abrir sesion en el navegador.
    """
    from html import escape

    def _txt(v):
        return escape(str(v)) if v else ""

    summary = _txt(fields.get("summary", ""))
    status = _txt((fields.get("status") or {}).get("name", ""))
    priority = _txt((fields.get("priority") or {}).get("name", ""))
    assignee = _txt((fields.get("assignee") or {}).get("displayName", ""))
    updated = _txt(rendered.get("updated") or fields.get("updated", ""))

    desc = rendered.get("description") or ""
    if not desc:
        desc = '<p class="vacio">Sin descripcion</p>'

    # Los comentarios se pintan del mas antiguo al mas reciente, igual que la
    # captura de Server (que fuerza actionOrder=asc), para que el ultimo quede
    # abajo del todo.
    comentarios = ((rendered.get("comment") or {}).get("comments")
                   or (fields.get("comment") or {}).get("comments") or [])
    trozos = []
    for c in comentarios:
        autor = _txt((c.get("author") or {}).get("displayName", ""))
        cuando = _txt(c.get("created", ""))
        cuerpo = c.get("body") or ""
        if not isinstance(cuerpo, str):
            cuerpo = escape(_adf_to_text(cuerpo)).replace("\n", "<br>")
        trozos.append(f'<div class="c"><span class="who">{autor}</span>'
                      f'<span class="when">{cuando}</span><div class="body">{cuerpo}</div></div>')
    coms = "".join(trozos) or '<p class="vacio">Sin comentarios</p>'

    chips = "".join(f'<span class="chip">{v}</span>'
                    for v in (status, priority, assignee, updated) if v)

    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<style>{_CLOUD_SHOT_CSS}</style></head><body>
<div class="key">{escape(key)}</div>
<h1>{summary}</h1>
<div class="meta">{chips}</div>
<h2>Descripcion</h2><div class="body">{desc}</div>
<h2>Comentarios ({len(comentarios)})</h2>{coms}
</body></html>"""


def _save_full_page_png(driver, destino):
    """Guarda la pagina entera, no solo lo que cabe en el viewport.

    save_screenshot() de Chrome captura unicamente el viewport, asi que una
    incidencia con varios comentarios salia cortada. Se agranda el viewport al
    tamano real del documento con Emulation.setDeviceMetricsOverride y se
    captura de una vez. Se evita a proposito el parametro `clip` de
    captureScreenshot: combinado con captureBeyondViewport, Chrome re-renderiza
    y la captura sale con el contenido repetido.
    """
    import base64

    # El alto se mide sobre el propio <body>, no con scrollHeight ni con
    # cssContentSize: esos dos devuelven como minimo el alto del viewport, asi
    # que con una incidencia corta daban un lienzo de 2246px con el contenido
    # perdido en medio de un mar de blanco.
    medida = driver.execute_script("""
        const b = document.body, cs = getComputedStyle(b);
        const alto = b.getBoundingClientRect().height
                   + parseFloat(cs.marginTop) + parseFloat(cs.marginBottom);
        return [Math.ceil(alto), Math.ceil(b.getBoundingClientRect().width)];
    """)
    try:
        alto, ancho = int(medida[0]), int(medida[1])
    except (TypeError, ValueError, IndexError):
        alto = ancho = 0

    ancho = max(min(ancho or 1128, 2000), 800)
    alto = max(min(alto or 800, 12000), 200)

    try:
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width": ancho, "height": alto, "deviceScaleFactor": 1, "mobile": False,
        })
        try:
            res = driver.execute_cdp_cmd("Page.captureScreenshot", {"format": "png"})
            with open(destino, "wb") as fh:
                fh.write(base64.b64decode(res["data"]))
            return
        finally:
            driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
    except Exception:
        pass

    # Fallback: la ventana se agranda al contenido y se captura el viewport.
    driver.set_window_size(ancho, alto + 120)
    driver.save_screenshot(str(destino))


def _screenshot_worker_cloud(instance, keys):
    """Capturas para Jira Cloud sin iniciar sesion en el navegador.

    Se pide cada incidencia con expand=renderedFields (Jira devuelve el HTML ya
    montado), se vuelca a un fichero temporal y se captura con Chrome headless.
    Asi se evita el login por formulario, que en Cloud no existe.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from datetime import datetime as _dt
    import tempfile

    def _log_error(msg):
        try:
            with open(SCREENSHOT_LOG, "a", encoding="utf-8") as f:
                f.write(f"[{_dt.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
        except Exception:
            pass
        print(f"  {msg}")

    base_url = instance["base_url"].rstrip("/")
    session = req_lib.Session()
    session.auth = (instance["username"], instance["password"])
    session.verify = False
    session.headers.update({"Accept": "application/json"})

    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1150,2400")
    chrome_options.add_argument("--ignore-certificate-errors")
    chrome_options.add_argument("--hide-scrollbars")

    driver = None
    tmpdir = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        tmpdir = tempfile.mkdtemp(prefix="jiraboard_shot_")
        conn = get_db()
        for key in keys:
            try:
                resp = session.get(
                    f"{base_url}/rest/api/2/issue/{key}",
                    params={"expand": "renderedFields",
                            "fields": "summary,status,priority,assignee,updated,description,comment"},
                    timeout=30,
                )
                if resp.status_code != 200:
                    raise RuntimeError(_jira_error(resp, base_url))
                data = resp.json()
                html = _cloud_shot_html(key, data.get("fields", {}),
                                        data.get("renderedFields", {}))

                html_path = os.path.join(tmpdir, f"{key}.html")
                with open(html_path, "w", encoding="utf-8") as fh:
                    fh.write(html)
                driver.get("file:///" + html_path.replace("\\", "/"))
                _save_full_page_png(driver, SCREENSHOTS_DIR / f"{key}.png")

                screenshot_file = f"{key}.png"
                conn.execute("UPDATE tasks SET screenshot=? WHERE jira_key=? AND jira_instance_id=?",
                             (screenshot_file, key, instance["id"]))
                conn.commit()
                _sync_progress["done"] += 1
                _sync_progress["current"] = key
            except Exception as e:
                _sync_progress["done"] += 1
                _log_error(f"Screenshot cloud error for {key}: {e}")
        conn.close()
    except Exception as e:
        _log_error(f"Screenshot cloud worker init error: {e}")
    finally:
        if driver:
            driver.quit()
        if tmpdir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    app.run(debug=True, port=5000)