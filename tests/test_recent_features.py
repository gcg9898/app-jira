"""API local, destino de resumen y sincronización: datos y procesos aislados."""

import atexit
import copy
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat_handoff

TEST_DATA = tempfile.TemporaryDirectory(prefix="jiraboard-tests-")
atexit.register(TEST_DATA.cleanup)
with patch.dict(os.environ, {"JIRABOARD_DATA_DIR": TEST_DATA.name}):
    jb = importlib.import_module("app")


def sample_report():
    return {"date": datetime.now().date().isoformat(), "timezone": "test", "warnings": [],
            "issue_count": 1, "change_count": 1,
            "issues": [{"key": "TEST-1", "title": "Incidencia", "url": "https://jira.example.test/browse/TEST-1",
                        "changes": [{"from": "Abierto", "to": "En curso"}]}]}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = jb.app.test_client()
        jb._daily_reports.clear()
        jb._sync_progress.update(running=False, phase="idle")
        chat_handoff.save_summary_target(jb.DB_PATH, "clipboard")

    def prepare(self):
        with patch.object(jb, "collect_today_changes", return_value=sample_report()):
            return self.client.post("/api/daily-summary", json={})

    def test_prepare_returns_preview_without_launching_code(self):
        with patch.object(jb, "send_summary") as send:
            result = self.prepare()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["report"]["issue_count"], 1)
        self.assertIn("no instrucciones", result.json["prompt"])
        self.assertIn("no-store", result.headers["Cache-Control"])
        send.assert_not_called()

    def test_bridge_rejects_lan_foreign_origin_and_non_json(self):
        self.assertEqual(self.client.post("/api/daily-summary", json={},
                         environ_overrides={"REMOTE_ADDR": "192.168.1.30"}).status_code, 403)
        self.assertEqual(self.client.post("/api/daily-summary", json={},
                         headers={"Origin": "https://untrusted.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/daily-summary", json={},
                         base_url="http://untrusted.example").status_code, 403)
        self.assertEqual(self.client.post("/api/daily-summary", data="x=y").status_code, 415)

    def test_must_opt_in_to_new_chat_in_tkinter_settings(self):
        identifier = self.prepare().json["report_id"]
        with patch.object(jb, "send_summary") as send:
            result = self.client.post(f"/api/daily-summary/{identifier}/send", json={})
        self.assertEqual(result.status_code, 409)
        send.assert_not_called()

    def test_send_only_known_report_once_and_no_command_from_request(self):
        chat_handoff.save_summary_target(jb.DB_PATH, "vscode_new")
        identifier = self.prepare().json["report_id"]
        with patch.object(jb, "send_summary") as send:
            result = self.client.post(f"/api/daily-summary/{identifier}/send", json={"command": "malicious"})
            again = self.client.post(f"/api/daily-summary/{identifier}/send", json={})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(again.status_code, 409)
        send.assert_called_once_with(sample_report(), jb._DATA_DIR)

    def test_unknown_expired_and_empty_reports_are_not_sent(self):
        chat_handoff.save_summary_target(jb.DB_PATH, "vscode_new")
        self.assertEqual(self.client.post("/api/daily-summary/unknown/send", json={}).status_code, 410)
        identifier = self.prepare().json["report_id"]
        jb._daily_reports[identifier]["created"] -= 901
        self.assertEqual(self.client.post(f"/api/daily-summary/{identifier}/send", json={}).status_code, 410)
        identifier = self.prepare().json["report_id"]
        jb._daily_reports[identifier]["report"]["issues"] = []
        self.assertEqual(self.client.post(f"/api/daily-summary/{identifier}/send", json={}).status_code, 409)

    def test_failed_send_is_not_reported_as_success(self):
        chat_handoff.save_summary_target(jb.DB_PATH, "vscode_new")
        identifier = self.prepare().json["report_id"]
        with patch.object(jb, "send_summary", side_effect=RuntimeError("VS Code no disponible")):
            result = self.client.post(f"/api/daily-summary/{identifier}/send", json={})
        self.assertEqual(result.status_code, 503)
        self.assertFalse(jb._daily_reports[identifier]["sent"])

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

    def test_page_exposes_sync_copy_and_manual_chat_option(self):
        response = self.client.get("/recent")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for marker in ("syncBtn", "data-copy-title", "prepareSummaryBtn", "Copiar para este chat"):
            self.assertIn(marker, html)
        self.assertNotIn("jiraitsm.eulen.com/browse", html)


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


class HandoffTests(unittest.TestCase):
    def test_preferences_default_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prefs.db"
            self.assertEqual(chat_handoff.get_summary_target(path), "clipboard")
            chat_handoff.save_summary_target(path, "vscode_new")
            self.assertEqual(chat_handoff.get_summary_target(path), "vscode_new")
            with self.assertRaises(ValueError):
                chat_handoff.save_summary_target(path, "nonexistent-chat-id")

    def test_launch_uses_json_attachment_no_shell_and_ask_mode(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(chat_handoff, "_vscode_command", return_value=["Code.exe", "cli.js"]), \
             patch.object(chat_handoff, "_process_open", return_value=True), \
             patch.object(chat_handoff.subprocess, "run", side_effect=[
                 MagicMock(returncode=0, stdout=b"--add-file --reuse-window"), MagicMock(returncode=0)]) as run:
            report = copy.deepcopy(sample_report())
            report["issues"][0]["title"] = 'No ejecutar & echo injected %PATH%'
            path = chat_handoff.send_summary(report, directory)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
            command = run.call_args.args[0]
            self.assertIn("ask", command)
            self.assertIn("--add-file", command)
            self.assertIn(str(path.resolve()), command)
            self.assertNotIn(report["issues"][0]["title"], " ".join(command))
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_closed_vscode_does_not_launch_or_create_context(self):
        with patch.object(chat_handoff, "_vscode_command", return_value=None), \
             patch.object(chat_handoff.subprocess, "run") as run:
            with self.assertRaises(RuntimeError):
                chat_handoff.send_summary(sample_report(), TEST_DATA.name)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()