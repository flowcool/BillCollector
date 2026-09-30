"""Offline publication contract tests; no browser, portal, or DMS required."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))
from download_publication import DownloadPublisher, PublicationError, RunLocked


PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class FakeDownload:
    def __init__(self, body=PDF, error=None):
        self.body = body
        self.error = error
        self.saved = False

    def failure(self):
        return self.error

    def save_as(self, path):
        self.saved = True
        Path(path).write_bytes(self.body)


class DownloadPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.output = self.root / "output"
        self.staging = self.root / "staging"

    def publisher(self):
        return DownloadPublisher(self.state, self.output, self.staging)

    def test_publish_and_deduplicate_across_runs(self):
        with self.publisher() as publisher:
            self.assertTrue(publisher.publish(FakeDownload(), service="portal", account="alice"))
            self.assertFalse(publisher.publish(FakeDownload(), service="portal", account="alice"))
        published = list(self.output.glob("*.pdf"))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].read_bytes(), PDF)
        published[0].unlink()  # Simulate the DMS consuming the file.
        with self.publisher() as publisher:
            self.assertFalse(publisher.publish(FakeDownload(), service="portal", account="alice"))
        self.assertEqual(list(self.output.glob("*.pdf")), [])

    def test_same_document_is_independent_per_account(self):
        with self.publisher() as publisher:
            self.assertTrue(publisher.publish(FakeDownload(), service="portal", account="alice"))
            self.assertTrue(publisher.publish(FakeDownload(), service="portal", account="bob"))
        self.assertEqual(len(list(self.output.glob("*.pdf"))), 2)

    def test_failed_and_invalid_downloads_never_appear_in_output(self):
        with self.publisher() as publisher:
            failed = FakeDownload(error="canceled")
            with self.assertRaises(PublicationError):
                publisher.publish(failed, service="portal", account="alice")
            self.assertFalse(failed.saved)
            for body in (b"", b"<html>login expired</html>"):
                with self.assertRaises(PublicationError):
                    publisher.publish(FakeDownload(body), service="portal", account="alice")
        self.assertEqual(list(self.output.glob("*.pdf")), [])
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_lock_covers_whole_run(self):
        with self.publisher():
            with self.assertRaises(RunLocked):
                with self.publisher():
                    pass

    def test_staging_cannot_be_inside_consumer_output(self):
        with self.assertRaisesRegex(PublicationError, "disjoint"):
            with DownloadPublisher(self.state, self.output, self.output / ".stage"):
                pass

    def test_failed_rename_keeps_recoverable_stage(self):
        with self.publisher() as publisher:
            with patch.object(publisher, "_finish", side_effect=OSError("rename failed")):
                with self.assertRaisesRegex(OSError, "rename failed"):
                    publisher.publish(FakeDownload(), service="portal", account="alice")
            row = publisher._db.execute("SELECT status, stage_name FROM documents").fetchone()
            self.assertEqual(row[0], "prepared")
            self.assertTrue((self.staging / row[1]).exists())
            with self.assertRaisesRegex(PublicationError, "awaits recovery"):
                publisher.publish(FakeDownload(), service="portal", account="alice")
        with self.publisher() as publisher:
            self.assertEqual(len(list(self.output.glob("*.pdf"))), 1)
            self.assertFalse(publisher.publish(FakeDownload(), service="portal", account="alice"))

    def test_recovers_prepared_stage_after_crash_before_rename(self):
        with self.publisher() as publisher:
            stage_name = "a" * 32 + ".part"
            stage = publisher.stage_dir / stage_name
            stage.write_bytes(PDF)
            account_hash = hashlib.sha256("portal\0alice".encode()).hexdigest()
            digest = hashlib.sha256(PDF).hexdigest()
            final_name = f"{account_hash[:16]}-{digest}.pdf"
            publisher._db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, 'prepared')",
                (account_hash, digest, stage_name, final_name),
            )
            publisher._db.commit()
        with self.publisher() as publisher:
            self.assertEqual((self.output / final_name).read_bytes(), PDF)
            self.assertFalse(stage.exists())
            self.assertFalse(publisher.publish(FakeDownload(), service="portal", account="alice"))

    def test_recovers_rename_after_consumer_took_final(self):
        with self.publisher() as publisher:
            download = FakeDownload()
            with patch.object(publisher, "_finish", side_effect=RuntimeError("crash")):
                with self.assertRaisesRegex(RuntimeError, "crash"):
                    publisher.publish(download, service="portal", account="alice")
            row = publisher._db.execute(
                "SELECT stage_name, final_name FROM documents WHERE status='prepared'"
            ).fetchone()
            (publisher.stage_dir / row[0]).rename(self.output / row[1])
            (self.output / row[1]).unlink()  # DMS consumed between rename and DB update.
        with self.publisher() as publisher:
            self.assertFalse(publisher.publish(FakeDownload(), service="portal", account="alice"))
            self.assertEqual(
                publisher._db.execute("SELECT status FROM documents").fetchone()[0],
                "published",
            )

    def test_corrupt_prepared_artifact_blocks_recovery_without_reset(self):
        with self.publisher() as publisher:
            with patch.object(publisher, "_finish", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    publisher.publish(FakeDownload(), service="portal", account="alice")
            stage_name = publisher._db.execute("SELECT stage_name FROM documents").fetchone()[0]
            (publisher.stage_dir / stage_name).write_bytes(b"tampered")
        with self.assertRaisesRegex(PublicationError, "corrupt"):
            with self.publisher():
                pass
        self.assertTrue((self.staging / stage_name).exists())

    def test_corrupt_database_blocks_without_reinitializing(self):
        self.state.mkdir()
        db = self.state / "publication.sqlite3"
        db.write_bytes(b"not a database")
        with self.assertRaisesRegex(PublicationError, "corrupt"):
            with self.publisher():
                pass
        self.assertEqual(db.read_bytes(), b"not a database")


if __name__ == "__main__":
    unittest.main()
