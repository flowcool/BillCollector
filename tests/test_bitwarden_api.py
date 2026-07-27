import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


APPS = Path(__file__).resolve().parents[1] / "apps"
sys.path.insert(0, str(APPS))

from BillCollector import get_json, is_api_url_local, post_json  # noqa: E402


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
    @patch("BillCollector.requests.get")
    def test_get_uses_configured_host_header(self, request):
        response = MagicMock(text='{"success": true}')
        request.return_value = response

        self.assertEqual(get_json("http://bitwarden-cli:8087/status"),
                         '{"success": true}')
        request.assert_called_once_with(
            "http://bitwarden-cli:8087/status",
            headers={"Host": "127.0.0.1:8087"})
        response.raise_for_status.assert_called_once_with()

    @patch.dict("BillCollector.os.environ", {"BW_API_HOST": "127.0.0.1:8087"})
    @patch("BillCollector.requests.post")
    def test_post_uses_configured_host_header(self, request):
        response = MagicMock()
        response.json.return_value = {"success": True}
        request.return_value = response

        self.assertEqual(
            post_json("http://bitwarden-cli:8087/sync", None),
            '{"success": true}')
        request.assert_called_once_with(
            "http://bitwarden-cli:8087/sync",
            json=None,
            headers={"Host": "127.0.0.1:8087"})
        response.raise_for_status.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
