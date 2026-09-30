"""Offline publication contract tests; no browser, portal, or DMS required."""

import errno
import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
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

    def test_stage_directory_sync_precedes_prepared_record(self):
        with self.publisher() as publisher:
            with patch.object(publisher, "_sync_directory", side_effect=OSError("stage fsync failed")):
                with self.assertRaisesRegex(OSError, "stage fsync failed"):
                    publisher.publish(FakeDownload(), service="portal", account="alice")
            self.assertEqual(publisher._db.execute("SELECT count(*) FROM documents").fetchone()[0], 0)
            self.assertEqual(list(self.staging.iterdir()), [])

    def test_crash_before_rename_intent_keeps_recoverable_stage(self):
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

    def test_failed_rename_after_intent_blocks_ambiguous_retry(self):
        with self.publisher() as publisher:
            with patch("download_publication.os.replace", side_effect=OSError("rename failed")):
                with self.assertRaisesRegex(OSError, "rename failed"):
                    publisher.publish(FakeDownload(), service="portal", account="alice")
            row = publisher._db.execute("SELECT status, stage_name FROM documents").fetchone()
            self.assertEqual(row[0], "renaming")
            self.assertTrue((self.staging / row[1]).exists())
        with self.assertRaisesRegex(PublicationError, "ambiguous"):
            with self.publisher():
                pass

    def test_cross_mount_rename_failure_never_marks_published(self):
        with self.publisher() as publisher:
            with patch("download_publication.os.replace", side_effect=OSError(errno.EXDEV, "cross-device link")):
                with self.assertRaises(OSError) as error:
                    publisher.publish(FakeDownload(), service="portal", account="alice")
            self.assertEqual(error.exception.errno, errno.EXDEV)
            status, stage_name = publisher._db.execute(
                "SELECT status, stage_name FROM documents"
            ).fetchone()
            self.assertEqual(status, "renaming")
            self.assertTrue((self.staging / stage_name).exists())
            self.assertEqual(list(self.output.iterdir()), [])
        with self.assertRaisesRegex(PublicationError, "ambiguous"):
            with self.publisher():
                pass
        with closing(sqlite3.connect(self.state / "publication.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status FROM documents").fetchone()[0], "renaming")

    def test_rename_syncs_both_directories_before_marking_published(self):
        with self.publisher() as publisher:
            calls = []
            sync_directory = publisher._sync_directory
            set_status = publisher._set_status

            def record_sync(path):
                calls.append(("sync", path))
                sync_directory(path)

            def record_status(account_hash, digest, status):
                calls.append(("status", status))
                set_status(account_hash, digest, status)

            with patch.object(publisher, "_sync_directory", side_effect=record_sync), patch.object(
                publisher, "_set_status", side_effect=record_status
            ):
                self.assertTrue(publisher.publish(FakeDownload(), service="portal", account="alice"))
            self.assertEqual(
                calls,
                [
                    ("sync", self.staging),
                    ("status", "renaming"),
                    ("sync", self.output),
                    ("sync", self.staging),
                    ("status", "published"),
                ],
            )

    def test_failed_stage_directory_sync_after_rename_recovers_from_final(self):
        with self.publisher() as publisher:
            sync_directory = publisher._sync_directory
            calls = []

            def fail_after_rename(path):
                calls.append(path)
                if calls == [self.staging, self.output, self.staging]:
                    raise OSError("stage directory fsync failed")
                sync_directory(path)

            with patch.object(publisher, "_sync_directory", side_effect=fail_after_rename):
                with self.assertRaisesRegex(OSError, "stage directory fsync failed"):
                    publisher.publish(FakeDownload(), service="portal", account="alice")
            self.assertEqual(
                publisher._db.execute("SELECT status FROM documents").fetchone()[0], "renaming"
            )
            self.assertEqual(len(list(self.output.glob("*.pdf"))), 1)
        with self.publisher() as publisher:
            self.assertEqual(
                publisher._db.execute("SELECT status FROM documents").fetchone()[0], "published"
            )
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

    def test_missing_stage_and_consumed_final_blocks_instead_of_false_published(self):
        with self.publisher() as publisher:
            download = FakeDownload()
            with patch.object(publisher, "_finish", side_effect=RuntimeError("crash")):
                with self.assertRaisesRegex(RuntimeError, "crash"):
                    publisher.publish(download, service="portal", account="alice")
            row = publisher._db.execute(
                "SELECT stage_name, final_name FROM documents WHERE status='prepared'"
            ).fetchone()
            os.replace(publisher.stage_dir / row[0], self.output / row[1])
            (self.output / row[1]).unlink()  # DMS consumed between rename and DB update.
        with self.assertRaisesRegex(PublicationError, "ambiguous"):
            with self.publisher():
                pass
        with closing(sqlite3.connect(self.state / "publication.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status FROM documents").fetchone()[0], "prepared")

    def test_renaming_with_no_stage_or_final_remains_ambiguous(self):
        with self.publisher() as publisher:
            with patch.object(publisher, "_finish", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    publisher.publish(FakeDownload(), service="portal", account="alice")
            row = publisher._db.execute(
                "SELECT account_hash, sha256, stage_name FROM documents"
            ).fetchone()
            publisher._set_status(row[0], row[1], "renaming")
            (publisher.stage_dir / row[2]).unlink()
        with self.assertRaisesRegex(PublicationError, "ambiguous"):
            with self.publisher():
                pass
        with closing(sqlite3.connect(self.state / "publication.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status FROM documents").fetchone()[0], "renaming")

    def test_old_state_schema_is_rejected_before_publish(self):
        self.state.mkdir()
        with closing(sqlite3.connect(self.state / "publication.sqlite3")) as db:
            db.execute("""
                CREATE TABLE documents (
                    account_hash TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    stage_name TEXT NOT NULL,
                    final_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('prepared', 'published')),
                    PRIMARY KEY (account_hash, sha256)
                )
            """)
            db.commit()
        with self.assertRaisesRegex(PublicationError, "schema is incompatible"):
            with self.publisher():
                pass

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
