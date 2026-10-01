import os
import re
import logging
import sqlite3
import json
import sys
import time
from pathlib import Path

from datetime import datetime
from playwright.sync_api import Playwright, sync_playwright, Route, Request, Page
from helpers import *
from helpers.BillCollectorRecipeContract import (
    EXTERNAL_ORIGINS_ENV,
    RECIPE_DIR_ENV,
    RecipeContractError,
    SECRET_PLACEHOLDERS,
    https_origin,
    load_playwright_recipe,
    preflight_external_recipe,
)
from profile_store import locked_profile
from download_publication import DownloadPublisher, PublicationError

logger = logging.getLogger(__name__)

PUBLICATION_ROOT_ENV = "BILLCOLLECTOR_PUBLICATION_ROOT"
PUBLICATION_SHARED_GID_ENV = "BILLCOLLECTOR_PUBLICATION_SHARED_GID"
DOWNLOAD_SETTLE_MS = 500
# Upper bound on one download_all list. Above it the run fails instead of
# truncating; the recipe author narrows the selector.
MAX_DOWNLOAD_ALL_ITEMS = 200


def publication_context():
    """Require one persistent mount for state, private stage and consumer output."""
    configured = os.environ.get(PUBLICATION_ROOT_ENV, "")
    if not configured or not Path(configured).is_absolute():
        raise PublicationError(f"{PUBLICATION_ROOT_ENV} must be an absolute persistent directory")
    root = Path(configured)
    raw_gid = os.environ.get(PUBLICATION_SHARED_GID_ENV)
    if raw_gid is not None and (not raw_gid.isascii() or not raw_gid.isdecimal()
                                or int(raw_gid) <= 0):
        raise PublicationError(f"{PUBLICATION_SHARED_GID_ENV} must be a positive numeric GID")
    shared_gid = int(raw_gid) if raw_gid is not None else None
    return DownloadPublisher(root / "state", root / "output", root / "staging",
                             shared_gid=shared_gid)

def InitBrowser(p, bcs, profile_dir=None):
    """Initialize the browser with a persistent context to always open PDF externally"""
    if profile_dir is None:
        raise ValueError("A locked profile directory is required")
    browser = p.chromium.launch_persistent_context(
        headless=not bcs.dbg,
        user_data_dir=profile_dir,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
                 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    )
    return browser

class DatabaseManager:
    """Manages service-specific and run-specific tables."""
    
    def __init__(self, db_name):
        """Initialize the central tracking system."""
        self.db_name = db_name
        self.conn = sqlite3.connect(self.db_name)
        self.cursor = self.conn.cursor()

        # Central service tracking table
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS Service (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_name TEXT NOT NULL,
            run_number INTEGER NOT NULL,
            run_table TEXT NOT NULL,
            timestamp_start DATETIME,
            timestamp_end DATETIME,
            download_info JSON,
            result TEXT
        );
        """)
        self.conn.commit()

    def create_service_run_table(self, service_name):
        """Creates a new run table for a service and registers it in the Service table."""
        if not isinstance(service_name, str) or not re.fullmatch(r"[a-z0-9_]+", service_name):
            raise ValueError("Service name is not a safe database identifier")
        run_number = self.get_latest_run_number(service_name) + 1
        run_id = f"{service_name}_Run{run_number}"
        table_name = f"PageStatus_{run_id}"

        # Create the service run table
        # SQLite cannot bind identifiers; service and run number are constrained above.
        self.cursor.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, service_name TEXT NOT NULL, "
            "step_number INTEGER NOT NULL, locator_action JSON, "
            "interactive_elements JSON, result JSON)")

        # Get the local timestamp
        local_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Register the run in the Service table and set the start timestamp
        self.cursor.execute("""
        INSERT INTO Service (service_name, run_number, run_table, timestamp_start) 
        VALUES (?, ?, ?, ?)
        """, (service_name, run_number, table_name, local_time))

        self.conn.commit()
        return table_name

    def get_latest_run_number(self, service_name):
        """Finds the highest run number for a service."""
        self.cursor.execute("SELECT MAX(run_number) FROM Service WHERE service_name = ?", (service_name,))
        result = self.cursor.fetchone()[0]
        return result if result else 0  # Start at 0 if no previous runs exist

    def insert_page_status(self, table_name, page_state):
        """Stores a PageState entry in the correct service run table."""
        if not isinstance(table_name, str) or not re.fullmatch(
            r"PageStatus_[a-z0-9_]+_Run[1-9][0-9]*", table_name
        ):
            raise ValueError("Run table is not a safe database identifier")
        # SQLite cannot bind identifiers; the strict pattern rejects SQL syntax.
        self.cursor.execute(
            f"INSERT INTO {table_name} "  # nosec B608
            "(service_name, step_number, locator_action, interactive_elements, result) "
            "VALUES (?, ?, ?, ?, ?)", (
            page_state.service_name,
            page_state.step_number,
            json.dumps(page_state.locator_action),  # Stores locator and action information
            json.dumps(page_state.interactive_elements),  # Stores interactive elements
            json.dumps(page_state.error_status)  # Stores error message or None
        ))

        self.conn.commit()

    def finalize_service_run(self, service_name, run_table, download_info, result):
        """Updates the Service table with timestamp_end, download_info, and result at the end of a service run."""
        timestamp_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        download_info_json = json.dumps({"count": len(download_info)})

        self.cursor.execute("""
            UPDATE Service 
            SET timestamp_end = ?, download_info = ?, result = ?
            WHERE service_name = ? AND run_table = ?
        """, (timestamp_end, download_info_json, result, service_name, run_table))

        self.conn.commit()

    def close_connection(self):
        """Closes the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None
            self.cursor = None

class PageState:
    """Represents the state of a page at a given time."""
    def __init__(self, 
                 service_name: str,
                 step_number: int,
                 page: Page, 
                 ):
        
        self.service_name = service_name
        self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.step_number = step_number
#        self.aria_snapshot = page.accessibility.snapshot()
#        self.dom_status = page.content()
        # DOM snapshots can contain passwords, tokens, document names, and URLs.
        # Keep this field empty until a separately reviewed privacy contract exists.
        self.interactive_elements = []
        self.error_status = None
        self.locator_action = None
    
    def set_error(self, error_message: str):
        """Sets an error status"""
        self.error_status = error_message

    def set_locator_action(self, locator_action):
        """Sets the locator action with the help of transform_step_to_json()"""
        self.locator_action = locator_action

def retrieve_from_service_with_playwright(service, url, user, pwd, otp, debug, *, account_id,
                                          recipe_preflight=None):
    """Retrieve file from service - main function - Playwright variant    """

    bcs = ServiceObj(service=service, usr=user, pwd=pwd, otp=otp, dbg=debug, dld=DOWNLOAD_DIR,
                     account_id=account_id)
    on_debug_start_keyboard_listener(bcs)
    try:
        bcs.external_recipe = os.environ.get(RECIPE_DIR_ENV) is not None
        if bcs.external_recipe:
            bcs.yml, bcs.allowed_recipe_origins = (
                recipe_preflight if recipe_preflight is not None else
                preflight_external_recipe(service, account_id)
            )
        else:
            bcs.yml = load_playwright_recipe(service)
            bcs.allowed_recipe_origins = None
        
        file_downloaded = perform_actions(bcs)
        logger.info("Service %s finished; published: %d", service,
                    sum(item["result"] == "published" for item in file_downloaded))
        on_debug_stop_keyboard_listener(bcs)
        return True
    except Exception:
        logger.error("Service did not finish successfully")
        on_debug_stop_keyboard_listener(bcs)
        return False

def perform_actions(bcs):
    """Perform actions from YAML recipe on web elements - helper function for dispatching actions"""
    files_downloaded = []
    with sync_playwright() as p, locked_profile(CHROMIUM_PLAYWRIGHT_PROFILE, bcs.account_id) as profile_dir, publication_context() as publisher:
        try:
            bcs.publisher = publisher
            bcs.drv = InitBrowser(p, bcs, profile_dir)
            bcs.page = bcs.drv.new_page()
            
            # Parse the YAML structure
            services = bcs.yml.get('services', [])
            for service in services:
                service_name = service.get('serviceName')
                logger.info("Processing service: %s", service_name)
                bcs.db = DatabaseManager(DB_FILE)
                bcs.run_table = None
                service_files = []
                bcs.download_results = service_files
                run_result = "failure"
                try:
                    bcs.run_table = bcs.db.create_service_run_table(service_name)
                    steps = service.get('steps', [])
                    for step in sorted(steps, key=lambda x: x.get('step', 0)):
                        step_state = process_step(bcs, step)
                        bcs.db.insert_page_status(bcs.run_table, step_state)
                        if step_state.error_status:
                            raise RuntimeError(f"Step {step.get('step', 0)} failed")

                        if check_parameter_in_json(step_state.locator_action, {"action": "expect_download"}):
                            try:
                                with bcs.page.expect_download() as di:
                                    for nested_step in sorted(step["steps"], key=lambda x: x.get("step", 0)):
                                        nested_state = process_step(bcs, nested_step)
                                        bcs.db.insert_page_status(bcs.run_table, nested_state)
                                        if nested_state.error_status:
                                            raise RuntimeError(f"Nested step {nested_step.get('step', 0)} failed")
                                download = di.value
                                published = publisher.publish(download, service=bcs.service,
                                                              account=bcs.account_id)
                            except Exception:
                                raise RuntimeError("Download step failed") from None
                            service_files.append({"result": "published" if published else "duplicate"})
                    run_result = "success"
                    files_downloaded.extend(service_files)
                finally:
                    try:
                        if bcs.run_table is not None:
                            bcs.db.finalize_service_run(
                                service_name, bcs.run_table, service_files, run_result
                            )
                    finally:
                        bcs.db.close_connection()
                        bcs.db = None
        except Exception as error:
            logger.error("Playwright service run failed")
            if isinstance(error, RuntimeError) and str(error).startswith(("Step ", "Nested step ", "Download step ", "Browser initialization ")):
                raise
            raise RuntimeError("Playwright service run failed") from None
        finally:
            cleanup_error = None
            for resource in (getattr(bcs, "page", None), getattr(bcs, "drv", None)):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception:
                    cleanup_error = True
            if cleanup_error and not sys.exc_info()[0]:
                raise RuntimeError("Browser cleanup failed")
    return files_downloaded


def download_all_locator(bcs, locator, publisher, timeout_ms=30_000, results=None):
    """Click each match and require one download event per control.

    Outcomes are appended to ``results`` as they publish, so a caller that
    owns the list keeps the count of a partial run. After navigation the list
    is re-read until it matches the first snapshot or the timeout expires.
    The target handle is re-checked before the click: a changed order, count
    or identity fails before clicking another item.
    """
    page = bcs.page
    results = [] if results is None else results
    locator.first.wait_for(state="attached", timeout=timeout_ms)
    read_identity = "e => [e.getAttribute('href'), e.textContent]"

    def snapshot():
        # One protocol call for the whole list, no handle per element.
        return [tuple(entry) for entry in locator.evaluate_all(
            f"elements => elements.map({read_identity})")]

    identities = snapshot()
    count = len(identities)
    if not count:
        raise RuntimeError("Download list is empty")
    if count > MAX_DOWNLOAD_ALL_ITEMS:
        raise RuntimeError(f"Download list has {count} items; the limit is {MAX_DOWNLOAD_ALL_ITEMS}")
    if len(set(identities)) != count:
        raise RuntimeError("Download list contains ambiguous duplicate controls")
    for index in range(count):
        if index:
            deadline = time.monotonic() + timeout_ms / 1000
            while snapshot() != identities:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Download list changed after {index} of {count} items")
                page.wait_for_timeout(50)
        # Unlike locator.nth(index) at click time, a checked ElementHandle
        # cannot silently retarget a different row if the DOM reorders.
        target = locator.nth(index).element_handle(timeout=timeout_ms)
        try:
            if tuple(target.evaluate(read_identity)) != identities[index]:
                raise RuntimeError(f"Download list changed after {index} of {count} items")
            download_one(bcs, target, publisher, index, count, timeout_ms, results)
        finally:
            target.dispose()
    return results


def download_one(bcs, target, publisher, index, count, timeout_ms, results):
    """Click one checked control, publish its single download, restore the list."""
    page = bcs.page
    listing_url = page.url
    downloads = []
    popups = []

    def on_download(download):
        downloads.append(download)

    def on_page(new_page):
        popups.append(new_page)
        new_page.on("download", on_download)

    page.on("download", on_download)
    bcs.drv.on("page", on_page)
    try:
        target.click(timeout=timeout_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        while not downloads and time.monotonic() < deadline:
            page.wait_for_timeout(min(50, max(1, int((deadline - time.monotonic()) * 1000))))
        # One control has one file. Observe a short fixed interval after
        # the first event to reject a second event from the same click.
        settle_deadline = time.monotonic() + DOWNLOAD_SETTLE_MS / 1000
        while len(downloads) == 1 and time.monotonic() < settle_deadline:
            page.wait_for_timeout(min(50, max(1, int((settle_deadline - time.monotonic()) * 1000))))
        if len(downloads) != 1:
            raise RuntimeError(
                f"Download item {index + 1} of {count} produced {len(downloads)} files; expected one"
            )
        published = publisher.publish(downloads[0], service=bcs.service, account=bcs.account_id)
        results.append({"result": "published" if published else "duplicate"})
    finally:
        page.remove_listener("download", on_download)
        bcs.drv.remove_listener("page", on_page)
        for popup in popups:
            if not popup.is_closed():
                popup.close()
        if page.url != listing_url:
            page.go_back(wait_until="domcontentloaded", timeout=timeout_ms)
            if page.url != listing_url:
                raise RuntimeError("Download listing could not be restored")


def process_step(bcs, step):
    """ Processes a step by executing a chain of methods """

    def process_argument(arg, bcs):
        """ Processes a single argument:
        - For a string that exactly matches a placeholder (e.g. "{{PASSWORD}}"), return the actual value.
        - For a dictionary, replace any placeholder values (leaving the dict intact).
        - For a list, process its elements recursively.
        - Otherwise, return the argument unchanged.
        """
        if isinstance(arg, str):
            return VARIABLE_MAP[arg](bcs) if arg in VARIABLE_MAP else arg
        elif isinstance(arg, dict):
            new_arg = {}
            for key, value in arg.items():
                new_arg[key] = VARIABLE_MAP[value](bcs) if isinstance(value, str) and value in VARIABLE_MAP else value
            return new_arg
        elif isinstance(arg, list):
            return [process_argument(item, bcs) for item in arg]
        else:
            return arg

    def transform_step_to_json(step):
        """Transforms a step object into JSON with locators and actions."""
        
        if not isinstance(step, dict) or "methods" not in step:
            raise ValueError("Invalid step format: Expected a dictionary with a 'methods' key.")

        # Persist only method names. Recipe descriptions and argument values may
        # contain credentials or private portal data, even before interpolation.
        transformed_step = {"locators": [], "actions": []}

        for method_entry in step["methods"]:
            method_name = method_entry.get("method")
            if not method_name:
                continue  # Skip invalid methods

            # Categorize as locator or action (just a simple heuristic based on action names)
            if method_name in ["click", "fill", "expect_download", "download_all", "goto", "close", "content_frame", "first"]:
                transformed_step["actions"].append({"action": method_name})
            else: # all other methods are considered locators
                transformed_step["locators"].append({"locator": method_name})

        return json.dumps(transformed_step, separators=(",", ":"))


    if not isinstance(step, dict) or "methods" not in step:
        raise ValueError("Invalid step format: Expected a dictionary with a 'methods' key.")

    step_number = step.get("step", 0)
    logger.debug(f"Processing Step {step_number}")
    step_results = {}
    chain_mapping = []  # This will accumulate our mapping entries.
    previous_result = bcs.page  # Starting object.

    page_state = PageState(bcs.service, step_number, bcs.page)
    page_state.set_locator_action(transform_step_to_json(step))

    for method_entry in step["methods"]:
        method_name = method_entry.get("method")
        arguments = method_entry.get("arguments", [])
        if getattr(bcs, "external_recipe", False):
            secret_values = [value for argument in arguments for value in argument.values()
                             if isinstance(value, str) and value in SECRET_PLACEHOLDERS]
            if secret_values:
                if method_name != "fill" or arguments != [{"value": secret_values[0]}]:
                    raise RecipeContractError("external credentials are only allowed in fill(value)")
                if https_origin(bcs.page.url) not in bcs.allowed_recipe_origins:
                    raise RecipeContractError(
                        f"credential fill blocked: page origin is not in {EXTERNAL_ORIGINS_ENV}"
                    )
        processed_args = [process_argument(arg, bcs) for arg in arguments]

        if not method_name:
            page_state.set_error({"error": "Method entry missing 'method' key"})
            return page_state

        # Special handling for "expect_download".
        if method_name == "expect_download":
            if "steps" in step and step["steps"]:
                page_state.set_error(step_results)
            else:
                page_state.set_error("error: No nested steps provided for 'expect_download'.")
            break
        # Standard handling of methods
        else:
            method_executor = getattr(previous_result, method_name, None)
            try:
                if method_name == "download_all":
                    timeout_ms = processed_args[0]["timeout_ms"] if processed_args else 30_000
                    download_all_locator(bcs, previous_result, bcs.publisher, timeout_ms,
                                         bcs.download_results)
                    break
                if method_executor is None:
                    raise AttributeError(f"Method '{method_name}' not found on {type(previous_result).__name__}.")
                if callable(method_executor):
                    if processed_args and all(isinstance(arg, dict) for arg in processed_args):
                        kwargs = {}
                        for d in processed_args:
                            kwargs.update(d)
                        result = method_executor(**kwargs)          # Call the method with keyword arguments.
                    else:
                        result = method_executor(*processed_args)   # Call the method with positional arguments.
                    previous_result = result if result is not None else previous_result
                else:
                    if processed_args:
                        raise TypeError(f"Attribute '{method_name}' is not callable but arguments were provided: {processed_args}")
                    previous_result = method_executor               # Get the value of a property.
            except Exception:
                step_results.setdefault("error", []).append({"method": method_name, "message": "method failed"})
                page_state.set_error(step_results)
                return page_state
            page_state.set_error(step_results)

    return page_state

def check_parameter_in_json(json_str, param_dict):
    """Checks if a given key-value pair exists in the JSON structure."""
    
    try:
        data = json.loads(json_str)  # Parse JSON string into a dictionary
        
        # Extract key-value pair from the parameter dictionary
        param_key, param_value = next(iter(param_dict.items()))
        
        # Recursively search for the key-value pair
        def recursive_search(obj):
            if isinstance(obj, dict):
                if param_key in obj and obj[param_key] == param_value:
                    return True
                return any(recursive_search(value) for value in obj.values())
            elif isinstance(obj, list):
                return any(recursive_search(item) for item in obj)
            return False

        return recursive_search(data)

    except json.JSONDecodeError:
        return False  # Return False if the JSON string is invalid
