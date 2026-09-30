"""Build a synthetic Docker context to prove runtime artefacts are excluded."""

import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "PRIVATE_CONTEXT_SENTINEL"


def main():
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        context = temporary / "context"
        output = temporary / "output"
        context.mkdir()
        output.mkdir()
        shutil.copy2(ROOT / ".dockerignore", context / ".dockerignore")
        (context / "Dockerfile").write_text("FROM scratch\nCOPY apps/ /apps/\n", encoding="utf-8")
        expected_missing = (
            "apps/bc.log",
            "apps/.bc_ui_run.json",
            "apps/recipes_playwright/.code/recorded.py",
            "apps/db/bc.db",
            "apps/profiles/account/Cookies",
            "apps/Downloads/invoice.pdf",
        )
        for name in expected_missing:
            path = context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(SENTINEL, encoding="utf-8")
        included = context / "apps" / "BillCollector.py"
        included.write_text("# harmless source\n", encoding="utf-8")
        subprocess.run(
            ["docker", "build", "--quiet", "--output", f"type=local,dest={output}", str(context)],
            check=True,
            capture_output=True,
            text=True,
        )
        assert (output / "apps" / "BillCollector.py").is_file(), "source was not copied"
        for name in expected_missing:
            assert not (output / name).exists(), f"runtime artifact entered Docker image: {name}"
        for path in output.rglob("*"):
            if path.is_file():
                assert SENTINEL not in path.read_text(encoding="utf-8"), \
                    f"sentinel entered Docker output: {path.relative_to(output)}"
    print("Docker context canary: runtime artifacts excluded")


if __name__ == "__main__":
    main()
