import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APPS = Path(__file__).resolve().parents[1] / "apps"
sys.path.insert(0, str(APPS))

from BillCollectorState import (  # noqa: E402
    DownloadState,
    DownloadStateLock,
    sha256_text,
)


class DownloadStateTests(unittest.TestCase):
    def test_publishes_new_file_and_persists_only_hashes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "downloads"
            output = root / "output"
            downloads.mkdir()
            invoice = downloads / "invoice.pdf"
            invoice.write_bytes(b"invoice")
            state = DownloadState(root / "state", "private-item-id")

            published = state.publish(
                "https://provider.test/private/invoice/1",
                invoice,
                output)

            self.assertEqual(published, "invoice.pdf")
            self.assertTrue((output / "invoice.pdf").is_file())
            payload = (root / "state" / "downloads-v1.json").read_text()
            self.assertNotIn("private-item-id", payload)
            self.assertNotIn("provider.test", payload)
            self.assertIn(sha256_text("private-item-id"), payload)

    def test_known_url_is_skipped_after_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "downloads"
            downloads.mkdir()
            invoice = downloads / "invoice.pdf"
            invoice.write_bytes(b"invoice")
            url = "https://provider.test/invoice/1"
            DownloadState(root / "state", "account").publish(
                url, invoice, root / "output")

            reloaded = DownloadState(root / "state", "account")

            self.assertTrue(reloaded.has_url(url))
            self.assertFalse(
                DownloadState(root / "state", "other-account").has_url(url))

    def test_reused_page_url_is_stored_only_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "downloads"
            downloads.mkdir()
            state = DownloadState(root / "state", "account")
            url = "https://provider.test/invoices"
            for name in ("invoice-1.pdf", "invoice-2.pdf"):
                invoice = downloads / name
                invoice.write_bytes(name.encode())
                state.publish(url, invoice, root / "output")

            account = next(iter(state.data["accounts"].values()))
            self.assertEqual(len(account["urls"]), 1)

    def test_duplicate_content_with_new_url_is_not_published(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "downloads"
            downloads.mkdir()
            first = downloads / "first.pdf"
            first.write_bytes(b"same invoice")
            state = DownloadState(root / "state", "account")
            state.publish(
                "https://provider.test/invoice/old",
                first,
                root / "output")
            second = downloads / "second.pdf"
            second.write_bytes(b"same invoice")

            published = state.publish(
                "https://provider.test/invoice/new",
                second,
                root / "output")

            self.assertIsNone(published)
            self.assertFalse(second.exists())
            self.assertTrue(
                state.has_url("https://provider.test/invoice/new"))

    def test_same_document_name_with_regenerated_content_is_not_published(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "downloads"
            downloads.mkdir()
            first = downloads / "invoice-2026-07.pdf"
            first.write_bytes(b"first generated representation")
            state = DownloadState(root / "state", "account")
            state.publish(
                "https://provider.test/session-one",
                first,
                root / "output")
            (root / "output" / first.name).unlink()
            regenerated = downloads / first.name
            regenerated.write_bytes(b"second generated representation")

            published = state.publish(
                "https://provider.test/session-two",
                regenerated,
                root / "output")

            self.assertIsNone(published)
            self.assertFalse(regenerated.exists())
            account = next(iter(state.data["accounts"].values()))
            self.assertEqual(len(account["urls"]), 1)
            self.assertEqual(len(account["documents"]), 1)

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            (state_dir / "downloads-v1.json").write_text(
                "not-json", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "state is invalid"):
                DownloadState(state_dir, "account")

    def test_concurrent_state_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with DownloadStateLock(temp_dir):
                with self.assertRaisesRegex(
                        RuntimeError, "Another BillCollector job"):
                    DownloadStateLock(temp_dir)

    def test_previous_valid_state_is_kept_as_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "downloads"
            downloads.mkdir()
            state = DownloadState(root / "state", "account")
            first = downloads / "first.pdf"
            first.write_bytes(b"first")
            state.publish("https://example.test/1", first, root / "output")
            first_state = json.loads(
                (root / "state" / "downloads-v1.json").read_text())
            second = downloads / "second.pdf"
            second.write_bytes(b"second")

            state.publish("https://example.test/2", second, root / "output")

            backup_state = json.loads(
                (root / "state" / "downloads-v1.json.bak").read_text())
            self.assertEqual(backup_state, first_state)

    def test_state_file_and_directory_are_fsynced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            downloads = root / "downloads"
            downloads.mkdir()
            invoice = downloads / "invoice.pdf"
            invoice.write_bytes(b"invoice")
            state = DownloadState(root / "state", "account")

            with patch("BillCollectorState.os.fsync") as fsync:
                state.publish(
                    "https://example.test/1", invoice, root / "output")

            self.assertGreaterEqual(fsync.call_count, 2)
