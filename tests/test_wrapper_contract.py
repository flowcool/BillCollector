"""Exercise the host launcher without Docker, providers, or credentials."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


WRAPPER = Path(__file__).resolve().parents[1] / "BillCollector.sh"


class WrapperContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.wrapper = self.root / "BillCollector.sh"
        shutil.copy2(WRAPPER, self.wrapper)
        (self.root / "apps" / "db").mkdir(parents=True)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._executable(
            "id",
            "#!/bin/sh\ncase \"$1\" in -u) echo 1234;; -g) echo 2345;; *) exit 2;; esac\n",
        )
        self._executable(
            "docker",
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['WRAPPER_TEST_ARGS_FILE'], 'w', encoding='utf-8') as output:\n"
            "    json.dump(sys.argv[1:], output)\n",
        )
        self.args_file = self.root / "docker-args.json"
        self.profile_dir = self.root / "profiles"
        self.publication_dir = self.root / "publication"
        self.environment = {
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "WRAPPER_TEST_ARGS_FILE": str(self.args_file),
            "BILLCOLLECTOR_HOST_PROFILE_DIR": str(self.profile_dir),
            "BILLCOLLECTOR_HOST_PUBLICATION_DIR": str(self.publication_dir),
        }

    def _executable(self, name, contents):
        path = self.bin_dir / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def run_wrapper(self, **overrides):
        self.args_file.unlink(missing_ok=True)
        environment = self.environment | overrides
        result = subprocess.run(
            ["bash", str(self.wrapper), "bc_test.ini"],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        arguments = json.loads(self.args_file.read_text(encoding="utf-8")) if self.args_file.exists() else None
        return result, arguments

    @staticmethod
    def values_after(arguments, flag):
        return [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == flag]

    def test_default_launcher_uses_host_uid_and_private_mounts(self):
        result, arguments = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(arguments[0:3], ["run", "--user", "1234:2345"])
        self.assertEqual(
            self.values_after(arguments, "-v"),
            [
                f"{self.root / 'apps' / 'db'}:/apps/db",
                f"{self.profile_dir}:/var/lib/billcollector/profiles",
                f"{self.publication_dir}:/var/lib/billcollector/publication",
            ],
        )
        self.assertEqual(
            self.values_after(arguments, "-e"),
            ["BILLCOLLECTOR_PUBLICATION_ROOT=/var/lib/billcollector/publication"],
        )
        self.assertEqual(arguments[-5:], ["--rm", "billcollector:latest", "python3", "./BillCollector.py", "bc_test.ini"])

    def test_external_recipes_are_mounted_read_only_with_operator_approval(self):
        recipes = self.root / "external recipes"
        recipes.mkdir()
        approvals = self.root / "operator approvals.json"
        approvals.write_text("{}", encoding="utf-8")
        result, arguments = self.run_wrapper(
            BILLCOLLECTOR_HOST_RECIPES_DIR=str(recipes),
            BILLCOLLECTOR_HOST_RECIPE_APPROVALS_FILE=str(approvals),
            BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS="https://example.test",
            BILLCOLLECTOR_HOST_SHARED_GID="3456",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(arguments[0:3], ["run", "--user", "1234:3456"])
        self.assertIn(f"{recipes}:/recipes:ro", self.values_after(arguments, "-v"))
        self.assertIn(f"{approvals}:/approvals/recipe-approvals.json:ro", self.values_after(arguments, "-v"))
        self.assertIn("BILLCOLLECTOR_PUBLICATION_SHARED_GID=3456", self.values_after(arguments, "-e"))
        self.assertIn("BILLCOLLECTOR_RECIPES_DIR=/recipes", self.values_after(arguments, "-e"))
        self.assertIn(
            "BILLCOLLECTOR_RECIPE_APPROVALS_FILE=/approvals/recipe-approvals.json",
            self.values_after(arguments, "-e"),
        )
        self.assertIn(
            "BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS=https://example.test",
            self.values_after(arguments, "-e"),
        )

    def test_root_and_invalid_shared_gid_are_rejected_before_docker(self):
        self._executable(
            "id",
            "#!/bin/sh\ncase \"$1\" in -u) echo 0;; -g) echo 2345;; *) exit 2;; esac\n",
        )
        result, arguments = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to run", result.stderr)
        self.assertIsNone(arguments)

        self._executable(
            "id",
            "#!/bin/sh\ncase \"$1\" in -u) echo 1234;; -g) echo 2345;; *) exit 2;; esac\n",
        )
        result, arguments = self.run_wrapper(BILLCOLLECTOR_HOST_SHARED_GID="not-a-gid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("positive numeric GID", result.stderr)
        self.assertIsNone(arguments)

    def test_invalid_external_recipe_configuration_never_invokes_docker(self):
        recipes = self.root / "recipes"
        recipes.mkdir()
        approvals = self.root / "approvals.json"
        approvals.write_text("{}", encoding="utf-8")
        inside_approval = recipes / "approval.json"
        inside_approval.write_text("{}", encoding="utf-8")
        recipe_link = self.root / "recipe-link"
        recipe_link.symlink_to(recipes, target_is_directory=True)
        approval_link = self.root / "approval-link"
        approval_link.symlink_to(approvals)
        cases = {
            "missing approval": {"BILLCOLLECTOR_HOST_RECIPES_DIR": str(recipes)},
            "missing origins": {
                "BILLCOLLECTOR_HOST_RECIPES_DIR": str(recipes),
                "BILLCOLLECTOR_HOST_RECIPE_APPROVALS_FILE": str(approvals),
            },
            "approval inside recipes": {
                "BILLCOLLECTOR_HOST_RECIPES_DIR": str(recipes),
                "BILLCOLLECTOR_HOST_RECIPE_APPROVALS_FILE": str(inside_approval),
                "BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS": "https://example.test",
            },
            "symlinked recipes": {
                "BILLCOLLECTOR_HOST_RECIPES_DIR": str(recipe_link),
                "BILLCOLLECTOR_HOST_RECIPE_APPROVALS_FILE": str(approvals),
                "BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS": "https://example.test",
            },
            "symlinked approval": {
                "BILLCOLLECTOR_HOST_RECIPES_DIR": str(recipes),
                "BILLCOLLECTOR_HOST_RECIPE_APPROVALS_FILE": str(approval_link),
                "BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS": "https://example.test",
            },
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                result, arguments = self.run_wrapper(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(arguments, "docker must not run after validation fails")
