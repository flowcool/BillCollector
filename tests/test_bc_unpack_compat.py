"""Preserve the direct tracker ZIP API independently of collection transport."""
import io
import stat
import zipfile
from pathlib import Path
from unittest import mock

from billcollector import downloads as bcdl
from tests.test_bc_incremental import PDF, _Base, _StubDownload


def _zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


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
