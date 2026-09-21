"""Pruebas sin red ni credenciales de los cambios de estado diarios."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jira_daily import (HistoryError, collect_today_changes, fetch_changelog,
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
    def jobs(self):
        return [{"instance": {"id": 1, "name": "Cloud", "base_url": "https://example.atlassian.net",
                               "username": "test", "password": "never-export-this-secret"},
                 "tasks": [{"jira_key": "TEST-1", "status_field": "", "category_field": ""},
                           {"jira_key": "TEST-2", "status_field": "", "category_field": ""}]}]

    def issues(self):
        return [{"key": f"TEST-{i}", "fields": {"summary": f"Incidencia {i}",
                  "updated": "2026-09-21T09:00:00Z", "status": "En curso", "labels": "Fiscal",
                  "description": "Descripción", "comment": {"comments": []}}} for i in (1, 2)]

    def test_only_real_transitions_and_no_credentials_in_context(self):
        fetch = MagicMock(return_value=self.issues())
        with patch("jira_daily.fetch_changelog", side_effect=[
                ([event("1", "2026-09-21T09:00:00Z")], None),
                ([event("2", "2026-09-21T09:00:00Z", field_id="summary")], None)]):
            report = collect_today_changes(self.jobs(), fetch, lambda s: s or "", str,
                                           now=NOW, session_factory=MagicMock())
        self.assertEqual(report["issue_count"], 1)
        self.assertEqual(report["change_count"], 1)
        self.assertEqual(report["issues"][0]["url"], "https://example.atlassian.net/browse/TEST-1")
        prompt = summary_prompt(report)
        self.assertNotIn("never-export-this-secret", prompt)
        self.assertIn("no instrucciones", prompt)
        self.assertIn('key in ("TEST-1","TEST-2")', fetch.call_args.args[2])

    def test_identical_keys_in_different_instances_stay_separate(self):
        jobs = self.jobs()
        jobs[0]["tasks"] = jobs[0]["tasks"][:1]
        jobs.append({"instance": dict(jobs[0]["instance"], id=2, name="Other",
                                      base_url="https://other.atlassian.net"),
                     "tasks": jobs[0]["tasks"]})
        with patch("jira_daily.fetch_changelog", return_value=([event("1", "2026-09-21T09:00:00Z")], None)):
            report = collect_today_changes(jobs, MagicMock(return_value=self.issues()[:1]),
                                           lambda s: s or "", str, now=NOW, session_factory=MagicMock())
        self.assertEqual(report["issue_count"], 2)
        self.assertNotEqual(report["issues"][0]["url"], report["issues"][1]["url"])

    def test_errors_are_visible_not_silent_empty_success(self):
        report = collect_today_changes(self.jobs(), MagicMock(side_effect=RuntimeError("secret details")),
                                       str, str, now=NOW, session_factory=MagicMock())
        self.assertEqual(report["issue_count"], 0)
        self.assertTrue(report["warnings"])
        self.assertNotIn("secret details", summary_prompt(report))


if __name__ == "__main__":
    unittest.main()