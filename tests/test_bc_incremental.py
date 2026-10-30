# -*- coding: utf-8 -*-
"""Incremental source keys and ZIP PDF unpacking for download_all (no browser)."""

import io
import sqlite3
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

from billcollector import collection  # noqa: E402
from billcollector import db as bcd  # noqa: E402
from billcollector import downloads as bcdl, runner  # noqa: E402
from tests.test_bc_collection import _Page, _Events, _Listing  # noqa: E402

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


class IncrementalRunnerTests(_Base):
    def collect(self, keys, *, attribute="data-key", incremental=True, task=None):
        context = _Events()
        page = _Page()
        items = [{"data-key": key, attribute: key,
                  "download": _StubDownload(PDF + key.encode(), key + ".pdf")} for key in keys]
        listing = _Listing(page, context, items)
        ctx = SimpleNamespace(page=page, context=context, run_id=self.rid,
                              task={"id": task or self.tid}, db=self.db, files_downloaded=[])
        runner._collection_actions(ctx)["download_all"](listing, {
            "key_attribute": attribute, "max_items": 10, "timeout_ms": 1000,
            "event_window_ms": 2, "incremental": incremental})
        self.last_context = ctx
        return listing.clicked

    def test_new_session_skips_known_and_only_clicks_new_document(self):
        self.assertEqual(self.collect(["a", "b"]), ["a", "b"])
        self.assertEqual(self.collect(["a", "b"]), [])
        self.assertEqual(runner.run_result(False, [], known_source_count=self.last_context.incremental_known_count), "success")
        self.assertEqual(runner.run_result(False, []), "failure")
        self.assertEqual(runner.run_result(True, [], known_source_count=2), "failure")
        self.assertEqual(self.collect(["a", "b", "c"]), ["c"])
        self.assertEqual(len(self.db.list_downloads()), 3)

    def test_other_task_attribute_and_nonincremental_are_not_skipped(self):
        self.collect(["a"])
        self.assertEqual(self.collect(["a"], attribute="data-other"), ["a"])
        other = self.db.create_task("svc", "other")
        self.assertEqual(self.collect(["a"], task=other), ["a"])
        self.assertEqual(self.collect(["a"], incremental=False), ["a"])


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

    def test_original_utf8_bom_is_retained(self):
        original = b"\xef\xbb\xbf" + PDF
        record = self.track(_zip({"original.pdf": original}))
        self.assertEqual(Path(record["saved_path"]).read_bytes(), original)

    def test_symlink_and_unsafe_directory_entries_fail_closed(self):
        for name, mode in [("link.pdf", stat.S_IFLNK | 0o777),
                           ("directory.pdf", stat.S_IFDIR | 0o755),
                           ("../", stat.S_IFDIR | 0o755),
                           ("C:invoice.pdf", stat.S_IFREG | 0o600)]:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                archive.writestr(info, PDF)
                if name.endswith("/"):
                    archive.writestr("ok.pdf", PDF)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.track(buffer.getvalue())
        self.assertEqual(self.db.list_downloads(), [])

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
