"""Local, DMS-agnostic publication of completed Playwright downloads.

Integration boundary: hold a DownloadPublisher context for the *entire* account
run and call publish() for each Playwright Download. The state and staging
directories must be persistent and private; output_dir is the directory
consumed by the downstream DMS. Staging must be a sibling on the *same mount*,
outside the consumer's watched tree. No browser, recipe, scheduler, or Paperless
dependency is required here.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path


class PublicationError(RuntimeError):
    """A download cannot be safely published or recovered."""


class RunLocked(PublicationError):
    """Another publisher is already using this state and output."""


class DownloadPublisher:
    """Publish content once per stable account identity and byte-for-byte digest.

    A durable prepared record precedes a durable rename intent. Recovery may
    retry only before that intent: once a rename could have occurred, an absent
    output might mean that the consumer already took it. Ambiguous states stop
    for manual recovery instead of risking duplicate publication.
    """

    def __init__(self, state_dir: str | Path, output_dir: str | Path, staging_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.output_dir = Path(output_dir)
        self.stage_dir = Path(staging_dir)
        self._lock_fd: int | None = None
        self._db: sqlite3.Connection | None = None

    def __enter__(self) -> "DownloadPublisher":
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.stage_dir.is_dir() or self.stage_dir.is_symlink():
            raise PublicationError("Staging path must be a private directory")
        paths = [self.state_dir.resolve(), self.output_dir.resolve(), self.stage_dir.resolve()]
        if len(set(paths)) != 3 or any(
            left in right.parents for left in paths for right in paths if left != right
        ):
            raise PublicationError("State, output, and staging must be disjoint")
        if self.stage_dir.stat().st_dev != self.output_dir.stat().st_dev:
            raise PublicationError("Staging and output must be on the same filesystem")
        os.chmod(self.stage_dir, 0o700)
        self._lock_fd = os.open(self.state_dir / "run.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._db = sqlite3.connect(self.state_dir / "publication.sqlite3")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    account_hash TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    stage_name TEXT NOT NULL,
                    final_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('prepared', 'renaming', 'published')),
                    PRIMARY KEY (account_hash, sha256)
                )
            """)
            self._db.commit()
            schema_sql = self._db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
            ).fetchone()[0]
            if "'renaming'" not in schema_sql:
                raise PublicationError(
                    "Publication state schema is incompatible; manual migration required"
                )
            self._sync_directory(self.state_dir)
            self._recover()
            return self
        except BlockingIOError as exc:
            self.__exit__(None, None, None)
            raise RunLocked("Another download run holds the state lock") from exc
        except (sqlite3.DatabaseError, OSError, PublicationError) as exc:
            self.__exit__(None, None, None)
            if isinstance(exc, sqlite3.DatabaseError):
                raise PublicationError("Publication state is corrupt; manual recovery required") from exc
            raise

    def __exit__(self, _type, _value, _traceback) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def publish(self, download, *, service: str, account: str) -> bool:
        """Return True if published, False if already seen for this account.

        ``download`` needs Playwright's ``failure()`` and ``save_as(path)`` API.
        Its suggested filename is deliberately ignored: it is untrusted input.
        """
        if self._db is None or not service or not account:
            raise PublicationError("An active run and stable service/account are required")
        account_hash = hashlib.sha256(f"{service}\0{account}".encode()).hexdigest()
        failure = download.failure()  # Waits for browser download completion.
        if failure is not None:
            raise PublicationError("Browser reported a failed download")

        stage_name = f"{uuid.uuid4().hex}.part"
        stage = self.stage_dir / stage_name
        prepared = False
        try:
            download.save_as(str(stage))
            info = stage.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise PublicationError("Downloaded artifact is not a nonempty regular file")
            os.chmod(stage, 0o600)
            digest = self._digest_and_sync(stage)
            final_name = f"{account_hash[:16]}-{digest}.pdf"
            existing = self._db.execute(
                "SELECT status FROM documents WHERE account_hash=? AND sha256=?",
                (account_hash, digest),
            ).fetchone()
            if existing is not None:
                if existing[0] != "published":
                    raise PublicationError("Previous publication awaits recovery")
                stage.unlink()
                return False
            final = self.output_dir / final_name
            if os.path.lexists(final):
                raise PublicationError("Output path occupied without matching state")

            # The stage entry must survive a crash before SQLite can durably
            # refer to it. fsyncing the file alone does not persist its name.
            self._sync_directory(self.stage_dir)
            self._db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, 'prepared')",
                (account_hash, digest, stage_name, final_name),
            )
            self._db.commit()
            prepared = True
            self._finish(stage, final, account_hash, digest)
            return True
        except Exception:
            # A prepared stage is durable recovery material, not a temporary file.
            if not prepared:
                stage.unlink(missing_ok=True)
            raise

    @staticmethod
    def _digest_and_sync(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            if file.read(5) != b"%PDF-":
                raise PublicationError("Downloaded artifact is not a PDF")
            file.seek(0)
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
            os.fsync(file.fileno())
        return digest.hexdigest()

    @staticmethod
    def _sync_directory(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _set_status(self, account_hash: str, digest: str, status: str) -> None:
        self._db.execute(
            "UPDATE documents SET status=? WHERE account_hash=? AND sha256=?",
            (status, account_hash, digest),
        )
        self._db.commit()

    def _finish(self, stage: Path, final: Path, account_hash: str, digest: str) -> None:
        # Commit intent before rename. If the stage later reappears after a
        # crash while the consumer has taken the output, retry is unsafe.
        self._set_status(account_hash, digest, "renaming")
        os.replace(stage, final)
        # Persist the new destination first, then removal of the source. Both
        # directory updates must be durable before recording publication.
        self._sync_directory(self.output_dir)
        self._sync_directory(self.stage_dir)
        self._set_status(account_hash, digest, "published")

    def _recover(self) -> None:
        rows = self._db.execute(
            "SELECT account_hash, sha256, stage_name, final_name, status "
            "FROM documents WHERE status IN ('prepared', 'renaming')"
        ).fetchall()
        for account_hash, digest, stage_name, final_name, status in rows:
            if (
                not re.fullmatch(r"[0-9a-f]{64}", account_hash)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not re.fullmatch(r"[0-9a-f]{32}\.part", stage_name)
                or final_name != f"{account_hash[:16]}-{digest}.pdf"
            ):
                raise PublicationError("Invalid prepared publication state")
            stage = self.stage_dir / stage_name
            final = self.output_dir / final_name
            has_stage = os.path.lexists(stage)
            has_final = os.path.lexists(final)
            if has_stage and has_final:
                raise PublicationError("Both staged and published artifacts exist")
            if status == "prepared" and has_stage:
                try:
                    valid = stat.S_ISREG(stage.lstat().st_mode) and self._digest_and_sync(stage) == digest
                except PublicationError:
                    valid = False
                if not valid:
                    raise PublicationError("Prepared artifact is corrupt")
                self._finish(stage, final, account_hash, digest)
            elif status == "renaming" and has_final and not has_stage:
                if not stat.S_ISREG(final.lstat().st_mode) or self._digest_and_sync(final) != digest:
                    raise PublicationError("Published artifact is corrupt")
                self._sync_directory(self.output_dir)
                self._sync_directory(self.stage_dir)
                self._set_status(account_hash, digest, "published")
            else:
                raise PublicationError(
                    "Publication state is ambiguous; manual recovery required"
                )
