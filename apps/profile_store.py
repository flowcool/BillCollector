"""Local, per-account Chromium profile storage.

The caller must provide a stable account identifier (the Bitwarden item name),
not a password or a session token. The profile root must live on a persistent,
trusted volume if sessions should survive container replacement.
"""

import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_PDF_PREFERENCES = {"plugins": {"always_open_pdf_externally": True}}


def _open_private_directory(path, *, dir_fd=None, managed_child=False):
    try:
        os.mkdir(path, mode=0o700, dir_fd=dir_fd)
    except FileExistsError:
        pass
    fd = os.open(path, _DIR_FLAGS, dir_fd=dir_fd)
    try:
        if managed_child:
            os.fchmod(fd, 0o700)
        elif stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
            raise PermissionError("Profile root must not be accessible by group or others")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _ensure_pdf_preferences(default_fd):
    try:
        preferences_fd = os.open("Preferences", _FILE_FLAGS, dir_fd=default_fd)
    except FileNotFoundError:
        temporary_name = ".Preferences-" + secrets.token_hex(12) + ".tmp"
        temporary_created = False
        try:
            preferences_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=default_fd,
            )
            temporary_created = True
            with os.fdopen(preferences_fd, "w", encoding="utf-8") as preferences:
                os.fchmod(preferences.fileno(), 0o600)
                json.dump(_PDF_PREFERENCES, preferences)
                preferences.flush()
                os.fsync(preferences.fileno())
            try:
                # Linking publishes the complete file only if no Preferences exists.
                os.link(temporary_name, "Preferences", src_dir_fd=default_fd,
                        dst_dir_fd=default_fd)
                os.fsync(default_fd)
                return
            except FileExistsError:
                # Another process won the race; never overwrite browser state.
                preferences_fd = os.open("Preferences", _FILE_FLAGS, dir_fd=default_fd)
        finally:
            if temporary_created:
                os.unlink(temporary_name, dir_fd=default_fd)

    try:
        if not stat.S_ISREG(os.fstat(preferences_fd).st_mode):
            raise ValueError("Chromium Preferences is not a regular file")
        os.fchmod(preferences_fd, 0o600)
    finally:
        os.close(preferences_fd)


def prepare_profile(root, account_id):
    """Return a private, reusable Chromium profile path for one stable account.

    Existing browser data is never removed or reset. A new profile receives
    Chromium's PDF-download preference; an existing Preferences file is kept.
    """
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("A non-empty stable account identifier is required")

    root = os.fspath(root)
    if not isinstance(root, str) or not root.strip():
        raise ValueError("A non-empty profile root is required")
    root = os.path.abspath(root)
    account_dir = "account-v1-" + hashlib.sha256(account_id.encode("utf-8")).hexdigest()

    # The private root is checked below; its parents remain an operator trust boundary.
    os.makedirs(os.path.dirname(root), mode=0o700, exist_ok=True)
    root_fd = _open_private_directory(root)
    try:
        account_fd = _open_private_directory(account_dir, dir_fd=root_fd, managed_child=True)
        try:
            default_fd = _open_private_directory("Default", dir_fd=account_fd, managed_child=True)
            try:
                _ensure_pdf_preferences(default_fd)
            finally:
                os.close(default_fd)
        finally:
            os.close(account_fd)
    finally:
        os.close(root_fd)

    return os.path.join(root, account_dir)


@contextmanager
def locked_profile(root, account_id):
    """Hold an exclusive same-account lock for the whole browser lifetime."""
    profile_dir = prepare_profile(root, account_id)
    root_fd = _open_private_directory(os.path.abspath(os.fspath(root)))
    lock_name = "." + os.path.basename(profile_dir) + ".lock"
    lock_fd = None
    try:
        lock_fd = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=root_fd)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError("Profile lock is not a regular file")
        os.fchmod(lock_fd, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("This account profile is already in use") from exc
        yield profile_dir
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(root_fd)
