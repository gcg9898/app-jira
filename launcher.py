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
    _env_data = os.environ.get("JIRABOARD_DATA_DIR")
    BASE_DIR = Path(_env_data) if _env_data else Path(sys.executable).parent
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


# ═══════════════════════════════════════════════════════════════
# CONFIGURACION MULTI-JIRA (tablas jira_instances / jira_filters)
#
# La config vive en board.db, no en el .env. El launcher la lee y escribe
# directamente por sqlite3 para poder gestionarla aunque Flask no esté
# levantado. Flask crea las tablas al arrancar (migrate_db).
# ═══════════════════════════════════════════════════════════════
def jira_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def jira_tables_ready():
    """Las tablas las crea Flask al arrancar. Si aún no existen, avisamos."""
    try:
        conn = jira_db()
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('jira_instances','jira_filters')"
        ).fetchone()[0]
        conn.close()
        return row == 2
    except Exception:
        return False


def list_instances():
    """[(instancia, [filtros])] ordenadas por posición."""
    try:
        conn = jira_db()
        result = []
        for inst in conn.execute("SELECT * FROM jira_instances ORDER BY position, id").fetchall():
            filters = conn.execute(
                "SELECT * FROM jira_filters WHERE instance_id = ? ORDER BY position, id", (inst["id"],)
            ).fetchall()
            result.append((dict(inst), [dict(f) for f in filters]))
        conn.close()
        return result
    except Exception:
        return []


def jira_counts():
    """(nº instancias activas, nº filtros activos) para el resumen del panel."""
    try:
        conn = jira_db()
        i = conn.execute("SELECT COUNT(*) FROM jira_instances WHERE enabled = 1").fetchone()[0]
        f = conn.execute(
            """SELECT COUNT(*) FROM jira_filters f, jira_instances i
                WHERE f.instance_id = i.id AND f.enabled = 1 AND i.enabled = 1"""
        ).fetchone()[0]
        conn.close()
        return i, f
    except Exception:
        return 0, 0


def save_instance(inst_id, name, base_url, username, password, enabled):
    """Crea o actualiza. password None = no tocar la guardada."""
    conn = jira_db()
    try:
        if inst_id:
            if password is None:
                conn.execute(
                    "UPDATE jira_instances SET name=?, base_url=?, username=?, enabled=? WHERE id=?",
                    (name, base_url, username, 1 if enabled else 0, inst_id))
            else:
                conn.execute(
                    "UPDATE jira_instances SET name=?, base_url=?, username=?, password=?, enabled=? WHERE id=?",
                    (name, base_url, username, password, 1 if enabled else 0, inst_id))
            new_id = inst_id
        else:
            n = conn.execute("SELECT COUNT(*) FROM jira_instances").fetchone()[0]
            colors = ["#4A90D9", "#E5A33D", "#7BC67B", "#C97BD9", "#D96B6B",
                      "#5FBFC4", "#B0A16B", "#8E8ED9"]
            conn.execute(
                """INSERT INTO jira_instances (name, base_url, username, password, color, enabled, position)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, base_url, username, password or "", colors[n % len(colors)],
                 1 if enabled else 0, n))
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return new_id, None
    except sqlite3.IntegrityError:
        return None, f"Ya existe una instancia llamada '{name}'"
    finally:
        conn.close()


def delete_instance_db(inst_id):
    conn = jira_db()
    conn.execute("UPDATE tasks SET jira_instance_id = NULL, jira_filter_id = NULL WHERE jira_instance_id = ?",
                 (inst_id,))
    conn.execute("DELETE FROM jira_filters WHERE instance_id = ?", (inst_id,))
    conn.execute("DELETE FROM jira_instances WHERE id = ?", (inst_id,))
    conn.commit()
    conn.close()


def save_filter(filt_id, instance_id, name, filter_id, jql, enabled):
    conn = jira_db()
    if filt_id:
        conn.execute("UPDATE jira_filters SET name=?, filter_id=?, jql=?, enabled=? WHERE id=?",
                     (name, filter_id, jql, 1 if enabled else 0, filt_id))
    else:
        n = conn.execute("SELECT COUNT(*) FROM jira_filters WHERE instance_id = ?",
                         (instance_id,)).fetchone()[0]
        conn.execute(
            """INSERT INTO jira_filters (instance_id, name, filter_id, jql, enabled, position)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (instance_id, name, filter_id, jql, 1 if enabled else 0, n))
    conn.commit()
    conn.close()


def delete_filter_db(filt_id):
    conn = jira_db()
    conn.execute("UPDATE tasks SET jira_filter_id = NULL WHERE jira_filter_id = ?", (filt_id,))
    conn.execute("DELETE FROM jira_filters WHERE id = ?", (filt_id,))
    conn.commit()
    conn.close()


def is_cloud_url(base_url):
    """Jira Cloud (*.atlassian.net) se comporta distinto que Server/DC."""
    return ".atlassian.net" in (base_url or "").lower()


def normalize_jira_url(raw):
    """Extrae la raiz del Jira de lo que pegue el usuario.

    Es habitual pegar la URL del navegador, p.ej.
      https://eulenjira.atlassian.net/jira/software/c/projects/EUL/list?jql=...
    Si se usara tal cual, al concatenar '/rest/api/2/...' sale una ruta absurda
    y el servidor devuelve el HTML de la SPA en vez de JSON.
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

    # En Cloud la raiz es siempre el host; cualquier ruta es interfaz.
    if is_cloud_url(root):
        return root

    # En Server/DC puede haber un context path real (https://host/jira).
    # Se conserva solo el primer segmento y solo si no es una ruta de la UI.
    ui_paths = {"browse", "secure", "issues", "projects", "jira", "plugins",
                "rest", "login.jsp", "servicedesk", "wiki", "software"}
    seg = [s for s in p.path.split("/") if s]
    if seg and seg[0].lower() not in ui_paths:
        return f"{root}/{seg[0]}"
    return root


def test_instance_conn(base_url, username, password):
    """Valida credenciales contra /rest/api/2/myself. Devuelve (ok, mensaje).

    No se parsea el JSON a ciegas: si el Jira responde HTML (portal de login,
    SSO, WAF, proxy) json.loads falla con "Expecting value: line 1 column 1
    (char 0)", que no dice absolutamente nada. Aquí se mira antes el código
    HTTP, el content-type y el principio del cuerpo para poder explicar qué
    ha llegado realmente.
    """
    import base64
    import json as _json
    import ssl

    if not base_url:
        return False, "La instancia no tiene URL configurada."
    root = normalize_jira_url(base_url)
    aviso = ""
    if root != (base_url or "").rstrip("/"):
        aviso = f"  [OJO: la URL guardada era '{base_url}'; se ha usado la raiz '{root}'. Guardala corregida.]"
    url = root + "/rest/api/2/myself"
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = Request(url, headers={
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "User-Agent": "JiraBoard-Launcher",
    })
    # Los Jira internos suelen tener certificado autofirmado
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urlopen(req, timeout=15, context=ctx) as r:
            status = getattr(r, "status", r.getcode())
            ctype = r.headers.get("Content-Type", "?")
            raw = r.read()
            final_url = r.geturl()
    except HTTPError as e:
        try:
            body = " ".join(e.read().decode("utf-8", "replace")[:300].split())
        except Exception:
            body = ""
        if e.code in (401, 403):
            if is_cloud_url(root):
                return False, (
                    f"HTTP {e.code} - Jira Cloud NO admite usuario+contrasena. "
                    f"Usa tu EMAIL como usuario y un API TOKEN como contrasena "
                    f"(se genera en https://id.atlassian.com/manage-profile/security/api-tokens). "
                    f"URL: {url}{aviso}")
            return False, f"HTTP {e.code} - usuario o contrasena incorrectos. URL: {url}{aviso}"
        return False, f"HTTP {e.code} en {url}. Respuesta: {body}{aviso}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}  [URL: {url}]{aviso}"

    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return False, f"Respuesta vacia (HTTP {status}) desde {final_url}"

    try:
        data = _json.loads(text)
    except ValueError:
        inicio = " ".join(text[:300].split())
        es_html = "<html" in text[:500].lower() or "text/html" in ctype.lower()
        if es_html:
            return False, (
                f"El servidor devolvio HTML en vez de JSON (HTTP {status}, {ctype}). "
                f"Suele significar que la URL no es la raiz del Jira, que redirige "
                f"a un portal de login/SSO, o que hay un proxy delante. "
                f"URL final: {final_url}{aviso} | Inicio: {inicio}")
        return False, (
            f"Respuesta no-JSON (HTTP {status}, {ctype}) desde {final_url}{aviso}. "
            f"Inicio de la respuesta: {inicio}")

    if not isinstance(data, dict):
        return False, f"JSON inesperado (HTTP {status}): {str(data)[:200]}"
    who = data.get("displayName") or data.get("name") or data.get("emailAddress") or username
    extra = "  (Jira Cloud)" if is_cloud_url(root) else ""
    return True, f"Conectado como {who}{extra}{aviso}"


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
GITHUB_EXE_COMMITS_URL = f"https://api.github.com/repos/{GITHUB_REPO}/commits?path=dist/JiraBoard.exe&sha={GITHUB_BRANCH}&per_page=1"
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
    """Get version by finding the parent of the last build commit that changed the exe.
    The post-commit hook writes the feature commit SHA to version.txt then commits the exe,
    so the parent of the build commit = the SHA bundled in the exe."""
    import json
    try:
        req = Request(GITHUB_EXE_COMMITS_URL, headers={
            "User-Agent": "JiraBoard-Updater",
            "Accept": "application/vnd.github.v3+json",
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and data[0].get("parents"):
                return data[0]["parents"][0]["sha"][:7]
            # If no parent (initial commit), use the commit itself
            return data[0]["sha"][:7]
    except (URLError, HTTPError, OSError, KeyError, IndexError):
        return None


def get_remote_changelog(local_ver, remote_ver, include_current=False):
    """Fetch CHANGELOG.txt from GitHub and return relevant entries.
    - If there are entries newer than local_ver → return all of them.
    - If local_ver is the newest or not found → return the latest entry."""
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/CHANGELOG.txt"
        req = Request(url, headers={"User-Agent": "JiraBoard-Updater"})
        with urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
    except (URLError, HTTPError, OSError):
        return None

    # Parse sections: each starts with [hash], ordered newest first
    import re
    sections = re.split(r'(?=^\[)', content, flags=re.MULTILINE)
    parsed = []
    for section in sections:
        match = re.match(r'\[([a-zA-Z0-9_-]+)\]', section)
        if match:
            parsed.append((match.group(1), section.strip()))

    if not parsed:
        return None

    # Find local version position in changelog
    local_idx = None
    for i, (ver, _) in enumerate(parsed):
        if ver == local_ver:
            local_idx = i
            break

    if local_idx is not None and local_idx > 0:
        # There are newer entries than mine → show them all
        newer = [text for _, text in parsed[:local_idx]]
        return "\n\n".join(newer)
    else:
        # My version is the newest or not found → show latest entry
        return parsed[0][1]


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
    # Download to TEMP first (outside OneDrive) to avoid sync issues
    import tempfile
    temp_dir = Path(tempfile.gettempdir())
    update_path = temp_dir / "JiraBoard_update.exe"
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
    # Use BASE_DIR to find the "real" exe location (may differ from sys.executable
    # when running from a local TEMP copy)
    exe_path = BASE_DIR / "JiraBoard.exe"
    if not exe_path.exists():
        exe_path = Path(sys.executable)
    import tempfile
    update_path = Path(tempfile.gettempdir()) / "JiraBoard_update.exe"
    if not update_path.exists():
        # Fallback to same directory
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
echo Update (en TEMP): {update_path} >> "{log_path}"

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
powershell -Command "Unblock-File -Path '{update_path}'; Remove-Item -Path '{update_path}:Zone.Identifier' -ErrorAction SilentlyContinue" >> "{log_path}" 2>&1

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

REM Move update into place (move is atomic, avoids OneDrive partial sync)
echo Moviendo update a destino... >> "{log_path}"
move /Y "{update_path}" "{exe_path}" >nul 2>>"{log_path}"
if errorlevel 1 (
    echo ERROR: move fallo, intentando copy... >> "{log_path}"
    copy /B /Y "{update_path}" "{exe_path}" >nul 2>>"{log_path}"
    if errorlevel 1 (
        echo ERROR: copy tambien fallo >> "{log_path}"
        goto cleanup
    )
)
echo Archivo en destino OK >> "{log_path}"

REM Verify sizes
for %%A in ("{exe_path}") do set DST_SIZE=%%~zA
echo Tamano destino: %DST_SIZE% >> "{log_path}"

REM Unblock final exe
echo Desbloqueando exe final... >> "{log_path}"
powershell -Command "Unblock-File -Path '{exe_path}'; Remove-Item -Path '{exe_path}:Zone.Identifier' -ErrorAction SilentlyContinue" >> "{log_path}" 2>&1

REM Pin file as "Always keep on this device" for OneDrive
echo Pineando archivo en disco local (attrib +P -U)... >> "{log_path}"
attrib -U +P "{exe_path}" >> "{log_path}" 2>&1

REM Force Windows to fully read and cache the file (triggers Defender scan early)
echo Forzando lectura completa del exe para cache... >> "{log_path}"
powershell -Command "[IO.File]::ReadAllBytes('{exe_path}').Length" >> "{log_path}" 2>&1

REM Wait for OneDrive sync to complete
echo Esperando sincronizacion OneDrive (5s)... >> "{log_path}"
ping 127.0.0.1 -n 6 > nul

REM Use %APPDATA%\JiraBoard as the trusted install location (not TEMP)
set "APP_DIR=%APPDATA%\\JiraBoard"
set "LOCAL_EXE=%APP_DIR%\\JiraBoard.exe"
echo Directorio local: %APP_DIR% >> "{log_path}"
mkdir "%APP_DIR%" 2>nul

REM Kill any previous instance running from that location
taskkill /F /IM "JiraBoard.exe" >nul 2>&1
ping 127.0.0.1 -n 2 > nul

REM Copy exe to trusted AppData location
echo Copiando exe a: %LOCAL_EXE% >> "{log_path}"
copy /B /Y "{exe_path}" "%LOCAL_EXE%" >nul 2>>"{log_path}"
if errorlevel 1 (
    echo ERROR copia a AppData, lanzando desde OneDrive... >> "{log_path}"
    start "" "{exe_path}"
    goto cleanup
)
echo Copia AppData OK >> "{log_path}"

REM Unblock in AppData location
powershell -Command "Unblock-File -Path '%LOCAL_EXE%'; Remove-Item -Path '%LOCAL_EXE%:Zone.Identifier' -ErrorAction SilentlyContinue" >> "{log_path}" 2>&1

REM Pre-run exe to trigger PyInstaller extraction and Defender pre-scan
REM This uses --mode=check (silently exits) so DLLs get extracted and trusted
echo Pre-extrayendo DLLs (Defender pre-scan)... >> "{log_path}"
set "JIRABOARD_DATA_DIR={exe_path.parent}"
set "JIRABOARD_PRECHECK=1"
start /wait /min "" "%LOCAL_EXE%"
echo Pre-extraccion completada >> "{log_path}"

REM Unblock all extracted DLLs in _MEI folders
echo Desbloqueando DLLs extraidas... >> "{log_path}"
for /d %%D in ("%TEMP%\\_MEI*") do (
    powershell -Command "Get-ChildItem '%%D' -Filter *.dll | ForEach-Object {{ Unblock-File $_.FullName }}" >> "{log_path}" 2>&1
)

REM Now launch normally
echo Lanzando JiraBoard desde AppData... >> "{log_path}"
start "" "%LOCAL_EXE%"
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


# ═══════════════════════════════════════════════════════════════
# DIALOGO DE GESTION DE JIRAS Y FILTROS
# ═══════════════════════════════════════════════════════════════
BG = "#0f0f23"
BG2 = "#1a1a2e"
FG = "#e0e0e0"
FG_DIM = "#ccc"
ACCENT = "#16c79a"
BORDER = "#2a2a4a"


def _entry(parent, width=34, show=None):
    return tk.Entry(parent, bg=BG, fg=FG, insertbackground=ACCENT,
                    font=("Segoe UI", 9), relief="flat", highlightthickness=1,
                    highlightcolor=ACCENT, highlightbackground=BORDER,
                    width=width, show=show)


def _btn(parent, text, cmd, bg=BORDER, fg=FG, width=None):
    return tk.Button(parent, text=text, bg=bg, fg=fg, font=("Segoe UI", 9),
                     relief="flat", padx=10, pady=3, cursor="hand2",
                     command=cmd, width=width)


class JiraManagerDialog(tk.Toplevel):
    """Gestor de instancias de Jira y sus filtros.

    Escribe directamente en board.db para poder usarse aunque Flask esté parado.
    El árbol de la izquierda muestra instancias con sus filtros colgando; el
    panel de la derecha cambia según lo seleccionado.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Jiras y filtros")
        self.configure(bg=BG2)
        self.geometry("860x520")
        self.transient(parent)
        self.grab_set()

        self.sel_kind = None      # 'instance' | 'filter'
        self.sel_id = None
        self.instances = []

        if not jira_tables_ready():
            tk.Label(self, text="Las tablas de configuración todavía no existen.\n\n"
                                "Arranca la aplicación una vez (botón «Iniciar») para que\n"
                                "se creen y vuelve a abrir esta ventana.",
                     bg=BG2, fg="#f5a623", font=("Segoe UI", 10), justify="left").pack(padx=30, pady=40)
            _btn(self, "Cerrar", self.destroy).pack(pady=(0, 20))
            return

        body = tk.Frame(self, bg=BG2)
        body.pack(fill="both", expand=True, padx=14, pady=14)

        # ── Izquierda: árbol instancias → filtros ──
        left = tk.Frame(body, bg=BG2)
        left.pack(side="left", fill="both", expand=True)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Jira.Treeview", background=BG, fieldbackground=BG,
                        foreground=FG, borderwidth=0, rowheight=24)
        style.map("Jira.Treeview", background=[("selected", BORDER)])

        self.tree = ttk.Treeview(left, style="Jira.Treeview", show="tree", selectmode="browse")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        # ── Derecha: detalle del elemento seleccionado ──
        self.right = tk.Frame(body, bg=BG, padx=14, pady=14,
                              highlightbackground=BORDER, highlightthickness=1)
        self.right.pack(side="left", fill="both", padx=(14, 0))

        # ── Botonera inferior ──
        bar = tk.Frame(self, bg=BG2)
        bar.pack(fill="x", padx=14, pady=(0, 14))
        _btn(bar, "+ Jira", self.new_instance, bg=ACCENT, fg=BG).pack(side="left")
        _btn(bar, "+ Filtro", self.new_filter).pack(side="left", padx=(6, 0))
        _btn(bar, "Eliminar", self.delete_selected, bg="#6b2020").pack(side="left", padx=(6, 0))
        _btn(bar, "Probar conexión", self.test_selected).pack(side="left", padx=(6, 0))
        _btn(bar, "Cerrar", self.destroy).pack(side="right")

        # Barra de estado: Entry de solo lectura en vez de Label para que el
        # texto se pueda seleccionar y copiar (los errores de conexión son
        # largos y hay que poder pegarlos en un ticket).
        status_bar = tk.Frame(self, bg=BG2)
        status_bar.pack(fill="x", padx=16, pady=(0, 10))
        self.status_var = tk.StringVar(value="")
        self.status = tk.Entry(status_bar, textvariable=self.status_var, bg=BG2, fg="#888",
                               font=("Segoe UI", 8), relief="flat", borderwidth=0,
                               highlightthickness=0, readonlybackground=BG2,
                               state="readonly")
        self.status.pack(side="left", fill="x", expand=True)
        _btn(status_bar, "Ver / copiar", self.show_status_detail).pack(side="right", padx=(6, 0))

        self.reload()

    def set_status(self, msg, color="#888"):
        self.status_var.set(msg)
        self.status.config(fg=color)

    def copy_status(self):
        self.clipboard_clear()
        self.clipboard_append(self.status_var.get())
        self.update()

    def show_status_detail(self):
        """Ventana con el mensaje completo en un Text seleccionable + botón copiar."""
        msg = self.status_var.get()
        if not msg:
            return
        win = tk.Toplevel(self)
        win.title("Detalle")
        win.configure(bg=BG2)
        win.geometry("700x260")
        win.transient(self)
        txt = tk.Text(win, bg=BG, fg=FG, font=("Consolas", 9), relief="flat",
                      wrap="word", padx=10, pady=10, insertbackground=ACCENT)
        txt.pack(fill="both", expand=True, padx=12, pady=12)
        txt.insert("1.0", msg)
        bar = tk.Frame(win, bg=BG2)
        bar.pack(fill="x", padx=12, pady=(0, 12))

        def copiar():
            win.clipboard_clear()
            win.clipboard_append(msg)
            win.update()
            lbl.config(text="Copiado al portapapeles", fg=ACCENT)

        _btn(bar, "Copiar", copiar, bg=ACCENT, fg=BG).pack(side="left")
        _btn(bar, "Cerrar", win.destroy).pack(side="right")
        lbl = tk.Label(bar, text="", bg=BG2, fg="#888", font=("Segoe UI", 8))
        lbl.pack(side="left", padx=(10, 0))

    # ── Datos ──
    def reload(self, keep=None):
        self.instances = list_instances()
        self.tree.delete(*self.tree.get_children())
        for inst, filters in self.instances:
            mark = "" if inst["enabled"] else "  (desactivada)"
            node = self.tree.insert("", "end", iid=f"i{inst['id']}",
                                    text=f"  {inst['name']}{mark}", open=True)
            for f in filters:
                tick = "✓" if f["enabled"] else "✗"
                ref = f"filter={f['filter_id']}" if f["filter_id"] else (f["jql"] or "")
                self.tree.insert(node, "end", iid=f"f{f['id']}",
                                 text=f"     {tick}  {f['name']}   ({ref[:40]})")
        target = keep if keep and self.tree.exists(keep) else (
            self.tree.get_children()[0] if self.tree.get_children() else None)
        if target:
            self.tree.selection_set(target)
            self.tree.focus(target)
        else:
            self.show_empty()

    def find_instance(self, inst_id):
        for inst, filters in self.instances:
            if inst["id"] == inst_id:
                return inst, filters
        return None, []

    def find_filter(self, filt_id):
        for inst, filters in self.instances:
            for f in filters:
                if f["id"] == filt_id:
                    return f, inst
        return None, None

    # ── Panel derecho ──
    def clear_right(self):
        for w in self.right.winfo_children():
            w.destroy()

    def show_empty(self):
        self.clear_right()
        self.sel_kind = self.sel_id = None
        tk.Label(self.right, text="No hay ningún Jira configurado.\n\nPulsa «+ Jira» para añadir el primero.",
                 bg=BG, fg="#888", font=("Segoe UI", 9), justify="left").pack(anchor="w")

    def on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("i"):
            self.show_instance(int(iid[1:]))
        else:
            self.show_filter(int(iid[1:]))

    def show_instance(self, inst_id, blank=False):
        self.clear_right()
        self.sel_kind, self.sel_id = "instance", (None if blank else inst_id)
        inst = {} if blank else self.find_instance(inst_id)[0] or {}

        tk.Label(self.right, text="Nuevo Jira" if blank else "Instancia de Jira",
                 bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2,
                                                                   sticky="w", pady=(0, 10))
        rows = [("Nombre:", "name", ""), ("URL:", "base_url", ""), ("Usuario:", "username", "")]
        self.i_fields = {}
        for r, (label, key, _d) in enumerate(rows, start=1):
            tk.Label(self.right, text=label, bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 9)).grid(row=r, column=0, sticky="w", pady=3)
            e = _entry(self.right)
            e.grid(row=r, column=1, sticky="w", padx=(8, 0), pady=3)
            e.insert(0, inst.get(key, "") or "")
            self.i_fields[key] = e

        tk.Label(self.right, text="Contraseña:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).grid(row=4, column=0, sticky="w", pady=3)
        self.i_pass = _entry(self.right, show="*")
        self.i_pass.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=3)
        if not blank:
            tk.Label(self.right, text="(vacío = no cambiar la guardada)", bg=BG, fg="#666",
                     font=("Segoe UI", 8)).grid(row=5, column=1, sticky="w", padx=(8, 0))

        self.i_enabled = tk.BooleanVar(value=bool(inst.get("enabled", 1)))
        tk.Checkbutton(self.right, text="Activa (se sincroniza)", variable=self.i_enabled,
                       bg=BG, fg=FG_DIM, selectcolor=BG, activebackground=BG,
                       activeforeground=FG, font=("Segoe UI", 9),
                       highlightthickness=0, borderwidth=0).grid(row=6, column=0, columnspan=2,
                                                                 sticky="w", pady=(8, 0))
        tk.Label(self.right,
                 text=("URL: solo la raiz (https://miempresa.atlassian.net).\n"
                       "Jira Cloud (*.atlassian.net): usuario = tu EMAIL y\n"
                       "contrasena = un API TOKEN, no la del usuario."),
                 bg=BG, fg="#666", font=("Segoe UI", 8), justify="left").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        _btn(self.right, "Guardar", self.save_instance_ui, bg=ACCENT, fg=BG).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(12, 0))

    def show_filter(self, filt_id, blank=False, instance_id=None):
        self.clear_right()
        self.sel_kind, self.sel_id = "filter", (None if blank else filt_id)
        self.filter_instance_id = instance_id
        f = {} if blank else self.find_filter(filt_id)[0] or {}
        if not blank:
            self.filter_instance_id = f.get("instance_id")

        tk.Label(self.right, text="Nuevo filtro" if blank else "Filtro",
                 bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2,
                                                                   sticky="w", pady=(0, 10))
        tk.Label(self.right, text="Nombre:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=3)
        self.f_name = _entry(self.right)
        self.f_name.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)
        self.f_name.insert(0, f.get("name", "") or "")

        tk.Label(self.right, text="ID filtro:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w", pady=3)
        self.f_id = _entry(self.right, width=14)
        self.f_id.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=3)
        self.f_id.insert(0, f.get("filter_id", "") or "")

        tk.Label(self.right, text="o JQL:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).grid(row=3, column=0, sticky="w", pady=3)
        self.f_jql = _entry(self.right)
        self.f_jql.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=3)
        self.f_jql.insert(0, f.get("jql", "") or "")

        tk.Label(self.right, text="Rellena el ID (ej. 30004) o una JQL, no ambos.\nSi pones los dos, manda el ID.",
                 bg=BG, fg="#666", font=("Segoe UI", 8), justify="left").grid(
            row=4, column=1, sticky="w", padx=(8, 0), pady=(2, 0))

        self.f_enabled = tk.BooleanVar(value=bool(f.get("enabled", 1)))
        tk.Checkbutton(self.right, text="Activo (se sincroniza)", variable=self.f_enabled,
                       bg=BG, fg=FG_DIM, selectcolor=BG, activebackground=BG,
                       activeforeground=FG, font=("Segoe UI", 9),
                       highlightthickness=0, borderwidth=0).grid(row=5, column=0, columnspan=2,
                                                                 sticky="w", pady=(8, 0))
        _btn(self.right, "Guardar", self.save_filter_ui, bg=ACCENT, fg=BG).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))

    # ── Acciones ──
    def new_instance(self):
        self.show_instance(None, blank=True)

    def new_filter(self):
        inst_id = self.current_instance_id()
        if not inst_id:
            messagebox.showwarning("Filtro", "Selecciona primero un Jira en la lista.", parent=self)
            return
        self.show_filter(None, blank=True, instance_id=inst_id)

    def current_instance_id(self):
        """Instancia asociada a la selección actual (sea instancia o filtro)."""
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        if iid.startswith("i"):
            return int(iid[1:])
        f, _inst = self.find_filter(int(iid[1:]))
        return f["instance_id"] if f else None

    def save_instance_ui(self):
        name = self.i_fields["name"].get().strip()
        url = normalize_jira_url(self.i_fields["base_url"].get())
        user = self.i_fields["username"].get().strip()
        pwd = self.i_pass.get()
        if not name or not url:
            messagebox.showwarning("Guardar", "Nombre y URL son obligatorios.", parent=self)
            return
        # Se refleja la URL normalizada para que el usuario vea que se ha
        # quedado solo con la raiz (suele pegar la URL del navegador).
        self.i_fields["base_url"].delete(0, "end")
        self.i_fields["base_url"].insert(0, url)
        # En alta la contraseña va tal cual; en edición, vacío = conservar
        password = pwd if (self.sel_id is None or pwd != "") else None
        new_id, err = save_instance(self.sel_id, name, url, user, password, self.i_enabled.get())
        if err:
            messagebox.showerror("Guardar", err, parent=self)
            return
        self.set_status(f"Guardado: {name}", ACCENT)
        self.reload(keep=f"i{new_id}")

    def save_filter_ui(self):
        fid = self.f_id.get().strip()
        jql = self.f_jql.get().strip()
        if not fid and not jql:
            messagebox.showwarning("Guardar", "Indica el ID del filtro o una JQL.", parent=self)
            return
        if fid and not fid.isdigit():
            messagebox.showwarning("Guardar", "El ID del filtro debe ser numérico.\n"
                                              "Si querías una JQL, ponla en el campo de abajo.", parent=self)
            return
        if fid:
            jql = ""
        name = self.f_name.get().strip() or (f"Filtro {fid}" if fid else "JQL")
        save_filter(self.sel_id, self.filter_instance_id, name, fid, jql, self.f_enabled.get())
        self.set_status(f"Guardado: {name}", ACCENT)
        self.reload(keep=f"i{self.filter_instance_id}")

    def delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("i"):
            inst, _f = self.find_instance(int(iid[1:]))
            if not inst:
                return
            if not messagebox.askyesno(
                    "Eliminar",
                    f"¿Eliminar el Jira «{inst['name']}» y todos sus filtros?\n\n"
                    "Las tarjetas ya importadas se conservan, pero dejarán de\n"
                    "sincronizarse y perderán el enlace a Jira.", parent=self):
                return
            delete_instance_db(inst["id"])
        else:
            f, _i = self.find_filter(int(iid[1:]))
            if not f:
                return
            if not messagebox.askyesno("Eliminar", f"¿Eliminar el filtro «{f['name']}»?", parent=self):
                return
            delete_filter_db(f["id"])
        self.set_status("Eliminado")
        self.reload()

    def test_selected(self):
        inst_id = self.current_instance_id()
        if not inst_id:
            messagebox.showwarning("Probar", "Selecciona un Jira en la lista.", parent=self)
            return
        inst, _f = self.find_instance(inst_id)
        if not inst:
            return
        self.set_status(f"Probando {inst['name']}...")
        self.update_idletasks()

        def run():
            ok, msg = test_instance_conn(inst["base_url"], inst["username"], inst["password"])
            self.set_status(msg, ACCENT if ok else "#D96B6B")
            if not ok:
                # El mensaje suele ser largo: se abre el detalle para poder leerlo y copiarlo
                self.after(0, self.show_status_detail)

        threading.Thread(target=run, daemon=True).start()


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

        # Jira config frame — instancias y filtros (config real en board.db)
        cred_frame = tk.Frame(root, bg="#0f0f23", padx=16, pady=12,
                              highlightbackground="#2a2a4a", highlightthickness=1)
        cred_frame.pack(fill="x", padx=20, pady=(0, 12))

        tk.Label(cred_frame, text="Jiras y filtros", bg="#0f0f23", fg="#e0e0e0",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        tk.Label(cred_frame, text="Configurados:", bg="#0f0f23", fg="#ccc",
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w")
        self.jira_summary = tk.Label(cred_frame, text="...", bg="#0f0f23", fg="#4A90D9",
                                     font=("Segoe UI", 9, "bold"))
        self.jira_summary.grid(row=1, column=1, sticky="w", padx=(8, 0))

        self.jira_detail = tk.Label(cred_frame, text="", bg="#0f0f23", fg="#888",
                                    font=("Segoe UI", 8), justify="left", anchor="w")
        self.jira_detail.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        tk.Button(cred_frame, text="\u2699 Gestionar Jiras y filtros", bg="#16c79a", fg="#0f0f23",
                  font=("Segoe UI", 9, "bold"), relief="flat", padx=10, pady=3,
                  cursor="hand2", command=self.open_jira_manager).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))

        self.refresh_jira_summary()

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

        # Check for updates on startup
        self.root.after(2000, lambda: self.check_for_updates(auto_check=True))

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

    def refresh_jira_summary(self):
        """Resumen de instancias/filtros activos en el panel principal."""
        n_inst, n_filt = jira_counts()
        if not jira_tables_ready():
            self.jira_summary.config(text="sin inicializar", fg="#f5a623")
            self.jira_detail.config(text="Arranca la aplicación una vez para crear la configuración.")
            return
        self.jira_summary.config(
            text=f"{n_inst} Jira{'s' if n_inst != 1 else ''} · {n_filt} filtro{'s' if n_filt != 1 else ''}",
            fg="#4A90D9" if n_inst else "#f5a623")
        lineas = []
        for inst, filters in list_instances():
            activos = [f for f in filters if f["enabled"]]
            estado = "" if inst["enabled"] else "  (desactivada)"
            lineas.append(f"• {inst['name']}{estado} — {len(activos)} filtro(s) activo(s)")
        self.jira_detail.config(text="\n".join(lineas) if lineas
                                else "Todavía no hay ningún Jira configurado.")

    def open_jira_manager(self):
        """Abre el gestor y refresca el resumen al cerrarlo."""
        dlg = JiraManagerDialog(self.root)
        self.root.wait_window(dlg)
        self.refresh_jira_summary()
        # Flask cachea poco, pero reiniciarlo garantiza que la próxima
        # sincronización use la configuración recién guardada.
        if flask_proc and flask_proc.poll() is None:
            self.restart_flask()

    def check_for_updates(self, auto_check=False):
        """Check GitHub for a newer version and offer to update."""
        self.update_btn.config(state="disabled", text="Comprobando...")
        self.update_status_label.config(text="Conectando con GitHub...", fg="#888")
        threading.Thread(target=self._check_update_worker, args=(auto_check,), daemon=True).start()

    def _check_update_worker(self, auto_check=False):
        try:
            local_ver = get_local_version()
            remote_ver = get_remote_version()
            changelog = get_remote_changelog(local_ver, remote_ver) if remote_ver else None
        except Exception:
            local_ver = get_local_version()
            remote_ver = None
            changelog = None
        self.root.after(0, self._handle_update_result, local_ver, remote_ver, changelog, auto_check)

    def _handle_update_result(self, local_ver, remote_ver, changelog=None, auto_check=False):
        # Always re-enable the button first, no matter what happens below
        self.update_btn.config(state="normal", text="\U0001F504 Comprobar actualizaciones")
        try:
            self._handle_update_result_inner(local_ver, remote_ver, changelog, auto_check)
        except Exception:
            self.update_status_label.config(text="Error al comprobar actualizaciones.", fg="#e74c3c")

    def _handle_update_result_inner(self, local_ver, remote_ver, changelog=None, auto_check=False):

        if remote_ver is None:
            self.update_status_label.config(
                text="No se pudo conectar con GitHub. Comprueba tu conexión.",
                fg="#e74c3c")
            return

        is_new = local_ver is None or local_ver != remote_ver

        if is_new:
            self.update_status_label.config(
                text=f"Nueva versión disponible: {remote_ver}",
                fg="#f5a623")
            should_update = self._show_changelog_and_ask(changelog, remote_ver)
            if should_update:
                self._start_download()
        else:
            self.update_status_label.config(
                text=f"Ya tienes la última versión ({local_ver})",
                fg="#16c79a")
            # On startup auto-check, don't show dialog if already up to date
            if not auto_check:
                should_reinstall = self._show_changelog_and_ask(changelog, local_ver, is_current=True)
                if should_reinstall:
                    self._start_download()

    def _show_changelog_and_ask(self, changelog, remote_ver, is_current=False):
        """Show changelog in a dialog and ask whether to update. Returns True if user wants to update."""
        title = f"Tu versión - v{remote_ver}" if is_current else f"Novedades - v{remote_ver}"
        header = f"\u2705 Novedades en tu versión (v{remote_ver})" if is_current else f"\U0001F4E2 Novedades en v{remote_ver}"
        update_btn_text = "\U0001F504 Reinstalar versión" if is_current else "\U0001F4E5 Actualizar ahora"
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg="#1a1a2e")
        win.geometry("650x520")
        win.resizable(True, True)
        win.transient(self.root)
        win.grab_set()

        # Header row with title and buttons side by side
        header_frame = tk.Frame(win, bg="#1a1a2e")
        header_frame.pack(fill="x", padx=16, pady=(12, 4))

        tk.Label(header_frame, text=header, bg="#1a1a2e", fg="#16c79a",
                 font=("Segoe UI", 14, "bold")).pack(side="left")

        result = {"update": False}

        def do_update():
            result["update"] = True
            win.destroy()

        def do_cancel():
            win.destroy()

        tk.Button(header_frame, text="Ahora no", bg="#2a2a4a", fg="#e0e0e0",
                  font=("Segoe UI", 9), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=do_cancel).pack(side="right", padx=(4, 0))
        tk.Button(header_frame, text=update_btn_text, bg="#16c79a", fg="#0f0f23",
                  font=("Segoe UI", 9, "bold"), relief="flat", padx=10, pady=4,
                  cursor="hand2", command=do_update).pack(side="right")

        sub_frame = tk.Frame(win, bg="#1a1a2e")
        sub_frame.pack(fill="x", padx=16, pady=(0, 4))
        sub_text = "✅ Ya tienes la última versión instalada" if is_current else f"Nueva versión: {remote_ver}"
        sub_color = "#16c79a" if is_current else "#f5a623"
        tk.Label(sub_frame, text=sub_text, bg="#1a1a2e", fg=sub_color,
                 font=("Segoe UI", 9)).pack(side="left")

        text_frame = tk.Frame(win, bg="#0f0f23")
        text_frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side="right", fill="y")

        text_widget = tk.Text(text_frame, bg="#0f0f23", fg="#e0e0e0", font=("Consolas", 9),
                              wrap="word", relief="flat", yscrollcommand=scrollbar.set,
                              padx=10, pady=10)
        text_widget.pack(fill="both", expand=True)
        scrollbar.config(command=text_widget.yview)

        if changelog:
            text_widget.insert("1.0", changelog)
        else:
            text_widget.insert("1.0", "No se pudo obtener el detalle de cambios.")
        text_widget.config(state="disabled")

        self.root.wait_window(win)
        return result["update"]

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
