"""Destino del resumen y envío explícito a la CLI de VS Code, sin automatizar teclas."""

import csv
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from jira_daily import SUMMARY_INSTRUCTIONS


SUMMARY_TARGETS = {
    "clipboard": "Este u otro chat existente (copiar y pegar)",
    "vscode_new": "Nuevo chat en la ventana activa de VS Code",
}


def get_summary_target(db_path):
    if not Path(db_path).exists():
        return "clipboard"
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        row = conn.execute("SELECT value FROM app_preferences WHERE key = 'summary_target'").fetchone()
        return row[0] if row and row[0] in SUMMARY_TARGETS else "clipboard"
    except sqlite3.OperationalError:
        return "clipboard"
    finally:
        conn.close()


def save_summary_target(db_path, target):
    if target not in SUMMARY_TARGETS:
        raise ValueError("Destino de resumen no válido")
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS app_preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO app_preferences (key, value) VALUES ('summary_target', ?)", (target,))
        conn.commit()
    finally:
        conn.close()


def _vscode_command():
    """En Windows ejecutar la CLI JS sin cmd.exe: ningún texto pasa por un shell."""
    candidates = []
    for name in ("code", "code-insiders"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    if sys.platform == "win32":
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
            if not base:
                continue
            root = Path(base) / "Programs" if "localappdata" in base.lower() or base == os.environ.get("LOCALAPPDATA") else Path(base)
            for product, cli in (("Microsoft VS Code", "code.cmd"), ("Microsoft VS Code Insiders", "code-insiders.cmd")):
                candidates.append(root / product / "bin" / cli)
    for cli in candidates:
        if not cli.is_file():
            continue
        if sys.platform != "win32":
            return [str(cli)]
        if cli.suffix.lower() not in (".cmd", ".bat"):
            continue
        try:
            root = cli.resolve().parent.parent
            match = re.search(r'"%~dp0([^"\r\n]*cli\.js)"', cli.read_text(encoding="utf-8"), re.IGNORECASE)
            script = (cli.parent / match.group(1)).resolve() if match else root / "resources/app/out/cli.js"
            exe = root / ("Code - Insiders.exe" if "insiders" in cli.name else "Code.exe")
            if exe.is_file() and script.is_file() and script.is_relative_to(root):
                return [str(exe), str(script)]
        except (OSError, UnicodeError):
            continue
    return None


def _cli_env():
    env = os.environ.copy()
    if sys.platform == "win32":
        env["ELECTRON_RUN_AS_NODE"] = "1"
        env.pop("VSCODE_DEV", None)
    return env


def _process_open(command):
    try:
        if sys.platform == "win32":
            name = Path(command[0]).name
            result = subprocess.run(["tasklist.exe", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                                    capture_output=True, text=True, errors="replace", timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            return result.returncode == 0 and any(
                row and row[0].casefold() == name.casefold() for row in csv.reader(io.StringIO(result.stdout)))
        result = subprocess.run(["pgrep", "-f", "Visual Studio Code|/code( |$)|/code-insiders( |$)"],
                                capture_output=True, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def vscode_status(db_path):
    command = _vscode_command()
    running = bool(command and _process_open(command))
    target = get_summary_target(db_path)
    return {"target": target, "target_label": SUMMARY_TARGETS[target],
            "available": command is not None, "running": running,
            "can_send": bool(command and running),
            "note": "VS Code abierto no garantiza que Copilot esté autenticado. La CLI no selecciona un chat existente por ID."}


def send_summary(report, data_dir):
    command = _vscode_command()
    if not command or not _process_open(command):
        raise RuntimeError("No se detecta VS Code abierto con su CLI. Ábrelo o usa Copiar contexto para este chat.")
    options = {"capture_output": True, "timeout": 20, "env": _cli_env(), "shell": False}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        help_result = subprocess.run(command + ["chat", "--help"], **options)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("No se pudo comprobar la CLI de VS Code.") from exc
    if help_result.returncode or b"--add-file" not in help_result.stdout or b"--reuse-window" not in help_result.stdout:
        raise RuntimeError("Esta versión de VS Code no admite el envío al chat. Actualízala o copia el contexto.")

    # Solo datos explícitos del informe; nunca passwords/tokens ni el resto de la BD.
    directory = Path(data_dir) / "copilot-context"
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("jira-daily-*.json"):
        try:
            if old.stat().st_mtime < time.time() - 7 * 86400:
                old.unlink()
        except OSError:
            pass
    path = directory / f"jira-daily-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        result = subprocess.run(command + ["chat", "--mode", "ask", "--reuse-window",
                                           "--add-file", str(path.resolve()), SUMMARY_INSTRUCTIONS], **options)
        if result.returncode:
            raise RuntimeError("VS Code rechazó el envío al chat. Usa Copiar contexto para continuar aquí.")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("No se pudo confirmar el envío. Revisa VS Code antes de repetir para evitar duplicados.") from exc
    return path