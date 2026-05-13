"""
JIRA BOARD - Tablero tipo Trello con sincronización Jira
Ejecutar: uv run --with flask --with requests app.py
"""
import os
import sys
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory

import requests as req_lib
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Frozen (PyInstaller) vs normal execution path resolution
if getattr(sys, 'frozen', False):
    _BUNDLE_DIR = Path(sys._MEIPASS)          # bundled read-only assets
    # Allow overriding data dir via env var (for running from TEMP after update)
    _env_data = os.environ.get("JIRABOARD_DATA_DIR")
    _DATA_DIR = Path(_env_data) if _env_data else Path(sys.executable).parent
else:
    _BUNDLE_DIR = Path(__file__).parent
    _DATA_DIR = Path(__file__).parent

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
JIRA_BASE_URL = _env.get("JIRA_BASE_URL", "https://jiraitsm.eulen.com")
FILTER_ID = _env.get("JIRA_FILTER_ID", "30004")
JIRA_USER = _env.get("JIRA_USER", "")
JIRA_PASS = _env.get("JIRA_PASS", "")


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
    conn.commit()
    conn.close()

migrate_db()


# ═══════════════════════════════════════════════════════════════
# RUTAS - VISTAS
# ═══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("board.html")


@app.route("/search")
def search_page():
    return render_template("search.html")


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    conn = get_db()
    pattern = f"%{q}%"
    tasks = conn.execute("""
        SELECT t.*, c.name as column_name
          FROM tasks t
          JOIN columns c ON t.column_id = c.id
         WHERE COALESCE(t.deleted, 0) = 0
           AND (t.title LIKE ? OR t.description LIKE ?
            OR t.jira_key LIKE ? OR t.labels LIKE ?
            OR t.last_comment LIKE ? OR t.jira_status LIKE ?
            OR t.id IN (
                SELECT tk.task_id FROM tickets tk WHERE tk.title LIKE ?
            ))
         ORDER BY t.updated_at DESC
    """, (pattern, pattern, pattern, pattern, pattern, pattern, pattern)).fetchall()
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
        result.append(td)
    conn.close()
    return jsonify(result)


@app.route("/api/screenshot-progress")
def screenshot_progress():
    return jsonify(_sync_progress)


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
        """UPDATE tasks SET title=?, description=?, priority=?, priority_override=?, labels=?, column_id=?, position=?, updated_at=?
           WHERE id=?""",
        (data.get("title"), data.get("description", ""), data.get("priority", "Normal"),
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
    task = conn.execute("SELECT jira_key FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task and task["jira_key"]:
        conn.close()
        return jsonify({"error": "No se pueden borrar incidencias de Jira"}), 403
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
              AND (t.title LIKE ? OR t.description LIKE ? OR t.jira_key LIKE ? OR t.labels LIKE ?)
            ORDER BY t.updated_at DESC
        """, (pattern, pattern, pattern, pattern)).fetchall()
    else:
        tasks = conn.execute("""
            SELECT t.*, c.name as column_name FROM tasks t
            LEFT JOIN columns c ON t.column_id = c.id
            WHERE t.deleted = 1
            ORDER BY t.updated_at DESC
        """).fetchall()
    conn.close()
    result = [dict(t) for t in tasks]
    # Sort
    if sort_by == "title":
        result.sort(key=lambda t: (t.get("title") or "").lower())
    elif sort_by == "title_desc":
        result.sort(key=lambda t: (t.get("title") or "").lower(), reverse=True)
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
# API - SINCRONIZAR CON JIRA
# ═══════════════════════════════════════════════════════════════
@app.route("/api/sync-jira", methods=["POST"])
def sync_jira():
    session = req_lib.Session()
    session.auth = (JIRA_USER, JIRA_PASS)
    session.verify = False
    session.headers.update({"Content-Type": "application/json"})

    # Phase 1: Fetching from Jira
    _sync_progress["phase"] = "fetching"
    _sync_progress["phase_text"] = "Obteniendo incidencias de Jira..."
    _sync_progress["running"] = True
    _sync_progress["total"] = 0
    _sync_progress["done"] = 0
    _sync_progress["current"] = ""

    all_issues = []
    start_at = 0
    while True:
        resp = session.get(
            f"{JIRA_BASE_URL}/rest/api/2/search",
            params={
                "jql": f"filter={FILTER_ID}",
                "startAt": start_at,
                "maxResults": 50,
                "fields": "status,description,priority,summary,labels,comment,updated",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return jsonify({"error": f"Error Jira: {resp.status_code}"}), 500
        data = resp.json()
        all_issues.extend(data.get("issues", []))
        if start_at + 50 >= data.get("total", 0):
            break
        start_at += 50

    # Mapeo de estado Jira a columna del board usando column_filters
    conn = get_db()
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

    for issue in all_issues:
        key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", key)
        status = fields.get("status", {}).get("name", "")
        priority = fields.get("priority", {}).get("name", "Normal") if fields.get("priority") else "Normal"
        labels = ", ".join(fields.get("labels", []))
        desc = fields.get("description", "") or ""
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
            body = lc.get("body", "")[:200]
            last_comment = f"[{autor}] {body}"

        # Ver si ya existe
        existing = conn.execute("SELECT id, column_override, priority_override FROM tasks WHERE jira_key = ?", (key,)).fetchone()
        col_id = get_column_id(status)

        if existing:
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
        else:
            max_pos = conn.execute(
                "SELECT COALESCE(MAX(position), -1) FROM tasks WHERE column_id = ?", (col_id,)
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO tasks (column_id, jira_column_id, title, description, jira_key, jira_status,
                   priority, labels, last_comment, jira_updated, jira_created, jira_due_date, jira_start_date, jira_oleada, position, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (col_id, col_id, summary, desc, key, status, priority, labels, last_comment,
                 jira_updated, jira_created, jira_due_date, jira_start_date, jira_oleada, max_pos + 1, now, now)
            )
            imported += 1

        _sync_progress["done"] += 1
        _sync_progress["current"] = key

    # Move Jira tasks not in filter to the column with "Desaparecidas del filtro"
    synced_keys = {issue["key"] for issue in all_issues}
    # Find the column that has the special filter
    disappeared_col_id = None
    for c in columns:
        filters = conn.execute("SELECT label FROM column_filters WHERE column_id = ?", (c["id"],)).fetchall()
        if any(f["label"] == "Desaparecidas del filtro" for f in filters):
            disappeared_col_id = c["id"]
            break
    # Fallback to "Hecho" column if no column has the special filter
    if disappeared_col_id is None:
        disappeared_col_id = col_map.get("hecho", columns[3]["id"] if len(columns) > 3 else columns[-1]["id"])
    all_jira_tasks = conn.execute("SELECT id, jira_key, column_id FROM tasks WHERE jira_key != ''").fetchall()
    for task in all_jira_tasks:
        if task["jira_key"] not in synced_keys and task["column_id"] != disappeared_col_id:
            conn.execute("UPDATE tasks SET column_id=?, jira_column_id=?, column_override=0, jira_status='Desaparecida del filtro', updated_at=? WHERE id=?",
                         (disappeared_col_id, disappeared_col_id, now, task["id"]))

    conn.commit()
    conn.close()

    # Phase 3: Screenshots
    issue_keys = [issue["key"] for issue in all_issues]
    _sync_progress["phase"] = "screenshots"
    _sync_progress["phase_text"] = "Capturando screenshots..."
    _sync_progress["total"] = len(issue_keys)
    _sync_progress["done"] = 0
    _sync_progress["current"] = ""
    import threading
    t = threading.Thread(target=_take_screenshots_background, args=(issue_keys,), daemon=True)
    t.start()

    return jsonify({"ok": True, "total": len(all_issues), "imported": imported, "screenshots_async": True})


def _take_screenshots_background(issue_keys):
    """Take screenshots in background using multiple Selenium workers."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import math

    num_workers = min(4, math.ceil(len(issue_keys) / 5))
    if num_workers == 0:
        _sync_progress["phase"] = "done"
        _sync_progress["phase_text"] = "Completado"
        _sync_progress["running"] = False
        return

    chunks = [issue_keys[i::num_workers] for i in range(num_workers)]

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(_screenshot_worker, chunk) for chunk in chunks]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"  Screenshot worker error: {e}")
    _sync_progress["phase"] = "done"
    _sync_progress["phase_text"] = "Completado"
    _sync_progress["running"] = False


def _screenshot_worker(keys):
    """Single Selenium worker that processes a list of Jira keys."""
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
    try:
        driver = webdriver.Chrome(options=chrome_options)
        driver.get(f"{JIRA_BASE_URL}/login.jsp")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "login-form-username")))
        driver.find_element(By.ID, "login-form-username").send_keys(JIRA_USER)
        driver.find_element(By.ID, "login-form-password").send_keys(JIRA_PASS)
        driver.find_element(By.ID, "login-form-submit").click()
        WebDriverWait(driver, 10).until(lambda d: "login" not in d.current_url.lower())

        conn = get_db()
        for key in keys:
            try:
                # actionOrder=asc en la URL fuerza orden ascendente (más antiguo arriba,
                # más reciente al final) sólo para esta carga, sin tocar las preferencias del usuario
                driver.get(f"{JIRA_BASE_URL}/browse/{key}?focusedId=comments&page=com.atlassian.jira.plugin.system.issuetabpanels:comment-tabpanel&actionOrder=asc")
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
                conn.execute("UPDATE tasks SET screenshot=? WHERE jira_key=?", (screenshot_file, key))
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
