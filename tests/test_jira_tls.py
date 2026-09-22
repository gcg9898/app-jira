"""Confianza TLS del sistema: verificación activa, sin reintentos inseguros."""

import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jira_tls import SystemTrustAdapter, request_error_detail, use_system_certificates


class TlsTests(unittest.TestCase):
    def test_system_roots_keep_host_and_certificate_validation(self):
        adapter = SystemTrustAdapter()
        try:
            self.assertTrue(adapter.system_context.check_hostname)
            self.assertEqual(adapter.system_context.verify_mode, ssl.CERT_REQUIRED)
            req = requests.Request("GET", "https://example.atlassian.net/rest/api/3/myself").prepare()
            host, pool = adapter.build_connection_pool_key_attributes(req, True)
            self.assertEqual(host["host"], "example.atlassian.net")
            self.assertIs(pool["ssl_context"], adapter.system_context)
        finally:
            adapter.close()

    def test_custom_ca_bundle_and_client_cert_are_preserved(self):
        adapter = SystemTrustAdapter()
        req = requests.Request("GET", "https://example.atlassian.net").prepare()
        try:
            _, pool = adapter.build_connection_pool_key_attributes(req, requests.certs.where(),
                                                                    ("client.pem", "client.key"))
            self.assertNotIn("ssl_context", pool)
            self.assertEqual(pool["ca_certs"], requests.certs.where())
            self.assertEqual(pool["cert_file"], "client.pem")
            self.assertEqual(pool["key_file"], "client.key")
        finally:
            adapter.close()

    def test_adapter_is_per_session_without_global_ssl_patch(self):
        before = ssl.SSLContext
        session = MagicMock()
        with patch("jira_tls.SystemTrustAdapter") as adapter:
            use_system_certificates(session)
        session.mount.assert_called_once_with("https://", adapter.return_value)
        self.assertIs(session.verify, True)
        self.assertIs(ssl.SSLContext, before)

    def test_ssl_error_has_actionable_safe_message(self):
        detail = request_error_detail(requests.exceptions.SSLError("do-not-show-proxy-password"))
        self.assertIn("SSL/TLS", detail)
        self.assertIn("Windows", detail)
        self.assertIn("REQUESTS_CA_BUNDLE", detail)
        self.assertNotIn("do-not-show-proxy-password", detail)


if __name__ == "__main__":
    unittest.main()