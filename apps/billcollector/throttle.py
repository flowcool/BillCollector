"""Durable request pacing for independent login and document transactions.

Call before dispatch, never between reading a fresh OTP and submitting it.
Namespaces contain public scope labels only, never credentials or account IDs.
"""
import fcntl
import json
import math
import os
import re
import time

from . import config
from .collection import CollectionError


class ThrottleError(CollectionError):
    """Request pacing cannot complete within its configured boundary."""


def request_interval(db, kind, *, default, minimum=0.1):
    """Read generic Settings without adding schema columns or task fields."""
    if kind not in {'login', 'document'}:
        raise ValueError('Unknown request gate')
    raw = db.get_setting(f'{kind}_request_interval_seconds')
    try:
        value = default if raw is None or raw == '' else float(raw)
        if isinstance(raw, bool) or not math.isfinite(value) or not minimum <= value <= 3600:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ThrottleError('Request interval is outside its supported bounds') from None
    return value


def wait_for_request(page, remaining, *, namespace, interval):
    """Serialize dispatches in one namespace; include lock and pacing in deadline.

    Separate namespaces have separate durable clocks. Existing namespace files
    remain usable after upgrade. No request or retry is performed by this gate.
    """
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', namespace):
        raise ValueError('Invalid request gate namespace')
    if not math.isfinite(interval) or not 0.1 <= interval <= 3600:
        raise ThrottleError('Request interval is outside its supported bounds')
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.RUN_DIR / f'{namespace}.lock', 'a') as lock:
        while True:
            remaining()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if remaining() <= 100:
                    raise ThrottleError('Request lock wait exceeds the remaining deadline') from None
                page.wait_for_timeout(100)
        path = config.RUN_DIR / f'{namespace}.json'
        last = json.loads(path.read_text())['at'] if path.exists() else None
        if last is not None and (type(last) not in (int, float) or not math.isfinite(last)):
            raise ThrottleError('Request gate state is invalid')
        wait = max(0, last + interval - time.time()) if last is not None else 0
        if wait * 1000 >= remaining():
            raise ThrottleError('Request rate wait exceeds the remaining deadline')
        if wait:
            page.wait_for_timeout(wait * 1000)
        remaining()  # A slow/interrupted wait must not reserve a new dispatch.
        temporary = path.with_suffix('.pending')
        with temporary.open('w') as stream:
            json.dump({'at': time.time()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(config.RUN_DIR, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
