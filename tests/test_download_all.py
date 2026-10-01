"""Offline DownloadAll contract tests; browser work is opt-in and loopback only."""

import copy
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import closing
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

from helpers.BillCollectorRecipeContract import RecipeContractError, validate_recipe_contract
import BillCollectorServices_pw as runner
from BillCollectorServices_pw import download_all_locator, process_step


class Events:
    def __init__(self):
        self.listeners = {}

    def on(self, name, callback):
        self.listeners.setdefault(name, []).append(callback)

    def remove_listener(self, name, callback):
        self.listeners[name].remove(callback)

    def emit(self, name, value):
        for callback in self.listeners.get(name, []):
            callback(value)


class Page(Events):
    def __init__(self):
        super().__init__()
        self.url = "https://example.test/list"
        self.back_calls = 0
        self.closed = False
        self.pending = []

    def wait_for_timeout(self, milliseconds):
        time.sleep(milliseconds / 1000)
        now = time.monotonic()
        ready = [event for event in self.pending if event[0] <= now]
        self.pending = [event for event in self.pending if event[0] > now]
        for _deadline, download in ready:
            self.emit("download", download)

    def go_back(self, **_kwargs):
        self.back_calls += 1
        self.url = "https://example.test/list"
        # Model an SPA: the list renders a few polls after DOMContentLoaded.
        self.listing.blank_snapshots = self.listing.render_delay_snapshots

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def locator(self, selector):
        assert selector == ".invoice"
        return self.listing


class Item:
    def __init__(self, listing, entry):
        self.listing = listing
        self.entry = entry
        self.disposed = False
        listing.handles.append(self)

    def evaluate(self, _script):
        return [self.entry.get("href"), self.entry["text"]]

    def dispose(self):
        self.disposed = True

    def click(self, **_kwargs):
        entry = self.entry
        index = self.listing.items.index(entry)
        self.listing.clicked.append(index)
        if entry.get("navigate"):
            self.listing.page.url = "https://example.test/view"
        if entry.get("popup"):
            popup = Page()
            self.listing.context.emit("page", popup)
            popup.emit("download", entry["download"])
        elif entry.get("download"):
            self.listing.page.emit("download", entry["download"])
        if entry.get("extra_after_ms"):
            self.listing.page.pending.append((
                time.monotonic() + entry["extra_after_ms"] / 1000, "late-extra"
            ))
        if entry.get("shrink"):
            self.listing.items.pop()
        if entry.get("change_next"):
            self.listing.items[index + 1]["href"] = "/changed.pdf"
        if entry.get("reorder"):
            self.listing.items.reverse()


class Listing:
    def __init__(self, page, context, items):
        self.page = page
        self.context = context
        self.items = items
        self.clicked = []
        self.first = self
        self.snapshot_count = 0
        self.reorder_after_snapshot = None
        self.render_delay_snapshots = 0
        self.blank_snapshots = 0
        self.handles = []

    def wait_for(self, **_kwargs):
        pass

    def count(self):
        return len(self.items)

    def evaluate_all(self, _script):
        self.snapshot_count += 1
        if self.blank_snapshots:
            self.blank_snapshots -= 1
            return []
        identities = [[entry.get("href"), entry["text"]] for entry in self.items]
        if self.snapshot_count == self.reorder_after_snapshot:
            self.items.reverse()
        return identities

    def nth(self, index):
        return SimpleNamespace(element_handle=lambda **_kwargs: Item(self, self.items[index]))


class Publisher:
    def __init__(self):
        self.downloads = []

    def publish(self, download, *, service, account):
        assert (service, account) == ("sample", "sample alice")
        self.downloads.append(download)
        return download not in self.downloads[:-1]


class DownloadAllTests(unittest.TestCase):
    def setup_listing(self, items):
        page = Page()
        context = Events()
        listing = Listing(page, context, items)
        page.listing = listing
        bcs = SimpleNamespace(page=page, drv=context, service="sample", account_id="sample alice")
        return bcs, listing, Publisher()

    def test_links_buttons_popup_navigation_and_duplicates(self):
        bcs, listing, publisher = self.setup_listing([
            {"href": "/one.pdf", "text": "One", "download": "one", "navigate": True},
            {"text": "Download", "download": "two", "popup": True},
            {"text": "Download again", "download": "one"},
        ])
        self.assertEqual(download_all_locator(bcs, listing, publisher, 100), [
            {"result": "published"}, {"result": "published"}, {"result": "duplicate"},
        ])
        self.assertEqual(listing.clicked, [0, 1, 2])
        self.assertEqual(publisher.downloads, ["one", "two", "one"])
        self.assertEqual(bcs.page.back_calls, 1)
        self.assertTrue(listing.handles and all(handle.disposed for handle in listing.handles))

    def test_list_rendered_late_after_back_navigation_completes(self):
        bcs, listing, publisher = self.setup_listing([
            {"href": "/one.pdf", "text": "One", "download": "one", "navigate": True},
            {"href": "/two.pdf", "text": "Two", "download": "two", "navigate": True},
            {"href": "/three.pdf", "text": "Three", "download": "three"},
        ])
        listing.render_delay_snapshots = 3
        results = []
        self.assertIs(download_all_locator(bcs, listing, publisher, 1000, results), results)
        self.assertEqual(publisher.downloads, ["one", "two", "three"])
        self.assertEqual(results, [{"result": "published"}] * 3)

    def test_partial_run_keeps_published_outcomes_in_caller_list(self):
        bcs, listing, publisher = self.setup_listing([
            {"text": "First", "download": "one"}, {"text": "Second"},
        ])
        results = []
        with self.assertRaisesRegex(RuntimeError, "item 2 of 2 produced 0 files"):
            download_all_locator(bcs, listing, publisher, 100, results)
        self.assertEqual(results, [{"result": "published"}])
        self.assertTrue(all(handle.disposed for handle in listing.handles))

    def test_empty_changed_and_partial_lists_fail(self):
        bcs, listing, publisher = self.setup_listing([])
        with self.assertRaisesRegex(RuntimeError, "empty"):
            download_all_locator(bcs, listing, publisher, 100)
        bcs, listing, publisher = self.setup_listing([
            {"text": "First", "download": "one", "shrink": True},
            {"text": "Second", "download": "two"},
        ])
        with self.assertRaisesRegex(RuntimeError, "changed after 1 of 2"):
            download_all_locator(bcs, listing, publisher, 100)
        self.assertEqual(publisher.downloads, ["one"])
        bcs, listing, publisher = self.setup_listing([
            {"text": "Invoice", "download": "one", "reorder": True},
            {"text": "Invoice", "download": "two"},
        ])
        with self.assertRaisesRegex(RuntimeError, "ambiguous duplicate"):
            download_all_locator(bcs, listing, publisher, 100)
        self.assertEqual(listing.clicked, [])
        bcs, listing, publisher = self.setup_listing([
            {"text": "First", "download": "one", "reorder": True},
            {"text": "Second", "download": "two"},
        ])
        with self.assertRaisesRegex(RuntimeError, "changed after 1 of 2"):
            download_all_locator(bcs, listing, publisher, 100)
        self.assertEqual(publisher.downloads, ["one"])
        # The snapshot says A,B; the DOM swaps before click. The handle still
        # clicks A, and the next snapshot fails on the changed order.
        bcs, listing, publisher = self.setup_listing([
            {"text": "First", "download": "one"},
            {"text": "Second", "download": "two"},
        ])
        listing.reorder_after_snapshot = 2
        with self.assertRaisesRegex(RuntimeError, "changed after 1 of 2"):
            download_all_locator(bcs, listing, publisher, 100)
        self.assertEqual(publisher.downloads, ["one"])
        bcs, listing, publisher = self.setup_listing([
            {"text": "First", "download": "one", "change_next": True},
            {"href": "/second.pdf", "text": "Second", "download": "two"},
        ])
        with self.assertRaisesRegex(RuntimeError, "changed after 1 of 2"):
            download_all_locator(bcs, listing, publisher, 100)
        bcs, listing, publisher = self.setup_listing([
            {"text": "First", "download": "one"}, {"text": "Second"},
        ])
        with self.assertRaisesRegex(RuntimeError, "item 2 of 2 produced 0 files"):
            download_all_locator(bcs, listing, publisher, 100)
        self.assertEqual(publisher.downloads, ["one"])
        bcs, listing, publisher = self.setup_listing([
            {"text": "Invoice", "download": "one", "extra_after_ms": 250},
        ])
        with self.assertRaisesRegex(RuntimeError, "produced 2 files"):
            download_all_locator(bcs, listing, publisher, 400)
        self.assertEqual(publisher.downloads, [])

    def test_recipe_requires_locator_terminal_action_and_download_capability(self):
        recipe = {"formatVersion": 1, "capabilities": ["browser", "download"],
                  "services": [{"serviceName": "sample", "steps": [{"step": 1, "methods": [
                      {"method": "locator", "arguments": [{"selector": "a.invoice"}]},
                      {"method": "download_all", "arguments": [{"timeout_ms": 1000}]},
                  ]}]}]}
        self.assertIs(validate_recipe_contract(recipe, "sample", external=True,
            allowed_origins=frozenset({"https://example.test"})), recipe)
        invalid = copy.deepcopy(recipe)
        invalid["capabilities"] = ["browser"]
        with self.assertRaisesRegex(RecipeContractError, "download capability"):
            validate_recipe_contract(invalid, "sample", external=True,
                                     allowed_origins=frozenset({"https://example.test"}))
        invalid = copy.deepcopy(recipe)
        invalid["services"][0]["steps"][0]["methods"].append({"method": "click"})
        with self.assertRaisesRegex(RecipeContractError, "invalid on done"):
            validate_recipe_contract(invalid, "sample", external=True,
                                     allowed_origins=frozenset({"https://example.test"}))
        invalid = copy.deepcopy(recipe)
        invalid["services"][0]["steps"][0]["methods"][-1]["arguments"] = [{"timeout_ms": True}]
        with self.assertRaisesRegex(RecipeContractError, "timeout_ms"):
            validate_recipe_contract(invalid, "sample", external=True,
                                     allowed_origins=frozenset({"https://example.test"}))
        invalid = copy.deepcopy(recipe)
        nested = invalid["services"][0]["steps"][0]
        invalid["services"][0]["steps"] = [{"step": 1,
            "methods": [{"method": "expect_download"}], "steps": [nested]}]
        with self.assertRaisesRegex(RecipeContractError, "cannot be nested"):
            validate_recipe_contract(invalid, "sample", external=True,
                                     allowed_origins=frozenset({"https://example.test"}))

    def test_process_step_dispatches_to_publisher(self):
        bcs, _listing, publisher = self.setup_listing([
            {"text": "Invoice", "download": "one"},
        ])
        bcs.publisher = publisher
        bcs.download_results = []
        bcs.external_recipe = False
        step = {"step": 1, "methods": [
            {"method": "locator", "arguments": [{"selector": ".invoice"}]},
            {"method": "download_all", "arguments": [{"timeout_ms": 100}]},
        ]}
        state = process_step(bcs, step)
        self.assertFalse(state.error_status)
        self.assertEqual(bcs.download_results, [{"result": "published"}])
        self.assertEqual(publisher.downloads, ["one"])


@unittest.skipUnless(os.environ.get("BILLCOLLECTOR_BROWSER_TESTS") == "1",
                     "set BILLCOLLECTOR_BROWSER_TESTS=1 for loopback Chromium smoke")
class DownloadAllBrowserTests(unittest.TestCase):
    def test_browser_links_and_buttons_publish_once(self):
        pdf = b"%PDF-1.4\n%%EOF\n"

        class Portal(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/":
                    body = (b"<button class='invoice' onclick=\"history.pushState({}, '', '/view'); "
                            b"const link=document.createElement('a'); link.href='/one.pdf'; link.click()\">One</button>"
                            b"<a class='invoice' href='/two.pdf' target='_blank'>Two</a>"
                            b"<button class='invoice' onclick=\"location.href='/one.pdf'\">Again</button>")
                    mime = "text/html"
                elif self.path == "/partial":
                    body = (b"<a class='invoice' href='/three.pdf'>Three</a>"
                            b"<button class='invoice'>Nothing</button>")
                    mime = "text/html"
                elif self.path in ("/one.pdf", "/two.pdf", "/three.pdf"):
                    body = {"/one.pdf": pdf, "/two.pdf": pdf + b"second\n",
                            "/three.pdf": pdf + b"third\n"}[self.path]
                    mime = "application/pdf"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime)
                if mime == "application/pdf":
                    self.send_header("Content-Disposition", "attachment; filename=invoice.pdf")
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
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recipe = {"services": [{"serviceName": "sample", "steps": [
                {"step": 1, "methods": [{"method": "goto", "arguments": [
                    {"url": f"http://127.0.0.1:{server.server_port}/"}]}]},
                {"step": 2, "methods": [
                    {"method": "locator", "arguments": [{"selector": ".invoice"}]},
                    {"method": "download_all", "arguments": [{"timeout_ms": 1000}]},
                ]},
            ]}]}
            with patch.dict(os.environ, {
                "PLAYWRIGHT_BROWSERS_PATH": str(Path.home() / ".cache" / "ms-playwright"),
                "BILLCOLLECTOR_PUBLICATION_ROOT": str(root / "publication"),
            }), patch.object(runner, "CHROMIUM_PLAYWRIGHT_PROFILE", str(root / "profiles")), \
                 patch.object(runner, "DB_FILE", str(root / "runs.sqlite3")):
                results = []
                for _ in range(2):
                    bcs = runner.ServiceObj("sample", "alice", "unused", None, False,
                                            str(root / "unused"), account_id="sample alice")
                    bcs.yml = recipe
                    bcs.external_recipe = False
                    results.append(runner.perform_actions(bcs))
                self.assertEqual(results, [
                    [{"result": "published"}, {"result": "published"}, {"result": "duplicate"}],
                    [{"result": "duplicate"}] * 3,
                ])
                self.assertEqual(len(list((root / "publication" / "output").glob("*.pdf"))), 2)
                partial = copy.deepcopy(recipe)
                partial["services"][0]["steps"][0]["methods"][0]["arguments"][0]["url"] = (
                    f"http://127.0.0.1:{server.server_port}/partial"
                )
                bcs = runner.ServiceObj("sample", "alice", "unused", None, False,
                                        str(root / "unused"), account_id="sample alice")
                bcs.yml = partial
                bcs.external_recipe = False
                with self.assertRaisesRegex(RuntimeError, "Step 2 failed"):
                    runner.perform_actions(bcs)
                with closing(sqlite3.connect(root / "runs.sqlite3")) as db:
                    result, info = db.execute(
                        "SELECT result, download_info FROM Service ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                self.assertEqual(result, "failure")
                self.assertEqual(json.loads(info), {"count": 1})
                self.assertEqual(len(list((root / "publication" / "output").glob("*.pdf"))), 3)
