"""Prove the documented Compose runtime in a disposable, offline Docker lab.

Run: python tests/check_container_compose.py --image billcollector:daemon-local
Tests default named volumes and pre-owned bind mounts at UID/GID 1003:1000.
No published ports, live vault, real provider or existing state is accepted.
"""

import argparse
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(args, *, environment=None, timeout=120):
    result = subprocess.run(args, env=environment, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Lab command failed: {args[:5]}\n{result.stderr[-4000:]}\n{result.stdout[-2000:]}")
    return result.stdout


def check(image, uid):
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("BC_", "BW_", "VAULT_", "COMPOSE_", "BEADS_"))}
    endpoint = command(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                       environment=environment).strip()
    if not endpoint.startswith("unix://") or environment.get("DOCKER_HOST", "unix://").startswith(("tcp:", "ssh:")):
        raise RuntimeError("The synthetic lab requires a local Unix-socket Docker engine")
    environment.update(BC_IMAGE=image, BC_UID=str(uid), BC_GID="1000")
    with tempfile.TemporaryDirectory(prefix="bc-compose-lab-") as directory:
        temporary = Path(directory)
        certs = temporary / "certs"
        certs.mkdir()
        command(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                 "-keyout", str(certs / "key.pem"), "-out", str(certs / "cert.pem")])
        # Synthetic test key only. Its read-only mount must work for image UID 1000.
        (certs / "key.pem").chmod(0o644)
        configuration = json.loads(command(
            ["docker", "compose", "-f", str(ROOT / "compose/daemon.compose.yaml"), "config", "--format", "json"],
            environment=environment))
        app = configuration["services"]["billcollector"]
        app.pop("ports", None)
        app["environment"].update(
            BW_API_URL="http://fixture:8087", BW_API_HOST="127.0.0.1:8087",
            BC_NOVNC_PATH="/browser", BC_NOVNC_HOST="0.0.0.0", NTFY_HOST="http://fixture:8080",
        )
        app["volumes"].append({"type": "bind", "source": str(ROOT / "tests"), "target": "/checks", "read_only": True})
        private = []
        if uid != 1000:
            for name, target in (("state", "/apps/data"), ("recipes", "/apps/recipes")):
                path = temporary / name
                path.mkdir()
                private.append(path)
                app["volumes"] = [v for v in app["volumes"] if v["target"] != target]
                app["volumes"].append({"type": "bind", "source": str(path), "target": target})
        configuration["services"]["fixture"] = {
            "image": image, "user": "1000:1000", "init": True,
            "command": ["python3", "/checks/container/fixture.py"],
            "volumes": [f"{ROOT / 'tests'}:/checks:ro", f"{certs}:/certs:ro"],
            "networks": {"default": {}}, "shm_size": "1gb",
        }
        configuration["networks"] = {"default": {"internal": True}}
        configuration.pop("name", None)
        # Resolved Compose output contains generated names from its source project.
        # Let this unique lab project allocate its own volumes instead.
        for definition in configuration.get("volumes", {}).values():
            definition.pop("name", None)
        compose_file = temporary / "compose.json"
        compose_file.write_text(json.dumps(configuration))
        compose = ["docker", "compose", "-p", "bccontract-" + uuid.uuid4().hex[:12], "-f", str(compose_file)]

        def execute(*args, timeout=120):
            return command(compose + list(args), environment=environment, timeout=timeout)

        def app_check(action):
            return execute("exec", "-T", "billcollector", "python3", "/checks/container/state.py", action)

        def status():
            return json.loads(app_check("status"))

        def provision(owner):
            for path in private:
                # Only newly created, private lab directories are mounted for chown.
                command(["docker", "run", "--rm", "--network", "none", "--user", "0:0",
                         "-v", f"{path}:/lab", "--entrypoint", "python3", image, "-c",
                         "import os; from pathlib import Path; "
                         f"paths=[Path('/lab'), *Path('/lab').rglob('*')]; "
                         f"[(os.chown(p,{owner},1000,follow_symlinks=False), os.chmod(p,0o700) if p.is_dir() and not p.is_symlink() else None) for p in paths]"],
                        environment=environment)

        runner = None
        try:
            provision(uid)
            execute("up", "-d", "--wait", "--wait-timeout", "90")
            assert int(execute("exec", "-T", "billcollector", "id", "-u").strip()) == uid
            assert status()["tasks"] == 0
            execute("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "90", "billcollector")
            assert status()["tasks"] == 0
            print(f"PASS uid={uid} empty startup/recreation", flush=True)
            print(app_check("seed").strip(), flush=True)
            for iteration in (1, 2):
                with tempfile.TemporaryFile(mode="w+") as log:
                    runner = subprocess.Popen(compose + ["exec", "-T", "billcollector", "python3", "-m", "billcollector", "run", "1"],
                                              env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        snapshot = status()
                        if (len(snapshot["runs"]) == iteration
                                and snapshot["runs"][0]["result"] is None
                                and snapshot["runs"][0]["pause_reason"] == "wait_user"):
                            break
                        if runner.poll() is not None:
                            log.seek(0)
                            raise RuntimeError("Synthetic runner exited before pause:\n" + log.read()[-4000:])
                        time.sleep(1)
                    else:
                        raise RuntimeError("Synthetic runner did not pause")
                    if iteration == 1:
                        print(execute("exec", "-T", "fixture", "python3", "/checks/container/browser.py").strip(), flush=True)
                    print(app_check("resume").strip(), flush=True)
                    result = runner.wait(timeout=60)
                    runner = None
                    if result:
                        log.seek(0)
                        raise RuntimeError("Synthetic run failed:\n" + log.read()[-4000:])
                snapshot = status()
                assert snapshot["runs"][0]["result"] == "success"
                assert len(snapshot["downloads"]) == iteration
                if iteration == 1:
                    execute("restart", "--timeout", "120", "billcollector")
                    execute("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "90", "billcollector")
                    print(app_check("persistence").strip(), flush=True)
                else:
                    assert snapshot["downloads"][1]["duplicate_of_id"] == snapshot["downloads"][0]["id"]
                    assert snapshot["downloads"][0]["saved_path"] == snapshot["downloads"][1]["saved_path"]
            metrics = execute("exec", "-T", "fixture", "python3", "-c",
                              "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode())")
            assert json.loads(metrics)["visits"] == [None, "yes"]
            container = execute("ps", "-q", "billcollector").strip()
            changes = command(["docker", "diff", container], environment=environment)
            unexpected = [line for line in changes.splitlines()
                          if line[2:].startswith("/apps/")
                          and not line[2:].startswith(("/apps/data", "/apps/recipes"))]
            assert not unexpected, unexpected
            print(f"PASS uid={uid} headed collection, dedup and persistent browser cookie", flush=True)
        finally:
            if runner is not None and runner.poll() is None:
                execute("stop", "--timeout", "30", "billcollector")
                runner.wait(timeout=40)
            execute("down", "-v", "--remove-orphans")
            provision(os.getuid())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--uid", type=int, choices=(1000, 1003))
    args = parser.parse_args()
    for uid in [args.uid] if args.uid is not None else [1000, 1003]:
        check(args.image, uid)


if __name__ == "__main__":
    main()
