import json
import sys
import tempfile
import unittest
from pathlib import Path


APPS = Path(__file__).resolve().parents[1] / "apps"
sys.path.insert(0, str(APPS))

from BillCollectorState import DownloadState, sha256_text  # noqa: E402


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

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            (state_dir / "downloads-v1.json").write_text(
                "not-json", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "state is invalid"):
                DownloadState(state_dir, "account")

