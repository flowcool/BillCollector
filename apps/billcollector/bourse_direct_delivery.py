"""Acknowledge one independently verified Paperless receipt; never upload."""
import argparse
import fcntl
import json
from pathlib import Path

from . import config
from .bourse_direct import reconcile_delivery
from .db import Database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=int, required=True)
    parser.add_argument('--download', type=int, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    config.load_env()
    config.ensure_dirs()
    lock_dir = config.DATA_DIR / 'task-locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f'task-{args.task}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = Database()
        try:
            row = db.conn.execute('SELECT task_id FROM Download WHERE id=?',
                                  (args.download,)).fetchone()
            if row is None or row['task_id'] != args.task:
                raise ValueError('Download does not belong to the selected task')
            reconcile_delivery(db, args.download, json.loads(args.receipt.read_text()))
            print('Receipt recorded; no document sent')
        finally:
            db.close()


if __name__ == '__main__':
    main()
