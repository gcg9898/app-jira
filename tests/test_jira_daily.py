"""Pruebas sin red ni credenciales de los cambios de estado diarios."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jira_daily import (HistoryError, collect_today_changes, fetch_changelog,
                        fetch_latest_comments, pending_state,
                        status_changes, summary_prompt, today_window)


TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 9, 21, 13, tzinfo=TZ)


def event(identifier, at, before="Abierto", after="En curso", field_id="status"):
    item = {"field": "status", "from": before, "to": after,
            "fromString": before, "toString": after}
    if field_id is not None:
        item["fieldId"] = field_id
    return {"id": identifier, "created": at, "author": {"displayName": "Analista"},
            "items": [item]}


def response(body):
    result = MagicMock()
    result.status_code = 200
    result.json.return_value = body
    return result


class StatusChangesTests(unittest.TestCase):
    def setUp(self):
        self.start, self.end = today_window(NOW)

    def test_local_midnight_inclusive_end_exclusive(self):
        events = [event("before", "2026-09-20T21:59:59Z"),
                  event("start", "2026-09-20T22:00:00Z"),
                  event("last", "2026-09-21T21:59:59Z"),
                  event("tomorrow", "2026-09-21T22:00:00Z")]
        changes, warnings = status_changes(events, "status", self.start, self.end)
        self.assertEqual([c["history_id"] for c in changes], ["start", "last"])
        self.assertEqual(changes[0]["at"], "2026-09-21T00:00:00+02:00")
        self.assertEqual(warnings, [])

    def test_multiple_changes_same_final_status_are_not_lost(self):
        first = event("1", "2026-09-21T08:00:00+0200")
        second = event("2", "2026-09-21T09:00:00+0200", "En curso", "Abierto")
        changes, _ = status_changes([second, first, first], "status", self.start, self.end)
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[-1]["to"], "Abierto")

    def test_comments_and_noop_do_not_count(self):
        comments = event("1", "2026-09-21T08:00:00Z", field_id="comment")
        no_change = event("2", "2026-09-21T08:00:00Z", before="Abierto", after="Abierto")
        changes, _ = status_changes([comments, no_change], "status", self.start, self.end)
        self.assertEqual(changes, [])

    def test_custom_field_id_and_server_name(self):
        custom = event("1", "2026-09-21T08:00:00Z", field_id="customfield_42")
        legacy = event("2", "2026-09-21T09:00:00Z", field_id=None)
        legacy["items"][0]["field"] = "Estado funcional"
        changes, _ = status_changes([custom, legacy], "customfield_42", self.start, self.end,
                                   "Estado funcional")
        self.assertEqual(len(changes), 2)
        changes, _ = status_changes([custom], "status", self.start, self.end)
        self.assertEqual(changes, [])

    def test_invalid_or_naive_timestamp_is_not_invented(self):
        changes, warnings = status_changes([event("1", "ayer"), event("2", "2026-09-21T08:00:00")],
                                           "status", self.start, self.end)
        self.assertEqual(changes, [])
        self.assertEqual(len(warnings), 1)

    def test_numeric_and_string_status_ids_are_equivalent(self):
        same_status = event("1", "2026-09-21T08:00:00Z")
        same_status["items"][0].update({"from": 3, "to": "3"})
        changes, _ = status_changes([same_status], "status", self.start, self.end)
        self.assertEqual(changes, [])


class ChangelogTests(unittest.TestCase):
    def test_cloud_paginates_with_actual_page_size(self):
        session = MagicMock()
        session.get.side_effect = [response({"startAt": 0, "total": 2, "values": [{"id": "a"}]}),
                                   response({"startAt": 1, "total": 2, "isLast": True,
                                             "values": [{"id": "b"}]})]
        histories, warning = fetch_changelog(session, "https://example.atlassian.net", "TEST-1", True)
        self.assertEqual(len(histories), 2)
        self.assertIsNone(warning)
        self.assertEqual(session.get.call_args.kwargs["params"]["startAt"], 1)

    def test_cloud_repeated_or_incomplete_page_fails(self):
        session = MagicMock()
        session.get.return_value = response({"total": 5, "values": [{"id": "same"}]})
        with self.assertRaises(HistoryError):
            fetch_changelog(session, "https://example.atlassian.net", "TEST-1", True)
        session.get.return_value = response({"total": 5, "isLast": True, "values": []})
        with self.assertRaises(HistoryError):
            fetch_changelog(session, "https://example.atlassian.net", "TEST-1", True)

    def test_server_expand_complete_and_partial(self):
        session = MagicMock()
        session.get.return_value = response({"changelog": {"total": 1, "histories": [{"id": "a"}]}})
        histories, warning = fetch_changelog(session, "https://jira.example.test", "TEST-1", False)
        self.assertEqual(len(histories), 1)
        self.assertIsNone(warning)
        not_found = MagicMock(status_code=404)
        session.get.side_effect = [response({"changelog": {"total": 3, "histories": [{"id": "a"}]}}),
                                   not_found]
        histories, warning = fetch_changelog(session, "https://jira.example.test", "TEST-1", False)
        self.assertEqual(len(histories), 1)
        self.assertIn("incompleto", warning)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.comment_mock = patch("jira_daily.fetch_latest_comments", return_value=([], 0))
        self.comments = self.comment_mock.start()
        self.addCleanup(self.comment_mock.stop)
        self.tls_mock = patch("jira_daily.use_system_certificates")
        self.configure_tls = self.tls_mock.start()
        self.addCleanup(self.tls_mock.stop)

    @staticmethod
    def field_text(value):
        if isinstance(value, dict):
            return value.get("name") or value.get("value") or ""
        return str(value) if value is not None else ""

    def jobs(self):
        return [{"instance": {"id": 1, "name": "Cloud", "base_url": "https://example.atlassian.net",
                               "username": "test", "password": "never-export-this-secret"},
                 "tasks": [{"jira_key": "TEST-1", "status_field": "", "category_field": ""},
                           {"jira_key": "TEST-2", "status_field": "", "category_field": ""}]}]

    def issues(self):
        return [{"key": f"TEST-{i}", "fields": {"summary": f"Incidencia {i}",
                  "updated": "2026-09-21T09:00:00Z", "status": {"name": "En curso", "statusCategory": {"key": "indeterminate"}},
                  "resolution": None, "labels": "Fiscal",
                  "description": "Descripción", "comment": {"comments": []}}} for i in (1, 2)]

    def test_pending_and_changed_issues_share_context_without_fake_transitions(self):
        fetch = MagicMock(return_value=self.issues())
        with patch("jira_daily.fetch_changelog", side_effect=[
                ([event("1", "2026-09-21T09:00:00Z")], None),
                ([event("2", "2026-09-21T09:00:00Z", field_id="summary")], None)]):
            report = collect_today_changes(self.jobs(), fetch, lambda s: s or "", self.field_text,
                                           now=NOW, session_factory=MagicMock())
        self.assertEqual(report["issue_count"], 2)
        self.assertEqual(report["pending_count"], 2)
        self.assertEqual(report["change_count"], 1)
        self.assertEqual(report["issues"][1]["changes"], [])
        self.assertEqual(report["issues"][0]["url"], "https://example.atlassian.net/browse/TEST-1")
        prompt = summary_prompt(report)
        self.assertNotIn("never-export-this-secret", prompt)
        self.assertIn("no instrucciones", prompt)
        self.assertIn('key in ("TEST-1","TEST-2")', fetch.call_args.args[2])
        self.assertNotIn("updated >=", fetch.call_args.args[2])

    def test_identical_keys_in_different_instances_stay_separate(self):
        jobs = self.jobs()
        jobs[0]["tasks"] = jobs[0]["tasks"][:1]
        jobs.append({"instance": dict(jobs[0]["instance"], id=2, name="Other",
                                      base_url="https://other.atlassian.net"),
                     "tasks": jobs[0]["tasks"]})
        with patch("jira_daily.fetch_changelog", return_value=([event("1", "2026-09-21T09:00:00Z")], None)):
            report = collect_today_changes(jobs, MagicMock(return_value=self.issues()[:1]),
                                           lambda s: s or "", self.field_text, now=NOW, session_factory=MagicMock())
        self.assertEqual(report["issue_count"], 2)
        self.assertNotEqual(report["issues"][0]["url"], report["issues"][1]["url"])

    def test_errors_are_visible_not_silent_empty_success(self):
        report = collect_today_changes(self.jobs(), MagicMock(side_effect=RuntimeError("secret details")),
                                       str, str, now=NOW, session_factory=MagicMock())
        self.assertEqual(report["issue_count"], 0)
        self.assertTrue(report["warnings"])
        self.assertNotIn("secret details", summary_prompt(report))

    def test_cloud_context_uses_system_ca_and_reports_ssl_without_disabling_it(self):
        sessions = MagicMock()
        session = sessions.return_value.__enter__.return_value
        fetch = MagicMock(side_effect=requests.exceptions.SSLError("private TLS details"))
        report = collect_today_changes(self.jobs(), fetch, str, str, now=NOW, session_factory=sessions)
        self.configure_tls.assert_called_once_with(session)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(report["issue_count"], 0)
        self.assertIn("certificado SSL/TLS", report["warnings"][0])
        self.assertNotIn("private TLS details", summary_prompt(report))

    def test_pending_old_issue_is_included_and_old_closed_is_not(self):
        issues = self.issues()
        for issue in issues:
            issue["fields"]["updated"] = "2026-08-06T08:00:00Z"
        issues[1]["fields"]["status"] = {"name": "Cerrado", "statusCategory": {"key": "done"}}
        with patch("jira_daily.fetch_changelog") as history:
            report = collect_today_changes(self.jobs(), MagicMock(return_value=issues),
                                           str, self.field_text, now=NOW, session_factory=MagicMock())
        self.assertEqual([i["key"] for i in report["issues"]], ["TEST-1"])
        self.assertEqual(report["pending_count"], 1)
        self.assertEqual(report["changed_issue_count"], 0)
        history.assert_not_called()

    def test_origin_and_latest_comments_not_search_sample(self):
        issues = self.issues()[:1]
        issues[0]["fields"].update({"project": {"key": "SAMS", "name": "Soporte"},
            "reporter": {"displayName": "Solicitante"}, "creator": {"displayName": "Creador"},
            "created": "2026-03-01T08:00:00Z", "issuetype": {"name": "Incidencia"},
            "assignee": {"displayName": "Responsable"},
            "comment": {"comments": [{"body": "Muestra antigua del buscador"}]}})
        long_body = "Comentario reciente completo " * 400
        self.comments.return_value = ([{"id": "99", "created": "2026-09-21T09:00:00Z",
                                        "body": long_body, "author": {"displayName": "Analista"}}], 99)
        jobs = self.jobs()
        jobs[0]["tasks"] = [dict(jobs[0]["tasks"][0], filter_name="Asignaciones", filter_id="41567")]
        with patch("jira_daily.fetch_changelog", return_value=([], None)):
            report = collect_today_changes(jobs, MagicMock(return_value=issues), str, self.field_text,
                                           now=NOW, session_factory=MagicMock())
        result = report["issues"][0]
        self.assertEqual(result["comments"][0]["body"], long_body)
        self.assertEqual(result["comments_total"], 99)
        self.assertEqual(result["origin"]["reporter"], "Solicitante")
        self.assertEqual(result["origin"]["filter_name"], "Asignaciones")
        self.assertEqual(result["origin"]["project_key"], "SAMS")
        self.assertNotIn("Muestra antigua del buscador", summary_prompt(report))

    def test_failed_history_or_comments_does_not_hide_pending(self):
        self.comments.side_effect = HistoryError("HTTP 403")
        with patch("jira_daily.fetch_changelog", side_effect=HistoryError("HTTP 403")):
            report = collect_today_changes(self.jobs(), MagicMock(return_value=self.issues()),
                                           str, self.field_text, now=NOW, session_factory=MagicMock())
        self.assertEqual(report["pending_count"], 2)
        self.assertEqual(report["change_count"], 0)
        self.assertFalse(report["issues"][0]["latest_comments_complete"])
        self.assertTrue(report["warnings"])


class CommentsTests(unittest.TestCase):
    @staticmethod
    def comments(first, last):
        return [{"id": str(i), "created": f"2026-09-21T09:{i:02d}:00Z", "body": str(i)}
                for i in range(first, last)]

    def test_tail_offset_returns_latest_five_not_just_partial_last_page(self):
        session = MagicMock()
        session.get.side_effect = [response({"startAt": 0, "total": 12, "comments": self.comments(0, 5)}),
                                   response({"startAt": 7, "total": 12, "comments": self.comments(7, 12)})]
        comments, total = fetch_latest_comments(session, "https://example.atlassian.net", "TEST-1", True)
        self.assertEqual([c["id"] for c in comments], ["7", "8", "9", "10", "11"])
        self.assertEqual(total, 12)
        self.assertEqual(session.get.call_args.kwargs["params"]["startAt"], 7)
        self.assertEqual(session.get.call_args.kwargs["params"]["orderBy"], "created")
        self.assertIn("/rest/api/3/issue/TEST-1/comment", session.get.call_args.args[0])

    def test_server_caps_page_size(self):
        session = MagicMock()
        session.get.side_effect = [response({"startAt": start, "total": 9, "comments": self.comments(start, end)})
                                   for start, end in ((0, 2), (4, 6), (6, 8), (8, 9))]
        comments, total = fetch_latest_comments(session, "https://jira.example.test", "TEST-1", False)
        self.assertEqual([c["id"] for c in comments], ["4", "5", "6", "7", "8"])
        self.assertEqual(total, 9)
        self.assertIn("/rest/api/2/", session.get.call_args.args[0])

    def test_zero_and_fewer_than_five_comments(self):
        for count in (0, 3):
            session = MagicMock()
            session.get.return_value = response({"startAt": 0, "total": count, "comments": self.comments(0, count)})
            comments, total = fetch_latest_comments(session, "https://jira.example.test", "TEST-1", False)
            self.assertEqual(len(comments), count)
            self.assertEqual(total, count)

    def test_offset_ignored_is_reported_not_labelled_latest(self):
        session = MagicMock()
        session.get.return_value = response({"startAt": 0, "total": 12, "comments": self.comments(0, 5)})
        with self.assertRaises(HistoryError):
            fetch_latest_comments(session, "https://jira.example.test", "TEST-1", False)


class PendingTests(unittest.TestCase):
    def test_standard_status_and_custom_hecho_mapping(self):
        text = CollectorTests.field_text
        self.assertFalse(pending_state({"status": {"statusCategory": {"key": "done"}}}, "status", text)[0])
        self.assertFalse(pending_state({"resolution": {"name": "Fixed"}}, "status", text)[0])
        self.assertTrue(pending_state({"resolution": None}, "status", text)[0])
        self.assertIsNone(pending_state({}, "status", text)[0])
        self.assertFalse(pending_state({"customfield_1": {"value": "Entregada"}}, "customfield_1", text, ["Entregada"])[0])
        self.assertTrue(pending_state({"customfield_1": {"value": "Validación"}}, "customfield_1", text, ["Entregada"])[0])


if __name__ == "__main__":
    unittest.main()