"""Run inside the built image with one tmpfs mounted at /publication."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/apps")
from download_publication import DownloadPublisher


PDF = b"%PDF-1.4\n%%EOF\n"


class FakeDownload:
    def failure(self):
        return None

    def save_as(self, path):
        Path(path).write_bytes(PDF)


def main():
    assert not Path("/apps/.env").exists(), "environment file entered image"
    assert not Path("/apps/profiles").exists(), "legacy profile entered image"
    assert not Path("/apps/db/bc.db").exists(), "run database entered image"
    assert not Path("/apps/Downloads").exists(), "download output entered image"
    profile_root = Path("/var/lib/billcollector/profiles")
    assert profile_root.is_dir() and not any(profile_root.iterdir()), "profile data entered image"
    with tempfile.TemporaryDirectory(dir="/publication") as directory:
        root = Path(directory)
        with DownloadPublisher(root / "state", root / "output", root / "staging") as publisher:
            assert publisher.publish(FakeDownload(), service="lab", account="lab account")
            assert not publisher.publish(FakeDownload(), service="lab", account="lab account")
        output = list((root / "output").glob("*.pdf"))
        assert len(output) == 1 and output[0].read_bytes() == PDF
        assert not list((root / "staging").iterdir())
    print("container contract: private image context and same-mount rename passed")


if __name__ == "__main__":
    main()
