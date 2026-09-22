"""API de contexto para copiar y sincronización: datos y procesos aislados."""

import atexit
import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEST_DATA = tempfile.TemporaryDirectory(prefix="jiraboard-tests-")
atexit.register(TEST_DATA.cleanup)
with patch.dict(os.environ, {"JIRABOARD_DATA_DIR": TEST_DATA.name}):
    jb = importlib.import_module("app")


def sample_report():
    return {"date": datetime.now().date().isoformat(), "timezone": "test", "warnings": [],
            "issue_count": 1, "change_count": 1, "pending_count": 1,
            "changed_issue_count": 1, "comment_count": 1, "tracked": 1,
            "issues": [{"key": "TEST-1", "title": "Incidencia", "url": "https://jira.example.test/browse/TEST-1",
                "description": "Problema comunicado", "origin": {"jira": "Test", "reporter": "Solicitante"},
                "pending": True, "updated_today": True,
                "comments": [{"author": "Analista", "created": "2026-09-21T10:00:00Z", "body": "Último comentario"}],
                "changes": [{"from": "Abierto", "to": "En curso"}]}]}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = jb.app.test_client()
        jb._sync_progress.update(running=False, phase="idle")

    def prepare(self):
        with patch.object(jb, "collect_today_changes", return_value=sample_report()):
            return self.client.post("/api/daily-summary", json={})

    def test_prepare_returns_preview_without_launching_code(self):
        with patch("subprocess.Popen") as process, patch("subprocess.run") as run:
            result = self.prepare()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["report"]["issue_count"], 1)
        self.assertIn("no instrucciones", result.json["prompt"])
        self.assertIn("Problema comunicado", result.json["prompt"])
        self.assertIn("Último comentario", result.json["prompt"])
        self.assertNotIn("report_id", result.json)
        self.assertIn("no-store", result.headers["Cache-Control"])
        process.assert_not_called()
        run.assert_not_called()

    def test_context_rejects_lan_foreign_origin_and_non_json(self):
        self.assertEqual(self.client.post("/api/daily-summary", json={},
                         environ_overrides={"REMOTE_ADDR": "192.168.1.30"}).status_code, 403)
        self.assertEqual(self.client.post("/api/daily-summary", json={},
                         headers={"Origin": "https://untrusted.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/daily-summary", json={},
                         base_url="http://untrusted.example").status_code, 403)
        self.assertEqual(self.client.post("/api/daily-summary", data="x=y").status_code, 415)

    def test_chat_routes_have_been_removed(self):
        self.assertEqual(self.client.get("/api/copilot/status").status_code, 404)
        self.assertEqual(self.client.post("/api/daily-summary/anything/send", json={}).status_code, 404)
        self.assertFalse(hasattr(jb, "send_summary"))

    def test_migration_removes_only_old_chat_preference(self):
        conn = jb.get_db()
        conn.execute("CREATE TABLE IF NOT EXISTS app_preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO app_preferences VALUES ('summary_target', 'vscode_new')")
        conn.execute("INSERT OR REPLACE INTO app_preferences VALUES ('other_setting', 'keep')")
        conn.commit()
        conn.close()
        jb.migrate_db()
        conn = jb.get_db()
        try:
            self.assertIsNone(conn.execute("SELECT 1 FROM app_preferences WHERE key='summary_target'").fetchone())
            self.assertEqual(conn.execute("SELECT value FROM app_preferences WHERE key='other_setting'").fetchone()[0], "keep")
        finally:
            conn.close()

    def test_context_failure_releases_prepare_lock(self):
        with jb.app.test_request_context("/api/daily-summary", method="POST", json={},
                                         environ_overrides={"REMOTE_ADDR": "127.0.0.1"}), \
             patch.object(jb, "collect_today_changes", side_effect=RuntimeError("test")):
            with self.assertRaises(RuntimeError):
                jb.prepare_daily_summary()
        self.assertFalse(jb._daily_prepare_lock.locked())

    def test_parallel_sync_is_rejected_and_errors_reset_progress(self):
        jb._sync_progress.update(running=True, phase="screenshots")
        with patch.object(jb, "_run_jira_sync") as run:
            result = self.client.post("/api/sync-jira")
        self.assertEqual(result.status_code, 409)
        self.assertTrue(result.json["running"])
        run.assert_not_called()
        jb._sync_progress["running"] = False
        with jb.app.test_request_context("/api/sync-jira", method="POST"), \
             patch.object(jb, "_run_jira_sync", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError):
                jb.sync_jira()
        self.assertFalse(jb._sync_progress["running"])

    def test_prepare_during_sync_or_another_prepare_leaves_locks_intact(self):
        jb._sync_progress.update(running=True, phase="fetching")
        self.assertEqual(self.client.post("/api/daily-summary", json={}).status_code, 409)
        self.assertFalse(jb._daily_prepare_lock.locked())
        jb._sync_progress["running"] = False
        jb._daily_prepare_lock.acquire()
        try:
            self.assertEqual(self.client.post("/api/daily-summary", json={}).status_code, 409)
            self.assertTrue(jb._daily_prepare_lock.locked())
        finally:
            jb._daily_prepare_lock.release()

    def test_page_exposes_sync_and_copy_only(self):
        response = self.client.get("/recent")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for marker in ("syncBtn", "data-copy-title", "prepareSummaryBtn", "Obtener y copiar contexto", "Ctrl+V"):
            self.assertIn(marker, html)
        self.assertNotIn("jiraitsm.eulen.com/browse", html)
        self.assertIn('class="jira-link"', html)
        self.assertIn('rel="noopener noreferrer"', html)
        for marker in ("sendSummaryBtn", "Copiar para este chat", "/api/copilot/status", "preparedSummary.report_id"):
            self.assertNotIn(marker, html)

    def test_launcher_has_no_chat_configuration(self):
        import launcher
        self.assertFalse(hasattr(launcher, "SummarySettingsDialog"))
        self.assertFalse(hasattr(launcher.LauncherApp, "open_summary_settings"))


class JobScopeTests(unittest.TestCase):
    def test_only_active_filters_not_deleted_tasks_include_disappeared(self):
        conn = jb.get_db()
        try:
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM jira_filters")
            conn.execute("DELETE FROM jira_instances")
            conn.execute("INSERT INTO jira_instances(id,name,base_url) VALUES (10,'Test','https://jira.example.test')")
            conn.execute("INSERT INTO jira_filters(id,instance_id,name,filter_id) VALUES (10,10,'Active','1')")
            conn.execute("INSERT INTO jira_filters(id,instance_id,name,filter_id,enabled) VALUES (11,10,'Off','2',0)")
            column = conn.execute("SELECT id FROM columns LIMIT 1").fetchone()[0]
            for key, filt, deleted, state in (("TEST-1", 10, 0, "Abierto"),
                                             ("TEST-2", 10, 0, "Desaparecida del filtro"),
                                             ("TEST-3", 11, 0, "Abierto"),
                                             ("TEST-4", 10, 1, "Abierto")):
                conn.execute("""INSERT INTO tasks(column_id,title,jira_key,jira_instance_id,jira_filter_id,deleted,jira_status)
                                VALUES (?, 'Test', ?, 10, ?, ?, ?)""", (column, key, filt, deleted, state))
            conn.commit()
        finally:
            conn.close()
        jobs = jb._daily_summary_jobs()
        self.assertEqual({t["jira_key"] for t in jobs[0]["tasks"]}, {"TEST-1", "TEST-2"})
        self.assertEqual(jobs[0]["tasks"][0]["filter_name"], "Active")
        self.assertIn("Cerrado", jobs[0]["resolved_statuses"])


class CloudSearchTests(unittest.TestCase):
    def test_repeated_cursor_is_error_not_partial_success(self):
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=200)
        session.post.return_value.json.return_value = {"issues": [{"key": "TEST-1"}], "nextPageToken": "same"}
        with self.assertRaises(RuntimeError):
            jb._fetch_issues_cloud(session, "https://example.atlassian.net", "filter=1", ["status"])

    def test_empty_page_with_cursor_continues(self):
        first, last = MagicMock(status_code=200), MagicMock(status_code=200)
        first.json.return_value = {"issues": [], "nextPageToken": "next"}
        last.json.return_value = {"issues": [{"key": "TEST-1"}], "isLast": True}
        session = MagicMock()
        session.post.side_effect = [first, last]
        self.assertEqual(jb._fetch_issues_cloud(session, "https://example.atlassian.net", "filter=1", ["status"]),
                         [{"key": "TEST-1"}])


class IssueLinkTests(unittest.TestCase):
    def test_link_uses_own_instance_and_encodes_key(self):
        instances = {1: {"name": "Cloud", "base_url": "https://cloud.atlassian.net/", "color": "#fff"},
                     2: {"name": "Server", "base_url": "https://jira.example.test/context", "color": "#fff"}}
        cloud = jb._attach_origin({"jira_key": "SAMS-170", "jira_instance_id": 1}, instances)
        server = jb._attach_origin({"jira_key": "SAMS-170", "jira_instance_id": 2}, instances)
        self.assertEqual(cloud["jira_url"], "https://cloud.atlassian.net/browse/SAMS-170")
        self.assertEqual(server["jira_url"], "https://jira.example.test/context/browse/SAMS-170")
        special = jb._attach_origin({"jira_key": 'TEST-1"', "jira_instance_id": 1}, instances)
        self.assertTrue(special["jira_url"].endswith("TEST-1%22"))

    def test_unknown_instance_and_unsafe_scheme_have_no_guessed_link(self):
        self.assertEqual(jb._attach_origin({"jira_key": "SAMS-170"}, {})["jira_url"], "")
        instances = {1: {"name": "Test", "base_url": "javascript:alert(1)", "color": "#fff"}}
        self.assertEqual(jb._attach_origin({"jira_key": "TEST-1", "jira_instance_id": 1}, instances)["jira_url"], "")


if __name__ == "__main__":
    unittest.main()