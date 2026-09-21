"""Una versión distinta no implica que el ejecutable publicado sea posterior."""

import sys
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher
import version_compare


class UpdateUiTests(unittest.TestCase):
    def test_unverified_different_versions_do_not_offer_downgrade(self):
        panel = MagicMock()
        panel._show_changelog_and_ask.return_value = False
        launcher.LauncherApp._handle_update_result_inner(
            panel, "ff1b655", "0df5169", "Changelog de la versión anterior")
        panel._show_changelog_and_ask.assert_not_called()
        panel._start_download.assert_not_called()

    def test_only_remote_ahead_offers_download(self):
        for relation in ("equal", "local_ahead", "remote_ahead", "diverged", "unknown"):
            with self.subTest(relation=relation):
                panel = MagicMock()
                panel._show_changelog_and_ask.return_value = True
                launcher.LauncherApp._handle_update_result_inner(
                    panel, "ff1b655", "0df5169", "Changelog publicado", relation=relation)
                if relation == "remote_ahead":
                    panel._show_changelog_and_ask.assert_called_once_with("Changelog publicado", "0df5169")
                    panel._start_download.assert_called_once()
                else:
                    panel._show_changelog_and_ask.assert_not_called()
                    panel._start_download.assert_not_called()
                if relation == "local_ahead":
                    self.assertIn("posterior", panel.update_status_label.config.call_args.kwargs["text"])

    def test_missing_remote_never_offers_install(self):
        panel = MagicMock()
        launcher.LauncherApp._handle_update_result_inner(panel, "ff1b655", None)
        panel._show_changelog_and_ask.assert_not_called()

    def test_worker_only_fetches_changelog_for_real_upgrade(self):
        for relation in ("equal", "local_ahead", "remote_ahead", "diverged", "unknown"):
            panel = MagicMock()
            with patch.object(launcher, "get_local_version", return_value="ff1b655"), \
                 patch.object(launcher, "get_remote_version", return_value="0df5169"), \
                 patch.object(launcher, "compare_versions", return_value=relation), \
                 patch.object(launcher, "get_remote_changelog", return_value="Publicado") as changelog:
                launcher.LauncherApp._check_update_worker(panel, False)
            self.assertEqual(changelog.call_count, int(relation == "remote_ahead"))
            self.assertEqual(panel.root.after.call_args.args[-1], relation)


class VersionComparisonTests(unittest.TestCase):
    def test_equal_hash_prefix_or_bom_needs_no_network(self):
        with patch.object(version_compare, "_git") as git, patch.object(version_compare, "urlopen") as remote:
            self.assertEqual(version_compare.compare_versions("\ufeffFF1B655\n", "ff1b655" + "0" * 33), "equal")
            git.assert_not_called()
            remote.assert_not_called()

    def test_local_unpublished_descendant_is_newer_using_git(self):
        results = [MagicMock(returncode=0), MagicMock(returncode=0),
                   MagicMock(returncode=1), MagicMock(returncode=0)]
        with patch.object(version_compare, "_git", side_effect=results) as git, \
             patch.object(version_compare, "urlopen") as remote:
            relation = version_compare.compare_versions("ff1b655", "0df5169", repo_dir="repo")
            self.assertEqual(relation, "local_ahead")
            self.assertEqual(git.call_args.args, ("repo", "merge-base", "--is-ancestor", "0df5169", "ff1b655"))
            remote.assert_not_called()

    def test_remote_descendant_is_newer_using_git(self):
        with patch.object(version_compare, "_git", side_effect=[MagicMock(returncode=0)] * 3):
            self.assertEqual(version_compare.compare_versions("0df5169", "ff1b655", repo_dir="repo"), "remote_ahead")

    def test_diverged_only_if_local_history_is_complete(self):
        results = [MagicMock(returncode=0), MagicMock(returncode=0),
                   MagicMock(returncode=1), MagicMock(returncode=1), MagicMock(returncode=0, stdout="false\n")]
        with patch.object(version_compare, "_git", side_effect=results), patch.object(version_compare, "urlopen") as remote:
            self.assertEqual(version_compare.compare_versions("0df5169", "ff1b655", repo_dir="repo"), "diverged")
            remote.assert_not_called()

    def test_github_compare_direction_and_invalid_payload(self):
        for status, expected in (("ahead", "remote_ahead"), ("behind", "local_ahead"),
                                 ("identical", "equal"), ("diverged", "diverged"), ("unexpected", "unknown")):
            with patch.object(version_compare, "urlopen") as remote:
                remote.return_value.__enter__.return_value.read.return_value = json.dumps({"status": status}).encode()
                self.assertEqual(version_compare.compare_versions("0df5169", "ff1b655"), expected)
                self.assertTrue(remote.call_args.args[0].full_url.endswith("/compare/0df5169...ff1b655"))

    def test_missing_git_commit_falls_back_to_github(self):
        with patch.object(version_compare, "_git", return_value=MagicMock(returncode=128)), \
             patch.object(version_compare, "urlopen") as remote:
            remote.return_value.__enter__.return_value.read.return_value = b'{"status":"ahead"}'
            self.assertEqual(version_compare.compare_versions("0df5169", "ff1b655", repo_dir="repo"), "remote_ahead")

    def test_offline_unpublished_or_invalid_version_never_means_upgrade(self):
        with patch.object(version_compare, "urlopen", side_effect=HTTPError("https://example.test", 404, "missing", {}, None)):
            self.assertEqual(version_compare.compare_versions("ff1b655", "0df5169"), "unknown")
        with patch.object(version_compare, "urlopen") as remote:
            for local in (None, "dev", "bad; command", ""):
                self.assertEqual(version_compare.compare_versions(local, "0df5169"), "unknown")
            remote.assert_not_called()


class PublishedChangelogTests(unittest.TestCase):
    def test_entire_changelog_is_fetched_at_published_commit(self):
        content = "JIRABOARD\n[pendiente] 2026-08-06\nCambios publicados\n[abcd123]\nHistoria\n"
        encoded = base64.b64encode(content.encode()).decode()
        with patch.object(launcher, "urlopen") as remote:
            remote.return_value.__enter__.return_value.read.return_value = json.dumps({"content": encoded}).encode()
            self.assertEqual(launcher.get_remote_changelog("aaaaaaa", "0df5169"), content)
            self.assertIn("ref=0df5169", remote.call_args.args[0].full_url)
            self.assertNotIn("ref=master", remote.call_args.args[0].full_url)

    def test_local_version_with_utf8_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "version.txt").write_text("ff1b655\n", encoding="utf-8-sig")
            with patch.object(launcher, "_BUNDLE_DIR", Path(directory)):
                self.assertEqual(launcher.get_local_version(), "ff1b655")


if __name__ == "__main__":
    unittest.main()