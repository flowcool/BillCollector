import hashlib
import json
import os
import shutil
from pathlib import Path


STATE_VERSION = 1


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DownloadState:
    """Persistent, privacy-preserving download history for one account."""

    def __init__(self, state_dir, account_id):
        self.state_dir = Path(state_dir)
        self.state_file = self.state_dir / "downloads-v1.json"
        self.account_key = sha256_text(account_id)
        self.data = self._load()

    def _load(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_file.exists():
            return {"version": STATE_VERSION, "accounts": {}}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("BillCollector download state is invalid") from error
        if data.get("version") != STATE_VERSION:
            raise RuntimeError(
                f"Unsupported download state version: {data.get('version')}")
        if not isinstance(data.get("accounts"), dict):
            raise RuntimeError("BillCollector download state is invalid")
        return data

    def _account(self):
        return self.data["accounts"].setdefault(
            self.account_key, {"urls": [], "content": []})

    def has_url(self, url):
        return sha256_text(url) in self._account()["urls"]

    def publish(self, url, source_path, output_dir):
        source = Path(source_path)
        if not source.is_file():
            raise RuntimeError(
                "Downloaded file disappeared before deduplication")

        account = self._account()
        url_hash = sha256_text(url)
        content_hash = sha256_file(source)
        if content_hash in account["content"]:
            source.unlink()
            if url_hash not in account["urls"]:
                account["urls"].append(url_hash)
            self._save()
            return None

        destination_dir = Path(output_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name
        if destination.exists():
            raise RuntimeError(
                f"Output file already exists: {destination.name}")
        shutil.move(str(source), str(destination))

        account["urls"].append(url_hash)
        account["content"].append(content_hash)
        self._save()
        return destination.name

    def _save(self):
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        os.replace(temporary, self.state_file)
