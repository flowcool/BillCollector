# -*- coding: utf-8 -*-
"""Incremental source keys and ZIP PDF unpacking for download_all (no browser)."""

import io
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

from billcollector import collection  # noqa: E402
from billcollector import db as bcd  # noqa: E402
from billcollector import downloads as bcdl  # noqa: E402

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class _StubDownload:
    def __init__(self, data, name, url="https://portal.example/invoice?token=x"):
        self.suggested_filename = name
        self.url = url
        self._data = data

    def save_as(self, target):
        Path(target).write_bytes(self._data)


def _zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = bcd.Database(path=self.root / "test.db")
        self.out = self.root / "dl"
        self.db.set_setting("download_dir", str(self.out))
        self.tid = self.db.create_task("svc", "u1")
        self.rid = self.db.create_run(self.tid)
        self.task = {"id": self.tid}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def published(self):
        return sorted(p.name for p in self.out.iterdir() if p.is_file())


class SourceKeyTests(_Base):
    def test_source_key_is_persisted_for_new_and_duplicate_rows(self):
        bcdl.track_download(_StubDownload(PDF, "a.pdf"), self.rid, self.task, self.db,
                            source_key="href=1")
        bcdl.track_download(_StubDownload(PDF, "a.pdf"), self.rid, self.task, self.db,
                            source_key="href=2")
        self.assertEqual(self.db.known_source_keys(self.tid), {"href=1", "href=2"})
        self.assertEqual(self.published(), ["a.pdf"])

    def test_known_keys_are_scoped_to_the_task(self):
        other = self.db.create_task("svc", "u2")
        bcdl.track_download(_StubDownload(PDF, "a.pdf"), self.rid, {"id": other}, self.db,
                            source_key="href=1")
        self.assertEqual(self.db.known_source_keys(self.tid), set())

    def test_existing_database_gains_the_column_and_keeps_rows(self):
        path = self.root / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE Download (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,
              task_id INTEGER, url TEXT, filename TEXT, size INTEGER, sha256 TEXT,
              saved_path TEXT, created_at TEXT, duplicate_of_id INTEGER,
              state TEXT NOT NULL DEFAULT 'consumed', staged_path TEXT);
            INSERT INTO Download (task_id, filename, sha256) VALUES (7, 'old.pdf', 'abc');
        """)
        conn.commit()
        conn.close()
        migrated = bcd.Database(path=path)
        try:
            rows = migrated.list_downloads()
            self.assertEqual([(r["filename"], r["sha256"], r["source_key"]) for r in rows],
                             [("old.pdf", "abc", None)])
            self.assertEqual(migrated.known_source_keys(7), set())
        finally:
            migrated.close()


class UnpackTests(_Base):
    def track(self, data, name="factures.zip"):
        return bcdl.track_download(_StubDownload(data, name), self.rid, self.task, self.db,
                                   unpack="pdf")

    def test_single_pdf_member_is_published_byte_identical(self):
        record = self.track(_zip({"facture_2026_10.pdf": PDF}))
        self.assertEqual(record["filename"], "facture_2026_10.pdf")
        self.assertEqual(self.published(), ["facture_2026_10.pdf"])
        self.assertEqual((self.out / "facture_2026_10.pdf").read_bytes(), PDF)
        self.assertEqual(record["sha256"], bcdl.sha256_of(self.out / "facture_2026_10.pdf"))

    def test_same_pdf_in_a_different_zip_is_a_duplicate(self):
        self.track(_zip({"f.pdf": PDF}))
        second = self.track(_zip({"f.pdf": PDF, "dir/": b""}))
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.published(), ["f.pdf"])

    def test_invalid_archives_fail_closed_without_publication(self):
        cases = {
            "not a zip": PDF,
            "no member": _zip({}),
            "two members": _zip({"a.pdf": PDF, "b.pdf": PDF}),
            "not pdf name": _zip({"a.txt": PDF}),
            "path": _zip({"sub/a.pdf": PDF}),
            "not pdf bytes": _zip({"a.pdf": b"hello"}),
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.track(data)
        self.assertFalse(self.out.exists() and self.published())
        self.assertEqual(self.db.list_downloads(), [])
        journals = [p for p in (self.out / ".billcollector").rglob("*") if p.is_file()
                    and p.name != "publication.lock"]
        self.assertEqual(journals, [])

    def test_oversized_member_fails_closed(self):
        with mock.patch.object(bcdl, "MAX_UNPACKED_BYTES", 8), self.assertRaises(ValueError):
            self.track(_zip({"a.pdf": PDF}))
        self.assertEqual(self.db.list_downloads(), [])


class DownloadAllOptionTests(unittest.TestCase):
    def call(self, options, keyed_save):
        base = {"key_attribute": "href", "max_items": 5, "timeout_ms": 60000,
                "event_window_ms": 1000}
        locator = mock.Mock()
        locator.evaluate_all.return_value = ["k1"]
        return collection.download_all(mock.Mock(), mock.Mock(), locator, mock.Mock(),
                                       {**base, **options}, completed_keys={"k1"},
                                       keyed_save=keyed_save)

    def test_options_need_a_keyed_runner(self):
        for options in ({"incremental": True}, {"unpack": "pdf"}):
            with self.subTest(options), self.assertRaises(collection.CollectionError):
                self.call(options, keyed_save=False)

    def test_option_values_are_validated(self):
        for options in ({"incremental": "yes"}, {"unpack": "zip"}):
            with self.subTest(options), self.assertRaises(collection.CollectionError):
                self.call(options, keyed_save=True)

    def test_all_known_keys_download_nothing(self):
        self.assertEqual(self.call({"incremental": True}, keyed_save=True), [])

    def test_source_key_format(self):
        self.assertEqual(collection.source_key("href", "a=b"), "href=a=b")


if __name__ == "__main__":
    unittest.main()
