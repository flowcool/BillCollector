"""Exercise the existing vault HTTP boundary without a real vault or account."""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))
import BillCollector


class VaultLoopbackTests(unittest.TestCase):
    def test_synthetic_vault_item_reaches_selected_service(self):
        calls = []

        class Vault(BaseHTTPRequestHandler):
            def reply(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                calls.append(self.path)
                if self.path == "/status":
                    self.reply({"success": True, "data": {"template_status": "unlocked"}})
                elif self.path == "/list/object/items?search=lab%20alice":
                    self.reply({"data": {"data": [{"id": "item-123", "name": "lab alice",
                                "login": {"username": "alice",
                                "password": "synthetic-password",
                                "uris": [{"uri": "https://portal.invalid"}]}}]}})
                elif self.path == "/object/totp/item-123":
                    self.reply({"data": {"data": "123456"}})
                else:
                    self.send_error(404)

            def do_POST(self):
                calls.append(self.path)
                self.reply({"success": True})

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Vault)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            ini = Path(directory) / "services.ini"
            ini.write_text("[Playwright]\nlab = alice\n", encoding="utf-8")
            config = BillCollector.defs("vault.bitwarden.com", f"http://127.0.0.1:{server.server_port}", fname=str(ini))
            with patch.dict(os.environ, {"HTTP_PROXY": "http://public-proxy.invalid:3128",
                                      "HTTPS_PROXY": "http://public-proxy.invalid:3128",
                                      "NO_PROXY": ""}), \
                 patch.object(BillCollector, "retrieve_from_service_with_playwright", return_value=True) as runner:
                self.assertTrue(BillCollector.WebRetriDoc(config, "playwright"))
            self.assertEqual(runner.call_args.args[:5],
                             ("lab", "https://portal.invalid", "alice", "synthetic-password", "123456"))
            self.assertEqual(runner.call_args.kwargs["account_id"], "item-123")
            self.assertEqual(calls, ["/status", "/sync", "/list/object/items?search=lab%20alice",
                                     "/object/totp/item-123"])


if __name__ == "__main__":
    unittest.main()
