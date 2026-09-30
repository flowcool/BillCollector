#!/bin/bash
#
# Wrapper for python app BillCollector
# - 1st param: ini-file
# - 2nd param: debug [True/False]

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
HOST_DB_DIR="$SCRIPT_DIR/apps/db"
HOST_PROFILE_DIR="${BILLCOLLECTOR_HOST_PROFILE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/billcollector/profiles}"
HOST_PUBLICATION_DIR="${BILLCOLLECTOR_HOST_PUBLICATION_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/billcollector/publication}"
HOST_RUN_UID="$(id -u)"
HOST_RUN_GID="${BILLCOLLECTOR_HOST_SHARED_GID:-$(id -g)}"

if [[ "$HOST_RUN_UID" -eq 0 ]]; then
    echo "Refusing to run the Playwright container as root" >&2
    exit 1
fi
if [[ ! "$HOST_RUN_GID" =~ ^[1-9][0-9]*$ ]]; then
    echo "BILLCOLLECTOR_HOST_SHARED_GID must be a positive numeric GID" >&2
    exit 1
fi
if [[ ! -e "$HOST_DB_DIR" && ! -L "$HOST_DB_DIR" ]]; then
    install -d -m 0700 "$HOST_DB_DIR"
fi
if [[ ! -d "$HOST_DB_DIR" || ! -w "$HOST_DB_DIR" ]]; then
    echo "Database directory must exist and be writable by the host UID" >&2
    exit 1
fi

# Chromium profiles contain live sessions. Keep them outside the repository and
# mount them separately from the download and database directories.
if [[ -L "$HOST_PROFILE_DIR" ]]; then
    echo "Refusing a symlinked profile root" >&2
    exit 1
fi
install -d -m 0700 "$HOST_PROFILE_DIR"
if [[ -L "$HOST_PUBLICATION_DIR" ]]; then
    echo "Refusing a symlinked publication root" >&2
    exit 1
fi
install -d -m 0700 "$HOST_PUBLICATION_DIR"

if [[ -f .commit_id ]]; then
    COMMIT_ID=$(cat .commit_id)
    echo "Commit-ID: $COMMIT_ID"
fi

DOCKER_ARGS=(
    --user "$HOST_RUN_UID:$HOST_RUN_GID"
    -v "$HOST_DB_DIR:/apps/db"
    -v "$HOST_PROFILE_DIR:/var/lib/billcollector/profiles"
    -v "$HOST_PUBLICATION_DIR:/var/lib/billcollector/publication"
    -e BILLCOLLECTOR_PUBLICATION_ROOT=/var/lib/billcollector/publication
)
if [[ -n "${BILLCOLLECTOR_HOST_SHARED_GID:-}" ]]; then
    DOCKER_ARGS+=(-e "BILLCOLLECTOR_PUBLICATION_SHARED_GID=$HOST_RUN_GID")
fi

docker run "${DOCKER_ARGS[@]}" --rm billcollector:latest python3 ./BillCollector.py "$@"
