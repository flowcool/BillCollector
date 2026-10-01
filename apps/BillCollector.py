# BillCollector
# Retrieval of Documents from Web Services

import os
import sys
import ipaddress
import socket
from urllib.parse import quote, urlparse
from dotenv import load_dotenv
import re
import logging
import requests
import json
import configparser
from flatten_json import flatten

from BillCollectorServices_pw import retrieve_from_service_with_playwright
from helpers.BillCollectorRecipeContract import RECIPE_DIR_ENV, RecipeContractError, preflight_external_recipe
from helpers import *

logger = logging.getLogger(__name__)

LOCAL_API_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "127.0.0.0/8", "fc00::/7", "::1/128",
))

# Function to extract strings before and within brackets
def extract_strings(line):
    match = re.search(r'([^\[]*)\[(.*?)\]', line)
    if match:
        before_bracket = match.group(1).strip()
        within_bracket = match.group(2).split(', ')
        return before_bracket, within_bracket
    return line.strip(), []

def pinned_api_url(url):
    """Resolve and return an HTTP URL bound to a vetted local IP address."""
    try:
        parsed = urlparse(url)
        if (parsed.scheme != "http" or not parsed.hostname or not parsed.port
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError("invalid local API URL")
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port)}
        if not addresses or not all(
            any(ipaddress.ip_address(address) in network for network in LOCAL_API_NETWORKS)
            for address in addresses
        ):
            raise ValueError("non-local API address")
        chosen = sorted(addresses)[0]
        host = f"[{chosen}]" if ":" in chosen else chosen
        return parsed._replace(netloc=f"{host}:{parsed.port}").geturl(), parsed.netloc
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("BW_API_URL must resolve only to local HTTP addresses") from None


def is_api_url_local(url):
    try:
        pinned_api_url(url)
        return True
    except ValueError:
        return False


def private_api_request(method, url, **kwargs):
    """Disable ambient proxies and pin the actual connection to a checked IP."""
    pinned_url, original_host = pinned_api_url(url)
    host = os.getenv("BW_API_HOST", "").strip() or original_host
    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, pinned_url, headers={"Host": host},
                               timeout=10, allow_redirects=False, **kwargs)


# Get web content
def get_json(url):
    try:
        response = private_api_request("GET", url)
        if response.status_code not in (200, 201):
            logger.error("Vault request failed (%s)", response.status_code)
            return None
        response.raise_for_status()
        return response.text
    except (requests.exceptions.RequestException, ValueError) as error:
        if isinstance(error, ValueError):
            logger.error("Invalid local Bitwarden API URL")
            return None
        status = error.response.status_code if error.response is not None else "network"
        logger.error("Vault request failed (%s)", status)
        return None

# Check Bitwarden API status
def bitwarden_api_check_status(url):
    content = get_json(f"{url}/status")
    if content is None:
        return False, None
    if not is_json_property_value(content, "success", True): return False, None
    else: 
        if not is_json_property_value(content, "data_template_status", "unlocked"): return True, "locked"
        return True, "unlocked"

def is_json_property_value(content, prop, val):
    if not is_json_valid(content): return False
    if not is_string_valid(prop): return False
    data = flatten(json.loads(content))
    result=data.get(prop)
    if (result) == val: return True
    else: return False

def is_json_valid(content):
    try:
        json.loads(content)
        return True
    except ValueError as e:
        logger.error(f"Error: Invalid JSON {e}")
        return False

def is_string_valid(string):
    try:
        if not isinstance(string, str) or not string or string.isspace():
            raise ValueError("Invalid string")
        return True
    except ValueError as e:
        logger.error(f"Error: {e}")
        return False
    
def post_json(url, payload):
    try:
        response = private_api_request("POST", url, json=payload)
    except (requests.exceptions.RequestException, ValueError):
        logger.error("Vault request failed")
        return False
    if response.status_code == 201 or response.status_code == 200:
        response.raise_for_status()
        logger.info("Successfully posted!")
        return json.dumps(response.json())
    else:
        logger.error("Vault request failed: HTTP %s", response.status_code)
        return False

def get_json_property_value(content, prop):
    data = flatten(json.loads(content))
    result=data.get(prop)
    return result


def get_item_by_name(api, name):
    """Select exactly one vault item by name, never a fuzzy search result."""
    content = get_json(f"{api}/list/object/items?search={quote(name, safe='')}")
    if content is None:
        raise RuntimeError("Bitwarden item search failed")
    try:
        items = json.loads(content)["data"]["data"]
        matches = [item for item in items if item.get("name") == name]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise RuntimeError("Invalid Bitwarden item search response") from None
    if len(matches) != 1:
        raise RuntimeError(f"Expected one exact Bitwarden item match, found {len(matches)}")
    item = matches[0]
    if not isinstance(item.get("id"), str) or not item["id"]:
        raise RuntimeError("Bitwarden item lacks a stable ID")
    return item


def get_totp(api, item_id):
    """Use the immutable item ID; Bitwarden reports absent TOTP as HTTP 400."""
    try:
        response = private_api_request("GET", f"{api}/object/totp/{quote(item_id, safe='')}")
        if response.status_code == 400:
            if "no totp" in response.text.lower():
                return None
            raise RuntimeError("Bitwarden TOTP request failed (400)")
        if response.status_code != 200:
            raise RuntimeError(f"Bitwarden TOTP request failed ({response.status_code})")
        response.raise_for_status()
        return response.json()["data"]["data"]
    except requests.exceptions.RequestException as error:
        status = error.response.status_code if error.response is not None else "network"
        raise RuntimeError(f"Bitwarden TOTP request failed ({status})") from None
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Invalid Bitwarden TOTP response") from None

class defs:
    def __init__(self, vault, api, fname=INI_DEFAULT_FILE, debug=False):
        self.vault = vault
        self.api = api
        self.fname = fname
        self.debug = debug

def WebRetriDoc(self, type=None, service=None):

    # The vault may be Bitwarden Cloud; only the local bw serve API is restricted.
    if not is_api_url_local(self.api):
        logger.error("BW_API_URL must resolve only to local HTTP addresses")
        sys.exit(1)

    # Check if Bitarden API at <bw_api_url> responds with success=true
    ret, status = bitwarden_api_check_status(self.api)
    if not ret or not status == "unlocked":
        sys.exit(1)
    logger.info(status)

    # Sync database
    ret = post_json(f"{self.api}/sync", None)
    if not ret:
        sys.exit(1)
    if not is_json_property_value(ret, "success", True):
        sys.exit(1)
    logger.info("Vault is sync'd successfully.")

    #################
    # Loop over Web Services
    script = configparser.ConfigParser()
    try:
        script.read(self.fname, encoding="utf-8")
    except configparser.Error as e:
        logger.error(f"Error: Reading ini-script: {e}")
        sys.exit(1)
    
    matched = False
    failed = False
    for automation_library in script.sections():
        if automation_library == None: break
        if type != None and automation_library.lower() != type.lower(): continue

        for servicename, users_list in script[automation_library].items():
            if service is not None and servicename.lower() != service.lower():
                continue
            matched = True
            users = []
            if not servicename: break    
            if users_list:
                users = [user.strip() for user in users_list.split(",")]
            else:
                users.insert(0, "")

            # handle service variant with list of users in array
            for user in users:
                service_user = f"{servicename} {user}".strip()
                logger.info("Service %s started", servicename)

                # Reject unapproved recipe bytes/account bindings before any
                # credential or TOTP lookup. Pass the frozen parsed recipe on.
                recipe_preflight = None
                if automation_library.lower() == "playwright" and os.environ.get(RECIPE_DIR_ENV) is not None:
                    try:
                        recipe_preflight = preflight_external_recipe(servicename, service_user)
                    except RecipeContractError:
                        logger.error("External recipe is not approved for service %s", servicename)
                        failed = True
                        continue

                # Retrieve credentials
                try:
                    item = get_item_by_name(self.api, service_user)
                    if recipe_preflight is not None and item["id"] != recipe_preflight[2]:
                        raise RuntimeError("Bitwarden item ID differs from operator approval")
                    login = item.get("login") or {}
                    username = login.get("username")
                    passsword = login.get("password")
                    uris = login.get("uris") or []
                    uri = uris[0].get("uri") if uris else None
                    if not username or not passsword or not uri:
                        raise RuntimeError("Bitwarden item lacks login fields or URI")
                    totp = get_totp(self.api, item["id"])
                except (RuntimeError, AttributeError, TypeError, IndexError) as error:
                    logger.error("Credential lookup failed for service %s: %s", servicename, error)
                    failed = True
                    continue

                # Download Documents with the help of the appropriate automation library
                if automation_library.lower() == "playwright":
                    if not retrieve_from_service_with_playwright(
                        servicename, uri, username, passsword, totp, self.debug,
                        account_id=item["id"], recipe_preflight=recipe_preflight,
                    ):
                        failed = True
    #
    #################

    if not matched:
        logger.warning(f"No service matched (library={type!r}, service={service!r}) in {self.fname}; nothing was done.")
        return False
    return not failed


def playwright_exit_code(config, service=None):
    """Return the CLI status for a selected Playwright run."""
    return 0 if WebRetriDoc(config, "playwright", service) else 1


if __name__ == "__main__":
    sys.stdout = sys.__stdout__

    load_dotenv()

    # Optional per-service filter: --service <NAME>. Extracted before the
    # positional handling, so the existing <ini> [debug] call (incl. cron)
    # stays fully compatible.
    service = None
    if "--service" in sys.argv[1:]:
        if sys.argv[-1] == "--service":
            logger.error("Error: --service requires a value.")
            sys.exit(1)
        idx = sys.argv.index("--service")
        service = sys.argv[idx + 1]
        del sys.argv[idx:idx + 2]

    bc = defs(
        os.getenv("VAULT_HOST"), 
        os.getenv("BW_API_URL")) #, 

    if is_debug_session():
        # Debugging
        logger.info("Executed in debugger. Debug mode enabled.")
        bc.fname = INI_DEFAULT_TEST_FILE
        bc.debug = True
    else:
        # Command line handling
        if len(sys.argv) < 2 or len(sys.argv) > 3:
            logger.error(" Usage: python3 BillCollector.py <ini-filename> [\"debug\"] [--service <NAME>]")
            sys.exit(1)
        if os.path.isfile(sys.argv[1]) == False:
            logger.error(f"File {sys.argv[1]} not found.")
            sys.exit(1)
        if len(sys.argv) == 2:
            bc.debug = False
        else:
            bc.debug = True
            logger.info("Debug mode enabled.")
        bc.fname = sys.argv[1]

    setup_logging(LOG_DEFAULT_FILE, debug=bc.debug)

    sys.exit(playwright_exit_code(bc, service))
