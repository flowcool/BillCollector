#!/bin/bash
#
# Wrapper for python app BillCollector
# - 1st param: ini-file
# - 2nd param: debug [True/False]

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
HOST_PROFILE_DIR="${BILLCOLLECTOR_HOST_PROFILE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/billcollector/profiles}"
HOST_PUBLICATION_DIR="${BILLCOLLECTOR_HOST_PUBLICATION_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/billcollector/publication}"

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

docker run -v "$SCRIPT_DIR/apps/db:/apps/db" \
        -v "$HOST_PROFILE_DIR:/var/lib/billcollector/profiles" \
        -v "$HOST_PUBLICATION_DIR:/var/lib/billcollector/publication" \
        -e BILLCOLLECTOR_PUBLICATION_ROOT=/var/lib/billcollector/publication \
        --rm billcollector:latest \
        python3 ./BillCollector.py "$@"
