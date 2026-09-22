"""Comprobación opcional con Chrome headless y servidor temporal en el mismo proceso."""

import copy
import threading
from unittest.mock import patch

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from werkzeug.serving import make_server

from test_recent_features import jb, sample_report


def main():
    server = make_server("127.0.0.1", 0, jb.app, threaded=True)
    base_url = f"http://127.0.0.1:{server.server_port}"

    @jb.app.get("/jira-preview/browse/<key>")
    def preview_issue(key):
        return f"Incidencia de prueba: {key}", 200, {"Content-Type": "text/plain; charset=utf-8"}

    report = sample_report()
    report.update(issue_count=2, pending_count=2, changed_issue_count=1, comment_count=2)
    first = report["issues"][0]
    first.update(instance="Jira de prueba", current_status="En curso", comments_total=8,
                 latest_comments_complete=True, other_changes=[])
    first["changes"][0].update(at="2026-09-21T10:00:00+02:00", author="Analista")
    second = copy.deepcopy(first)
    second.update(key="TEST-2", title="Pendiente antiguo sin cambios hoy", updated_today=False,
                  changes=[], url="https://jira.example.test/browse/TEST-2")
    report["issues"].append(second)
    conn = jb.get_db()
    try:
        column = conn.execute("SELECT id FROM columns LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO jira_instances(name,base_url) VALUES (?,?)", ("Jira preview", base_url + "/jira-preview"))
        inst_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for issue in report["issues"]:
            conn.execute("INSERT INTO tasks(column_id,title,jira_key,jira_status,jira_instance_id) VALUES (?,?,?,?,?)",
                         (column, issue["title"], issue["key"], "En curso", inst_id))
        conn.commit()
    finally:
        conn.close()

    def sync():
        jb._sync_progress.update(running=False, phase="done")
        return jb.jsonify(ok=True, total=2, imported=0, errors=[])

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1360,1000")
    try:
        with patch.object(jb, "collect_today_changes", return_value=report), \
             patch.object(jb, "_run_jira_sync", side_effect=sync), \
             webdriver.Chrome(options=options) as driver:
            driver.set_page_load_timeout(15)
            driver.get(base_url + "/recent")
            wait = WebDriverWait(driver, 15)
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "[data-copy-title]")) == 2)
            original_tab = driver.current_window_handle
            link = driver.find_element(By.CSS_SELECTOR, ".task-card a.jira-link")
            self_url = base_url + "/jira-preview/browse/TEST-1"
            assert link.get_attribute("href") == self_url
            assert link.text == "link ↗"
            assert "noopener" in link.get_attribute("rel")
            link.click()
            wait.until(lambda d: len(d.window_handles) == 2)
            driver.switch_to.window(next(h for h in driver.window_handles if h != original_tab))
            wait.until(lambda d: d.current_url == self_url)
            assert "TEST-1" in driver.find_element(By.TAG_NAME, "body").text
            driver.close()
            driver.switch_to.window(original_tab)
            print("OK: link abre directamente la incidencia en una pestaña nueva; no copia la URL.")
            driver.execute_script("""
                Object.defineProperty(navigator, 'clipboard', {configurable:true,
                    value:{writeText:async text => {window.copiedContext=text;}}});
            """)
            button = driver.find_element(By.ID, "prepareSummaryBtn")
            assert "Obtener y copiar contexto" in button.text
            button.click()
            wait.until(lambda d: d.execute_script("return Boolean(window.copiedContext)"))
            copied = driver.execute_script("return window.copiedContext")
            for expected in ("TEST-1", "TEST-2", "Problema comunicado", "Último comentario", "Solicitante",
                             "https://jira.example.test/browse/TEST-2"):
                assert expected in copied, expected
            assert not driver.find_elements(By.ID, "sendSummaryBtn")
            assert len(driver.find_elements(By.CSS_SELECTOR, "#summaryIssues > li")) == 2
            assert driver.find_element(By.ID, "summaryCopyFallback").get_attribute("value") == copied
            print("OK: botón copia cambios, pendiente antigua, origen, enlace y comentarios.")

            driver.execute_script("""
                Object.defineProperty(navigator, 'clipboard', {configurable:true,
                    value:{writeText:async () => {throw new Error('blocked');}}});
                document.execCommand = () => false;
            """)
            driver.find_element(By.ID, "copySummaryBtn").click()
            field = driver.find_element(By.ID, "summaryCopyFallback")
            wait.until(lambda _d: field.is_displayed())
            assert field.get_attribute("value") == copied
            assert driver.execute_script("return document.getElementById('summaryCopyFallback').selectionEnd") == len(copied)
            print("OK: con portapapeles bloqueado se muestra y selecciona el texto para Ctrl+C.")

            driver.find_element(By.ID, "syncBtn").click()
            wait.until(lambda d: d.find_element(By.ID, "syncBtn").is_enabled())
            assert len(driver.find_elements(By.CSS_SELECTOR, ".task-card")) == 2
            errors = [e for e in driver.get_log("browser")
                      if e["level"] == "SEVERE" and "favicon.ico" not in e["message"]]
            assert not errors, str(errors)
            print("OK: sincronización desde Últimas actualizaciones y sin errores JavaScript.")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()