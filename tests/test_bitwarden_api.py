import socket
import io
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests


APPS = Path(__file__).resolve().parents[1] / "apps"
sys.path.insert(0, str(APPS))

from BillCollector import (  # noqa: E402
    WebRetriDoc,
    defs,
    get_item_by_name,
    get_json,
    get_totp,
    is_api_url_local,
    pinned_api_url,
    private_api_request,
    post_json,
)


def address_info(*addresses):
    return [
        (socket.AF_INET6 if ":" in address else socket.AF_INET,
         socket.SOCK_STREAM, 6, "", (address, 8087))
        for address in addresses
    ]


class BitwardenApiUrlTests(unittest.TestCase):
    def test_accepts_loopback_api(self):
        with patch("BillCollector.socket.getaddrinfo",
                   return_value=address_info("127.0.0.1")):
            self.assertTrue(is_api_url_local("http://localhost:8087"))

    def test_accepts_private_container_address(self):
        with patch("BillCollector.socket.getaddrinfo",
                   return_value=address_info("172.20.0.3")):
            self.assertTrue(is_api_url_local("http://bitwarden-cli:8087"))

    def test_ipv6_loopback_is_pinned_and_https_is_rejected(self):
        with patch("BillCollector.socket.getaddrinfo", return_value=address_info("::1")):
            self.assertEqual(pinned_api_url("http://bitwarden-cli:8087/status"),
                             ("http://[::1]:8087/status", "bitwarden-cli:8087"))
            self.assertFalse(is_api_url_local("https://bitwarden-cli:8087"))

    def test_rejects_public_api(self):
        with patch("BillCollector.socket.getaddrinfo",
                   return_value=address_info("8.8.8.8")):
            self.assertFalse(is_api_url_local("https://bw-api.example:8087"))

    def test_rejects_non_rfc1918_non_public_address(self):
        with patch("BillCollector.socket.getaddrinfo",
                   return_value=address_info("192.0.2.1")):
            self.assertFalse(is_api_url_local("http://bw-api.example:8087"))

    def test_rejects_mixed_private_and_public_dns(self):
        with patch("BillCollector.socket.getaddrinfo",
                   return_value=address_info("172.20.0.3", "8.8.8.8")):
            self.assertFalse(is_api_url_local("http://bw-api.example:8087"))

    def test_rejects_invalid_or_unresolvable_url(self):
        self.assertFalse(is_api_url_local("not-a-url"))
        with patch("BillCollector.socket.getaddrinfo",
                   side_effect=socket.gaierror):
            self.assertFalse(is_api_url_local("http://missing:8087"))

    @patch.dict("BillCollector.os.environ", {"BW_API_HOST": "127.0.0.1:8087"})
    @patch("BillCollector.socket.getaddrinfo", return_value=address_info("172.20.0.3"))
    @patch("BillCollector.requests.Session")
    def test_get_uses_configured_host_header(self, session_class, _dns):
        response = MagicMock(text='{"success": true}', status_code=200)
        session = session_class.return_value.__enter__.return_value
        session.request.return_value = response

        self.assertEqual(get_json("http://bitwarden-cli:8087/status"),
                         '{"success": true}')
        session.request.assert_called_once_with(
            "GET", "http://172.20.0.3:8087/status",
            headers={"Host": "127.0.0.1:8087"}, timeout=10,
            allow_redirects=False)
        self.assertIs(session.trust_env, False)

    @patch.dict("BillCollector.os.environ", {"BW_API_HOST": "127.0.0.1:8087"})
    @patch("BillCollector.socket.getaddrinfo", return_value=address_info("172.20.0.3"))
    @patch("BillCollector.requests.Session")
    def test_post_uses_configured_host_header(self, session_class, _dns):
        response = MagicMock(status_code=200)
        response.json.return_value = {"success": True}
        session = session_class.return_value.__enter__.return_value
        session.request.return_value = response

        self.assertEqual(
            post_json("http://bitwarden-cli:8087/sync", None),
            '{"success": true}')
        session.request.assert_called_once_with(
            "POST", "http://172.20.0.3:8087/sync",
            json=None,
            headers={"Host": "127.0.0.1:8087"}, timeout=10,
            allow_redirects=False)

    @patch("BillCollector.socket.getaddrinfo", return_value=address_info("172.20.0.3"))
    @patch("BillCollector.requests.Session")
    def test_post_non_json_success_body_fails_cleanly(self, session_class, _dns):
        response = MagicMock(status_code=200)
        response.json.side_effect = ValueError("not json")
        session_class.return_value.__enter__.return_value.request.return_value = response
        with self.assertLogs("BillCollector", level="ERROR"):
            self.assertIs(post_json("http://bitwarden-cli:8087/sync", None), False)

    def test_dual_stack_private_resolution_pins_ipv4(self):
        with patch("BillCollector.socket.getaddrinfo",
                   return_value=address_info("fd00::5", "172.20.0.3", "10.0.0.9")):
            self.assertEqual(pinned_api_url("http://bitwarden-cli:8087/status")[0],
                             "http://10.0.0.9:8087/status")

    @patch("BillCollector.get_json")
    def test_item_lookup_ignores_ini_lowercasing(self, get):
        get.return_value = '{"data":{"data":[{"id":"wanted","name":"winSIM Stefan"}]}}'
        self.assertEqual(get_item_by_name("http://bitwarden-cli:8087", "winsim Stefan")["id"], "wanted")

    @patch("BillCollector.get_json")
    def test_item_lookup_rejects_case_variant_duplicates(self, get):
        get.return_value = ('{"data":{"data":['
                            '{"id":"a","name":"winSIM Stefan"},'
                            '{"id":"b","name":"WINSIM Stefan"}]}}')
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            get_item_by_name("http://bitwarden-cli:8087", "winsim Stefan")

    @patch("BillCollector.get_json")
    def test_item_lookup_requires_one_exact_name(self, get):
        get.return_value = (
            '{"data":{"data":['
            '{"id":"wanted","name":"Free Collonges"},'
            '{"id":"other","name":"Free Collonges Archive"}'
            ']}}'
        )

        item = get_item_by_name(
            "http://bitwarden-cli:8087", "Free Collonges")

        self.assertEqual(item["id"], "wanted")
        get.assert_called_once_with(
            "http://bitwarden-cli:8087/list/object/items"
            "?search=Free%20Collonges")

    @patch("BillCollector.get_json")
    def test_item_lookup_rejects_missing_match(self, get):
        get.return_value = '{"data":{"data":[]}}'

        with self.assertRaisesRegex(RuntimeError, "found 0"):
            get_item_by_name(
                "http://bitwarden-cli:8087", "Free Collonges")

    @patch("BillCollector.get_json")
    def test_item_lookup_rejects_duplicate_exact_names(self, get):
        get.return_value = ('{"data":{"data":['
                            '{"id":"first","name":"Portal"},'
                            '{"id":"second","name":"Portal"}]}}')
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            get_item_by_name("http://bitwarden-cli:8087", "Portal")

    @patch("BillCollector.get_json")
    def test_item_lookup_requires_stable_id(self, get):
        get.return_value = '{"data":{"data":[{"name":"Portal"}]}}'
        with self.assertRaisesRegex(RuntimeError, "stable ID"):
            get_item_by_name("http://bitwarden-cli:8087", "Portal")

    @patch.dict("BillCollector.os.environ", {"BW_API_HOST": ""})
    @patch("BillCollector.private_api_request")
    def test_missing_totp_is_silent(self, request):
        response = MagicMock(status_code=400, text="No TOTP configured")
        request.return_value = response

        self.assertIsNone(
            get_totp("http://bitwarden-cli:8087", "item-id"))

        response.raise_for_status.assert_not_called()
        request.assert_called_once_with("GET", "http://bitwarden-cli:8087/object/totp/item-id")

    @patch("BillCollector.private_api_request")
    def test_totp_is_returned(self, request):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "success": True,
            "data": {"data": "123456"},
        }
        request.return_value = response

        self.assertEqual(
            get_totp("http://bitwarden-cli:8087", "item-id"), "123456")

    @patch("BillCollector.private_api_request")
    def test_unexpected_totp_failure_is_not_hidden(self, request):
        response = MagicMock(status_code=503)
        error = requests.exceptions.HTTPError(response=response)
        response.raise_for_status.side_effect = error
        request.return_value = response

        with self.assertRaisesRegex(
                RuntimeError, r"Bitwarden TOTP request failed \(503\)"):
            get_totp("http://bitwarden-cli:8087", "item-id")

    def test_public_api_fails_before_any_vault_request_even_with_cloud_vault(self):
        with patch("BillCollector.socket.getaddrinfo", return_value=address_info("8.8.8.8")), \
             patch("BillCollector.requests.Session") as session:
            with self.assertRaises(SystemExit) as error:
                WebRetriDoc(defs("vault.bitwarden.com", "http://public.example:8087"), "playwright")
        self.assertEqual(error.exception.code, 1)
        session.assert_not_called()

    @patch("BillCollector.private_api_request")
    def test_local_api_does_not_follow_redirect(self, request):
        response = MagicMock(status_code=302)
        request.return_value = response
        self.assertIsNone(get_json("http://localhost:8087/status"))

    @patch("BillCollector.private_api_request")
    def test_unexpected_400_totp_fails(self, request):
        request.return_value = MagicMock(status_code=400, text="Invalid item ID")
        with self.assertRaisesRegex(RuntimeError, r"failed \(400\)"):
            get_totp("http://localhost:8087", "item-id")

    def test_proxy_environment_is_disabled_and_dns_change_cannot_redirect(self):
        with patch.dict("BillCollector.os.environ", {
                "HTTP_PROXY": "http://public-proxy.invalid:3128", "NO_PROXY": ""}), \
             patch("BillCollector.socket.getaddrinfo", side_effect=[
                 address_info("172.20.0.3"), address_info("8.8.8.8")]), \
             patch("BillCollector.requests.Session") as session_class:
            session = session_class.return_value.__enter__.return_value
            session.request.return_value = MagicMock(status_code=200)
            private_api_request("GET", "http://bitwarden-cli:8087/status")
            self.assertIs(session.trust_env, False)
            self.assertEqual(session.request.call_args.args[1], "http://172.20.0.3:8087/status")
            with self.assertRaisesRegex(ValueError, "local HTTP"):
                private_api_request("GET", "http://bitwarden-cli:8087/status")
            self.assertEqual(session.request.call_count, 1)

    def test_vault_errors_do_not_log_response_or_request_exception(self):
        secret = "SENTINEL_PRIVATE_VALUE"
        response = MagicMock(status_code=503)
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            secret, response=response)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("BillCollector")
        logger.addHandler(handler)
        try:
            with patch("BillCollector.private_api_request", return_value=response):
                self.assertIsNone(get_json("http://localhost:8087/status"))
        finally:
            logger.removeHandler(handler)
        self.assertNotIn(secret, stream.getvalue())


if __name__ == "__main__":
    unittest.main()
