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
if [[ -L "$HOST_DB_DIR" ]]; then
    echo "Refusing a symlinked database directory" >&2
    exit 1
fi
if [[ ! -e "$HOST_DB_DIR" ]]; then
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

# External recipes are opt-in. The recipe directory and the operator approval
# file are mounted read-only from separate host locations (see
# doc/playwright-external-recipes.md); the origin ceiling must be set explicitly.
if [[ -n "${BILLCOLLECTOR_HOST_RECIPES_DIR:-}" ]]; then
    HOST_APPROVALS="${BILLCOLLECTOR_HOST_RECIPE_APPROVALS_FILE:-}"
    if [[ -L "$BILLCOLLECTOR_HOST_RECIPES_DIR" || ! -d "$BILLCOLLECTOR_HOST_RECIPES_DIR" ]]; then
        echo "BILLCOLLECTOR_HOST_RECIPES_DIR must be a real directory" >&2
        exit 1
    fi
    if [[ -z "$HOST_APPROVALS" || -L "$HOST_APPROVALS" || ! -f "$HOST_APPROVALS" ]]; then
        echo "BILLCOLLECTOR_HOST_RECIPE_APPROVALS_FILE must be a regular file (not a symlink)" >&2
        exit 1
    fi
    if [[ -z "${BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS:-}" ]]; then
        echo "BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS is required with external recipes" >&2
        exit 1
    fi
    HOST_RECIPES_REAL="$(readlink -f "$BILLCOLLECTOR_HOST_RECIPES_DIR")"
    HOST_APPROVALS_REAL="$(readlink -f "$HOST_APPROVALS")"
    if [[ "$HOST_APPROVALS_REAL" == "$HOST_RECIPES_REAL"/* ]]; then
        echo "The approval file must be outside the recipe directory" >&2
        exit 1
    fi
    DOCKER_ARGS+=(
        -v "$HOST_RECIPES_REAL:/recipes:ro"
        -v "$HOST_APPROVALS_REAL:/approvals/recipe-approvals.json:ro"
        -e BILLCOLLECTOR_RECIPES_DIR=/recipes
        -e BILLCOLLECTOR_RECIPE_APPROVALS_FILE=/approvals/recipe-approvals.json
        -e "BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS=$BILLCOLLECTOR_EXTERNAL_RECIPE_ORIGINS"
    )
fi

docker run "${DOCKER_ARGS[@]}" --rm billcollector:latest python3 ./BillCollector.py "$@"
