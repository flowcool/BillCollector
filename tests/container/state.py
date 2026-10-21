"""Synthetic daemon state control, executed only by check_container_compose.py."""

import json
import sys

import yaml

sys.path.insert(0, "/apps")

from billcollector import config, recipes  # noqa: E402
from billcollector.control import ControlClient  # noqa: E402
from billcollector.db import Database  # noqa: E402


def main():
    db = Database()
    try:
        if sys.argv[1] == "seed":
            assert db.list_tasks() == []
            assert not list(config.RECIPE_DIR.iterdir())
            recipe = {"services": [{"serviceName": "synthetic", "steps": [
                {"step": 1, "description": "Open synthetic portal", "methods": [
                    {"method": "goto", "arguments": [{"url": "http://fixture:8080/"}]}]},
                {"step": 2, "description": "Hold for synthetic browser verification", "methods": [
                    {"method": "wait_user", "arguments": [{"prompt": "Synthetic check"}]}]},
                {"step": 3, "description": "Download one synthetic PDF", "methods": [
                    {"method": "expect_download"}], "steps": [
                        {"step": 4, "methods": [{"method": "get_by_role", "arguments": [
                            {"role": "link"}, {"name": "Download invoice"}]}, {"method": "click"}]}]},
            ]}]}
            path = config.recipe_path_for("synthetic")
            assert recipes.save_recipe(path, yaml.safe_dump(recipe)) == (True, None)
            assert not (config.RECIPE_DIR / "recipe-pw-schema.yaml").exists()
            assert not recipes.validate_recipe("services: wrong")[0]
            assert db.create_task("synthetic", "user", recipe_file=str(path)) == 1
            db.set_setting("ntfy_topic", "")
            print("seeded synthetic task and validated empty recipe mount")
        elif sys.argv[1] == "resume":
            run = db.list_runs(1)[0]
            assert ControlClient(run["control_sock"]).request("resume")["ok"]
            print("resumed synthetic run")
        elif sys.argv[1] == "status":
            runs = db.list_runs(1)
            downloads = db.list_downloads(task_id=1)
            print(json.dumps({"tasks": len(db.list_tasks()), "runs": [dict(r) for r in runs],
                              "downloads": [dict(d) for d in downloads]}))
        elif sys.argv[1] == "persistence":
            assert recipes.load_recipe(config.recipe_path_for("synthetic"))[0]
            assert not (config.RECIPE_DIR / "recipe-pw-schema.yaml").exists()
            assert len(db.list_tasks()) == 1
            assert db.get_setting("ui_password_hash")
            assert list(config.PROFILE_ROOT.glob("synthetic_user/Default/*"))
            assert list(config.DOWNLOAD_DIR.glob("*.pdf"))
            print("persistent DB/password/profile/download/recipe verified")
        else:
            raise ValueError("Unknown synthetic check")
    finally:
        db.close()


if __name__ == "__main__":
    main()
