"""Tests for Amdocs network allowlist in enterprise_security."""
import os
import sys
import unittest

SRC_DIR = os.path.join(os.path.dirname(__file__), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from enterprise_security import (  # noqa: E402
    check_request_access,
    client_in_network_allowlist,
    network_restriction_enabled,
)


class NetworkAllowlistTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in ("CRM_NETWORK_RESTRICT", "CRM_NETWORK_ALLOWLIST", "CRM_API_TOKEN")
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_amdocs_10_range_allowed(self):
        os.environ["CRM_NETWORK_RESTRICT"] = "1"
        os.environ.pop("CRM_NETWORK_ALLOWLIST", None)
        self.assertTrue(client_in_network_allowlist("10.19.164.180"))

    def test_home_network_blocked(self):
        os.environ["CRM_NETWORK_RESTRICT"] = "1"
        os.environ.pop("CRM_NETWORK_ALLOWLIST", None)
        self.assertFalse(client_in_network_allowlist("192.168.1.50"))

    def test_public_internet_blocked(self):
        os.environ["CRM_NETWORK_RESTRICT"] = "1"
        os.environ.pop("CRM_NETWORK_ALLOWLIST", None)
        self.assertFalse(client_in_network_allowlist("8.8.8.8"))

    def test_localhost_always_allowed_via_check_request(self):
        os.environ["CRM_NETWORK_RESTRICT"] = "1"
        allowed, _, status = check_request_access(
            "127.0.0.1",
            "/crm",
            "",
            "GET",
            {},
        )
        self.assertTrue(allowed)
        self.assertEqual(status, 200)

    def test_remote_non_amdocs_denied(self):
        os.environ["CRM_NETWORK_RESTRICT"] = "1"
        os.environ.pop("CRM_NETWORK_ALLOWLIST", None)
        allowed, message, status = check_request_access(
            "203.0.113.10",
            "/crm",
            "",
            "GET",
            {},
        )
        self.assertFalse(allowed)
        self.assertEqual(status, 403)
        self.assertIn("Amdocs", message)

    def test_custom_allowlist(self):
        os.environ["CRM_NETWORK_RESTRICT"] = "1"
        os.environ["CRM_NETWORK_ALLOWLIST"] = "192.168.50.0/24"
        self.assertTrue(client_in_network_allowlist("192.168.50.10"))
        self.assertFalse(client_in_network_allowlist("10.19.164.180"))

    def test_restriction_can_be_disabled(self):
        os.environ["CRM_NETWORK_RESTRICT"] = "0"
        self.assertFalse(network_restriction_enabled())
        self.assertTrue(client_in_network_allowlist("8.8.8.8"))


if __name__ == "__main__":
    unittest.main()
