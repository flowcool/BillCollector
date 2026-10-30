# -*- coding: utf-8 -*-
"""Durable download publication and at-least-once consumer delivery.

The caller's task lock serializes recipe execution. An output-directory lock
also serializes publication/recovery across tasks and daemon startup. SQLite
rows are committed before publication and before any consumer effect.
"""

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import stat
# Fixed child interpreter/module, argument list, and no shell.
import subprocess  # nosec B404
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from . import config, notice
from .consumers import CONSUMERS
from .interrupt import RunInterrupted
from .redaction import sanitize_url

logger = logging.getLogger(__name__)
# Private recovery-child exit codes. Parent diagnostics contain fixed text only.
_RECOVERY_FAILURES = {65: "checksum mismatch", 66: "reserved destination occupied",
                      70: "database failure", 74: "filesystem failure", 77: "permission denied"}


class NoticeDeliveryUncertain(ValueError):
    """A provider consumer may have acted; retain state without automatic replay."""


class _RecoveryFailure(OSError):
    def __init__(self, message, exit_code):
        super().__init__(message)
        self.exit_code = exit_code


_CHUNK = 1024 * 1024
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
_FIELDS = ("run_id", "task_id", "url", "filename", "size", "sha256", "saved_path")


def sha256_of(path):
    """Chunked sha256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _unpack_pdf(staged):
    """Replace a staged ZIP by its single PDF member; return the member name.

    The member bytes are kept unchanged. Anything but exactly one regular PDF
    member with a plain name fails closed before any publication.
    """
    if staged.stat().st_size > MAX_UNPACKED_BYTES:
        raise ValueError("unpack: pdf archive exceeds the size limit")
    try:
        archive = zipfile.ZipFile(staged)
    except zipfile.BadZipFile as error:
        raise ValueError("unpack: pdf expects a ZIP download") from error
    with archive:
        entries = archive.infolist()
        if len(entries) > 128:
            raise ValueError("unpack: pdf archive has too many entries")
        for entry in entries:
            name = entry.orig_filename
            mode = entry.external_attr >> 16
            if ("\x00" in name or "\\" in name or PureWindowsPath(name).drive
                    or PurePosixPath(name).is_absolute()
                    or ".." in PurePosixPath(name).parts
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("unpack: pdf archive contains an unsafe member")
        members = [m for m in entries if not m.is_dir()]
        if len(members) != 1:
            raise ValueError(f"unpack: pdf expects one file member, found {len(members)}")
        member = members[0]
        name = member.filename
        if (Path(name).name != name or name in ("", ".", "..", ".billcollector")
                or len(os.fsencode(name)) > 255
                or stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG)
                or "\\" in name or not name.lower().endswith(".pdf")):
            raise ValueError("unpack: pdf member name is not a plain .pdf filename")
        if member.file_size > MAX_UNPACKED_BYTES:
            raise ValueError("unpack: pdf member exceeds the size limit")
        unpacked = staged.with_name("unpacked")
        with archive.open(member) as source, unpacked.open("xb") as target:
            written = 0
            for chunk in iter(lambda: source.read(_CHUNK), b""):
                written += len(chunk)
                if written > MAX_UNPACKED_BYTES:
                    raise ValueError("unpack: pdf member exceeds the size limit")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    with unpacked.open("rb") as stream:
        # Some original portal PDFs carry a UTF-8 BOM. Recognize it without
        # removing or otherwise transforming the original member bytes.
        signature = stream.read(8)
        if not (signature.startswith(b"%PDF-") or signature.startswith(b"\xef\xbb\xbf%PDF-")):
            unpacked.unlink()
            raise ValueError("unpack: pdf member is not a PDF document")
    os.replace(unpacked, staged)
    _sync_dir(staged.parent)
    return name


def download_dir(db):
    """The download folder: the Settings override wins, else the default."""
    value = db.get_setting("download_dir")
    return value if value else str(config.DOWNLOAD_DIR)


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _publication_lock(target):
    target.mkdir(parents=True, exist_ok=True)
    control = target / ".billcollector"
    control.mkdir(exist_ok=True)
    with (control / "publication.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _journal_root(target, db):
    # Separate independent databases sharing an output directory. Recovery must
    # never adopt another database's task IDs or orphan manifests.
    identity = hashlib.sha256(os.fsencode(Path(db.path).resolve())).hexdigest()[:16]
    return target / ".billcollector" / identity


def _deliver(row, db):
    if row["duplicate_of_id"] is not None:
        return
    record = {k: row[k] for k in _FIELDS}
    record["idempotency_key"] = f"{row['task_id']}:{row['sha256']}"
    policy = row.get('delivery_policy') if isinstance(row, dict) else row['delivery_policy']
    source = row.get('source_identity') if isinstance(row, dict) else row['source_identity']
    # Preserve pre-policy BD journal rows only when the durable acquisition key
    # positively binds this exact source. Do not convert generic notice history.
    strict = policy == 'bourse-direct' or (source is not None and
        db.get_setting(f"bourse_direct_pending_{row['task_id']}") == source)
    if strict:
        # Commit before any external effect. A crash never permits automatic replay.
        db.set_download_state(row['id'], 'delivery_uncertain')
    failed = False
    for consume in CONSUMERS:
        saved = Path(row["saved_path"])
        if saved.exists() and (not saved.is_file() or sha256_of(saved) != row["sha256"]):
            raise _RecoveryFailure("Published download checksum mismatch", 65)
        try:
            consume(record.copy())
        except RunInterrupted:
            raise
        except Exception:
            failed = True
            # Consumer exceptions can contain document URLs or credentials.
            logger.error("Download consumer failed; delivery remains pending")
            if strict:
                break
    if strict and failed:
        raise NoticeDeliveryUncertain('Notice delivery is uncertain; reconcile before another action')
    if not failed:
        db.set_download_state(row["id"], "consumed")


def _clean_journal(entry):
    for name in ("document", "manifest.json"):
        (entry / name).unlink(missing_ok=True)
    entry.rmdir()
    _sync_dir(entry.parent)


def _clean_journal_tree(entry):
    for name in ("document", "unpacked"):
        (entry / name).unlink(missing_ok=True)
    entry.rmdir()
    _sync_dir(entry.parent)


def _quarantine(entry):
    quarantine = entry.parent / "quarantine"
    quarantine.mkdir(exist_ok=True)
    os.rename(entry, quarantine / entry.name)
    _sync_dir(quarantine)
    _sync_dir(entry.parent)
    logger.warning("Uncommitted download quarantined; no document published")


def _publish(row, db):
    staged = Path(row["staged_path"])
    saved = Path(row["saved_path"])
    if staged.exists():
        if sha256_of(staged) != row["sha256"]:
            raise _RecoveryFailure("Staged download checksum mismatch", 65)
        if saved.exists():
            raise _RecoveryFailure("Reserved download destination already exists", 66)
        os.rename(staged, saved)
        _sync_dir(saved.parent)
        _sync_dir(staged.parent)
    elif saved.exists() and sha256_of(saved) != row["sha256"]:
        raise _RecoveryFailure("Published download checksum mismatch", 65)
    # A downstream folder watcher may already have moved the published file.
    # The committed intent and disappearance of the staged file retain the
    # delivery obligation; consumers use the stable key on re-delivery.
    db.set_download_state(row["id"], "published")


def _recover_locked(db, target):
    root = _journal_root(target, db)
    rows = {r["staged_path"]: dict(r) for r in db.list_downloads()
            if r["staged_path"] and Path(r["saved_path"]).parent == target}
    if root.exists():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name == "quarantine":
                continue
            staged = entry / "document"
            row = rows.get(str(staged))
            if row is None:
                try:
                    metadata = json.loads((entry / "manifest.json").read_text())
                    saved = Path(metadata["saved_path"])
                    valid = (saved.parent == target and Path(metadata["filename"]).name == metadata["filename"]
                             and db.get_task(metadata["task_id"]) is not None)
                    if not valid:
                        raise ValueError("Invalid publication manifest")
                except (OSError, ValueError, KeyError, TypeError):
                    _quarantine(entry)
                    continue
                if (metadata.get('delivery_policy') == 'bourse-direct'
                        or (metadata.get('source_identity') and
                            db.get_setting(f"bourse_direct_pending_{metadata['task_id']}")
                            == metadata['source_identity'])):
                    db.set_setting(f"bourse_direct_pending_{metadata['task_id']}",
                                   metadata['source_identity'])
                    logger.warning('Uncommitted notice retained for operator reconciliation')
                    continue
                if saved.is_file() and sha256_of(saved) == metadata["sha256"]:
                    # Conservative recovery of publication without its row:
                    # do not replay an unknown external consumer effect.
                    db.insert_download(**{k: metadata[k] for k in _FIELDS},
                                       staged_path=str(staged), state="consumed",
                                       source_key=metadata.get("source_key"),
                                       source_identity=metadata.get("source_identity"),
                                       revision_sha256=metadata.get("revision_sha256"),
                                       delivery_policy=metadata.get("delivery_policy"))
                    _clean_journal(entry)
                else:
                    _quarantine(entry)
    for row in rows.values():
        if row['state'] == 'delivery_uncertain':
            logger.warning('Notice delivery is uncertain; automatic resend refused')
            continue
        entry = Path(row["staged_path"]).parent
        if row["state"] == "staged":
            _publish(row, db)
            row["state"] = "published"
        if row["state"] == "published":
            try:
                _deliver(row, db)
            except NoticeDeliveryUncertain:
                # The affected provider stops; unrelated tasks can recover/save.
                logger.warning('Notice recovery needs a receipt; other tasks may continue')
                continue
        if entry.exists():
            # Failed consumers retain the journal and pending row for retry.
            current = db.find_download_by_sha(row["task_id"], row["sha256"])
            if row["state"] == "consumed" or (current and current["state"] == "consumed"):
                _clean_journal(entry)


def recover_downloads(db):
    """Reconcile managed publications and retry pending deliveries at startup.

    Also visit old output roots retained in pending rows after a setting change.
    Unknown staging files are quarantined. Unmanaged files are never assigned
    to a task by guessing from their name.
    """
    targets = {Path(download_dir(db)).resolve()}
    targets.update(Path(r["saved_path"]).parent for r in db.list_downloads()
                   if r["staged_path"] and (r["state"] != "consumed"
                                             or Path(r["staged_path"]).parent.is_dir()))
    for target in sorted(targets):
        with _publication_lock(target):
            _recover_locked(db, target)


def track_download(download, run_id, task, db, *, source_key=None, unpack=None, identity=None,
                   delivery_policy=None):
    """Stage one Playwright Download, commit intent, publish, then deliver.

    ``source_key`` records the collection key for incremental runs.
    ``unpack="pdf"`` publishes the single PDF member of a ZIP download.
    Existing same-task hashes retain an audit row without another publication.
    Generic consumer failures remain pending without failing the recipe.
    Bourse Direct failures raise after committing uncertainty; recovery skips
    that provider effect. Filesystem/DB errors propagate. Recovery precedes saves.
    """
    if delivery_policy not in {None, 'bourse-direct'}:
        raise ValueError('Unsupported delivery policy')
    if delivery_policy is not None and identity is None:
        raise ValueError('Strict delivery requires notice identity')
    target = Path(download_dir(db)).resolve()
    with _publication_lock(target):
        if identity is not None and any(r['state'] == 'delivery_uncertain'
                                       for r in db.list_downloads(task_id=task['id'])):
            raise ValueError('Notice delivery needs reconciliation before collection')
        _recover_locked(db, target)
        # Preserve the fork's basename normalization for portal filenames.
        filename = str(download.suggested_filename).replace("\\", "/").rsplit("/", 1)[-1]
        if filename in ("", ".", ".."):
            filename = "download"
        if filename == ".billcollector":
            raise ValueError("Download filename conflicts with publication state")
        root = _journal_root(target, db)
        root.mkdir(parents=True, exist_ok=True)
        entry = root / uuid4().hex
        entry.mkdir()
        _sync_dir(root)
        staged = entry / "document"
        download.save_as(str(staged))
        with staged.open("rb") as stream:
            os.fsync(stream.fileno())
        if unpack == "pdf":
            try:
                filename = _unpack_pdf(staged)
            except Exception:
                _clean_journal_tree(entry)
                raise
        elif unpack is not None:
            _clean_journal_tree(entry)
            raise ValueError(f"unsupported unpack mode: {unpack}")
        sha = sha256_of(staged)
        record = {
            "run_id": run_id, "task_id": task["id"],
            "url": sanitize_url(str(download.url)), "filename": filename,
            "size": staged.stat().st_size, "sha256": sha,
            "source_key": source_key, "delivery_policy": delivery_policy,
        }
        if identity is not None:
            record['source_identity'] = notice.source_digest(str(download.url), identity)
            record['revision_sha256'] = notice.revision_digest(staged)
            existing = db.find_notice_revision(task['id'], record['source_identity'],
                                               record['revision_sha256'])
        else:
            existing = db.find_download_by_sha(task["id"], sha)
        if existing is not None:
            record["saved_path"] = existing["saved_path"]
            db.insert_download(**record, duplicate_of_id=existing["id"])
            _clean_journal(entry)
            return {**record, "duplicate": True}
        saved = target / filename
        # Legacy publication without a row: adopt only an exact filename/hash
        # match observed on this task's new download. Do not invoke consumers.
        rows = db.list_downloads()
        known = {r["saved_path"] for r in rows}
        reserved = {r["saved_path"] for r in rows if r["state"] != "consumed"}
        orphan = str(saved) not in known and saved.is_file() and sha256_of(saved) == sha
        if not orphan:
            stem, ext = os.path.splitext(filename)
            index = 0
            while saved.exists() or str(saved) in reserved:
                index += 1
                saved = target / f"{stem} ({index}){ext}"
        record["saved_path"] = str(saved)
        # Keep the portal filename as metadata; saved_path identifies storage.
        with (entry / "manifest.json").open("x") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_dir(entry)
        download_id = db.insert_download(**record, staged_path=str(staged),
                                         state="consumed" if orphan else "staged")
        if not orphan:
            row = {**record, "id": download_id, "staged_path": str(staged),
                   "duplicate_of_id": None}
            _publish(row, db)
            _deliver(row, db)
        current = db.find_download_by_sha(task["id"], sha)
        if current["state"] == "consumed":
            _clean_journal(entry)
        return {**record, "duplicate": False}


def recover_downloads_in_subprocess(db):
    """Finish recovery before the daemon starts its UI or scheduler.

    Keep consumers out of the UI process, as in normal runner execution.
    The child opens its own SQLite connection and uses the publication lock.
    """
    try:
        timeout = int(db.get_setting("download_recovery_timeout_seconds"))
        if timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise RuntimeError(
            "Download recovery timeout must be a positive integer; daemon not started") from None
    # Only the database path varies; it is a positional module argument.
    try:
        result = subprocess.run(  # nosec B603
            [sys.executable, "-m", "billcollector.downloads", str(Path(db.path).resolve())],
            cwd=config.APP_DIR, check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        # run() kills and waits for the child before raising. Do not include
        # its command, output or database path in the startup diagnostic.
        raise RuntimeError(
            f"Download startup recovery timed out after {timeout}s; daemon not started") from None
    if result.returncode:
        reason = _RECOVERY_FAILURES.get(result.returncode)
        diagnostic = f"; {reason}" if reason else ""
        raise RuntimeError(
            f"Download startup recovery failed (exit code {result.returncode}{diagnostic}); daemon not started")


if __name__ == "__main__":
    from .db import Database

    database = None
    try:
        database = Database(path=sys.argv[1])
        recover_downloads(database)
    except _RecoveryFailure as error:
        sys.exit(error.exit_code)
    except PermissionError:
        sys.exit(77)
    except sqlite3.Error:
        sys.exit(70)
    except OSError:
        sys.exit(74)
    except Exception:
        # Do not persist raw exception text or paths from recovery/consumers.
        sys.exit(1)
    finally:
        if database is not None:
            database.close()
