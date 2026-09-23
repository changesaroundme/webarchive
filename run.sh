#!/bin/bash
# Run archive_page.py inside Apple's container runtime (macOS 26, `container`).
#   ./run.sh --due                     what the daily job runs
#   ./run.sh --all --verbose           any archive_page.py arguments pass straight through
#   ./run.sh https://example.org/page --force
# Nothing runs natively: Chromium and Python live in the image; this checkout,
# the vault's Web Archive folder and the calendars checkout are bind-mounted.
# CAM_ARCHIVE_ROOT / CAM_CALENDARS_REPO in the environment override the two
# default folders.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE=cam-webarchive
ARCHIVE="${CAM_ARCHIVE_ROOT:-$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Archive - Changes Around Me/Tooling/Web Archive}"
CALENDARS="${CAM_CALENDARS_REPO:-$(cd "$HERE/../calendars" && pwd)}"
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

command -v container >/dev/null || { echo "Apple's container tool is not installed (https://github.com/apple/container/releases)"; exit 1; }
[ -d "$ARCHIVE" ] || { echo "archive folder not found: $ARCHIVE"; exit 1; }
[ -f "$CALENDARS/sources.yaml" ] || { echo "calendars checkout not found: $CALENDARS"; exit 1; }

# The container service runs only for the duration of the job: started here
# (idempotent), stopped at the end, so nothing lingers between runs. The Linux
# kernel it boots is a one-time install — `container system kernel set
# --recommended` — never done from here, because the prompt needs a terminal
# and this may be running under launchd.
if ! container system start --disable-kernel-install >/dev/null 2>&1; then
    echo "could not start the container service — if this is the first time, run:"
    echo "  container system kernel set --recommended"
    exit 1
fi
if ! container image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "building $IMAGE (first run only; a few minutes)"
    container build --tag "$IMAGE" --file "$HERE/Dockerfile" "$HERE"
fi
# Object storage (Cloudflare R2) is optional: when ~/.config/cam-webarchive/r2.env
# exists — KEY=value lines, mode 600, never in the repo — its settings go into the
# container and the batch modes mirror the archive after each run (r2sync.py).
R2_ENV="$HOME/.config/cam-webarchive/r2.env"
env_args=()
if [ -f "$R2_ENV" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        env_args+=(--env "$line")
    done < "$R2_ENV"
fi
set +e
# (bash 3.2 on macOS: an empty array is "unbound" under set -u, hence the idiom)
container run --rm --cpus 2 --memory 4g ${env_args[@]+"${env_args[@]}"} \
    --volume "$HERE:/app:ro" \
    --volume "$ARCHIVE:/archive" \
    --volume "$CALENDARS:/calendars" \
    "$IMAGE" "$@"
status=$?
# Stop the service only if nothing else is running (a second run.sh in another
# terminal, say) — `container list` shows running containers under a header line.
if [ "$(container list 2>/dev/null | wc -l | tr -d ' ')" -le 1 ]; then
    container system stop >/dev/null 2>&1 || true
fi
exit $status
