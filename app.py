"""
JIRA BOARD - Tablero tipo Trello con sincronización Jira
Ejecutar: uv run --with flask --with requests app.py
"""
import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory

import requests as req_lib
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
DB_PATH = Path(__file__).parent / "board.db"
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN JIRA (desde .env)
# ═══════════════════════════════════════════════════════════════
ENV_PATH = Path(__file__).parent / ".env"


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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
            labels TEXT DEFAULT '',
            last_comment TEXT DEFAULT '',
            screenshot TEXT DEFAULT '',
            jira_updated TEXT DEFAULT '',
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
    """)
    # Crear columnas por defecto si no existen
    existing = conn.execute("SELECT COUNT(*) FROM columns").fetchone()[0]
    if existing == 0:
        conn.executemany("INSERT INTO columns (name, position) VALUES (?, ?)", [
            ("Por Hacer", 0),
            ("En Progreso", 1),
            ("En Revisión", 2),
            ("Hecho", 3),
        ])
    conn.commit()
    conn.close()


init_db()

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
    conn.commit()
    conn.close()

migrate_db()


# ═══════════════════════════════════════════════════════════════
# RUTAS - VISTAS
# ═══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("board.html")


@app.route("/screenshots/<path:filename>")
def serve_screenshot(filename):
    return send_from_directory(SCREENSHOTS_DIR, filename)


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
            "SELECT * FROM tasks WHERE column_id = ? ORDER BY position",
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
            task_dict["tickets"] = [dict(tk) for tk in tickets]
            tasks_list.append(task_dict)

        if sort_by == "updated":
            # Sort by jira_updated descending (most recent first)
            tasks_list.sort(key=lambda t: t.get("jira_updated", "") or "", reverse=True)
        else:
            # Sort by priority
            PRIO_ORDER = {"most important": -1,
                          "highest": 0, "blocker": 0, "critical": 0, "cr\u00edtica": 0,
                          "high": 1, "alta": 1, "medium": 2, "media": 2, "normal": 2,
                          "low": 3, "baja": 3, "lowest": 4, "muy baja": 4}
            tasks_list.sort(key=lambda t: PRIO_ORDER.get(t.get("priority", "Normal").lower().strip(), 5))

        result.append({**dict(col), "tasks": tasks_list})
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
    conn.execute("DELETE FROM columns WHERE id = ?", (col_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


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
        """UPDATE tasks SET title=?, description=?, priority=?, labels=?, column_id=?, position=?, updated_at=?
           WHERE id=?""",
        (data.get("title"), data.get("description", ""), data.get("priority", "Normal"),
         data.get("labels", ""), data.get("column_id"), data.get("position", 0), now, task_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>/move", methods=["PUT"])
def move_task(task_id):
    data = request.json
    conn = get_db()
    conn.execute("UPDATE tasks SET column_id=?, position=? WHERE id=?",
                 (data["column_id"], data["position"], task_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    conn = get_db()
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


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

    # Obtener todas las incidencias del filtro
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

    # Mapeo de estado Jira a columna del board
    conn = get_db()
    columns = conn.execute("SELECT * FROM columns").fetchall()
    col_map = {c["name"].lower(): c["id"] for c in columns}

    def get_column_id(jira_status):
        s = jira_status.lower()
        if any(w in s for w in ["abierto", "abierta", "open", "to do", "nuevo"]):
            return col_map.get("por hacer", columns[0]["id"])
        if any(w in s for w in ["progreso", "progress", "desarrollo", "respondido"]):
            return col_map.get("en progreso", columns[1]["id"] if len(columns) > 1 else columns[0]["id"])
        if any(w in s for w in ["espera", "esperando", "usuario", "waiting"]):
            return col_map.get("esperando respuesta usuario", columns[2]["id"] if len(columns) > 2 else columns[0]["id"])
        if any(w in s for w in ["done", "cerrado", "finalizado", "resuelto", "closed"]):
            return col_map.get("hecho", columns[3]["id"] if len(columns) > 3 else columns[0]["id"])
        return columns[0]["id"]

    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    imported = 0

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
        comments = fields.get("comment", {}).get("comments", [])
        last_comment = ""
        if comments:
            lc = comments[-1]
            autor = lc.get("author", {}).get("displayName", "")
            body = lc.get("body", "")[:200]
            last_comment = f"[{autor}] {body}"

        # Ver si ya existe
        existing = conn.execute("SELECT id FROM tasks WHERE jira_key = ?", (key,)).fetchone()
        col_id = get_column_id(status)

        if existing:
            conn.execute(
                """UPDATE tasks SET title=?, description=?, jira_status=?, priority=?,
                   labels=?, last_comment=?, column_id=?, jira_updated=?, updated_at=? WHERE id=?""",
                (summary, desc, status, priority, labels, last_comment, col_id, jira_updated, now, existing["id"])
            )
        else:
            max_pos = conn.execute(
                "SELECT COALESCE(MAX(position), -1) FROM tasks WHERE column_id = ?", (col_id,)
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO tasks (column_id, title, description, jira_key, jira_status,
                   priority, labels, last_comment, jira_updated, position, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (col_id, summary, desc, key, status, priority, labels, last_comment,
                 jira_updated, max_pos + 1, now, now)
            )
            imported += 1

    # Move Jira tasks not in filter to "Hecho"
    synced_keys = {issue["key"] for issue in all_issues}
    hecho_id = col_map.get("hecho", columns[3]["id"] if len(columns) > 3 else columns[-1]["id"])
    all_jira_tasks = conn.execute("SELECT id, jira_key, column_id FROM tasks WHERE jira_key != ''").fetchall()
    for task in all_jira_tasks:
        if task["jira_key"] not in synced_keys and task["column_id"] != hecho_id:
            conn.execute("UPDATE tasks SET column_id=?, jira_status='Finalizado', updated_at=? WHERE id=?",
                         (hecho_id, now, task["id"]))

    conn.commit()
    conn.close()

    # Take screenshots with Selenium
    screenshots_taken = 0
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        import time as _time

        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--window-size=1200,900")
        chrome_options.add_argument("--ignore-certificate-errors")

        driver = webdriver.Chrome(options=chrome_options)
        driver.get(f"{JIRA_BASE_URL}/login.jsp")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "login-form-username")))
        driver.find_element(By.ID, "login-form-username").send_keys(JIRA_USER)
        driver.find_element(By.ID, "login-form-password").send_keys(JIRA_PASS)
        driver.find_element(By.ID, "login-form-submit").click()
        WebDriverWait(driver, 10).until(lambda d: "login" not in d.current_url.lower())

        conn2 = get_db()
        for issue in all_issues:
            key = issue["key"]
            try:
                driver.get(f"{JIRA_BASE_URL}/browse/{key}?focusedId=comments&page=com.atlassian.jira.plugin.system.issuetabpanels:comment-tabpanel")
                _time.sleep(3)
                # Scroll to the very last comment to capture latest activity
                driver.execute_script("""
                    var comments = document.querySelectorAll('.activity-comment, .issue-data-block');
                    if (comments.length > 0) {
                        comments[comments.length - 1].scrollIntoView({block: 'end'});
                    } else {
                        window.scrollTo(0, document.body.scrollHeight);
                    }
                """)
                _time.sleep(1)
                screenshot_file = f"{key}.png"
                screenshot_path = SCREENSHOTS_DIR / screenshot_file
                driver.save_screenshot(str(screenshot_path))
                conn2.execute("UPDATE tasks SET screenshot=? WHERE jira_key=?", (screenshot_file, key))
                screenshots_taken += 1
            except Exception:
                pass
        conn2.commit()
        conn2.close()
        driver.quit()
    except Exception as e:
        print(f"  Screenshots error: {e}")

    return jsonify({"ok": True, "total": len(all_issues), "imported": imported, "screenshots": screenshots_taken})


if __name__ == "__main__":
    print("\n  ===========================================")
    print("   JIRA BOARD - http://localhost:5000")
    print("  ===========================================\n")
    app.run(debug=True, port=5000)
