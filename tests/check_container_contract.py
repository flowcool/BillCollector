"""Run inside the built image with one tmpfs mounted at /publication."""

import sys
import os
import stat
import subprocess
import tempfile
import traceback
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
    assert os.geteuid() == 5678, "image must run as its dedicated non-root user"
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


def startup_contract():
    """The non-root entrypoint must reach its work, not die on an unwritable path."""
    assert os.geteuid() == 5678, "startup contract runs as the image user"
    log_file = Path(os.environ["BILLCOLLECTOR_LOG_FILE"])
    result = subprocess.run(
        [sys.executable, "BillCollector.py", "bc_test.ini"], cwd="/apps",
        capture_output=True, text=True, timeout=120,
        env={k: v for k, v in os.environ.items() if k not in ("VAULT_HOST", "BW_API_URL")},
    )
    output = result.stdout + result.stderr
    assert "PermissionError" not in output, output
    assert log_file.is_file() and log_file.stat().st_size > 0, "entrypoint wrote no log"
    assert result.returncode == 1, f"vault-less run must fail cleanly, got {result.returncode}"
    print("container contract: non-root entrypoint starts and logs")


def _run_as(uid, gid, action):
    pid = os.fork()
    if pid == 0:
        try:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
            action()
        except BaseException:
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, f"{action.__name__} failed"


def shared_group_contract():
    assert os.geteuid() == 0, "privilege-dropping contract needs an isolated root test process"
    app_uid = app_gid = 5678
    consumer_uid = 5679
    with tempfile.TemporaryDirectory(dir="/publication") as directory:
        root = Path(directory)
        os.chown(root, app_uid, app_gid)

        def publish():
            with DownloadPublisher(root / "state", root / "output", root / "staging",
                                   shared_gid=app_gid) as publisher:
                assert publisher.publish(FakeDownload(), service="lab", account="lab account")

        _run_as(app_uid, app_gid, publish)
        output = root / "output"
        published = next(output.glob("*.pdf"))
        assert stat.S_IMODE(root.stat().st_mode) == 0o710
        assert stat.S_IMODE(output.stat().st_mode) == 0o2770
        assert stat.S_IMODE(published.stat().st_mode) == 0o640
        assert stat.S_IMODE((root / "state").stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "staging").stat().st_mode) == 0o700

        def consume():
            assert published.read_bytes() == PDF
            published.unlink()
            try:
                next((root / "state").iterdir())
            except PermissionError:
                return
            raise AssertionError("consumer must not access private publication state")

        _run_as(consumer_uid, app_gid, consume)
    print("container contract: separate group member read/consume passed")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--shared-group":
        shared_group_contract()
    elif len(sys.argv) == 2 and sys.argv[1] == "--startup":
        startup_contract()
    else:
        main()
