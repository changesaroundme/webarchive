#!/bin/bash
# The daily archive job, as a launchd user agent that runs ./run.sh --due
# (everything else happens inside the container).
#   ./install.sh            install / refresh (builds the image if missing)
#   ./install.sh remove     take it all out again — see the list at the bottom
#   ./install.sh status     is it loaded, when did it last run
# Paths come from where this checkout lives; nothing personal is committed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.changesaroundme.webarchive
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/cam-webarchive.log"
DOMAIN="gui/$(id -u)"
IMAGE=cam-webarchive
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

case "${1:-install}" in
  remove)
    # Everything this job put on the Mac, in order. The container runtime
    # itself (Apple's pkg) is left alone: uninstall it with the
    # uninstall-container.sh script that ships with it, if you want it gone.
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && echo "unloaded $LABEL" || true
    rm -f "$PLIST" && echo "removed $PLIST"
    rm -f "$LOG" && echo "removed $LOG"
    if command -v container >/dev/null; then
        container image delete "$IMAGE" 2>/dev/null && echo "deleted image $IMAGE" || true
        container builder delete 2>/dev/null && echo "deleted the build helper" || true
        container system stop 2>/dev/null && echo "stopped the container service" || true
    fi
    echo "done. Not touched (yours to remove, see README › Install and uninstall):"
    [ -f "$HOME/.config/cam-webarchive/r2.env" ] && echo "  - R2 credentials: rm -r ~/.config/cam-webarchive" || true
    echo "  - iCloud Drive permission for container-runtime-linux (System Settings › Privacy & Security › Files and Folders)"
    echo "  - Keep Downloaded on the Web Archive folder (Finder)"
    echo "  - Apple's container runtime: brew uninstall container   (only if nothing else uses it)"
    echo "  - this checkout"
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && echo "loaded: $LABEL (daily 6:30am)" || echo "not loaded"
    [ -f "$LOG" ] && { echo "last log lines:"; tail -n 5 "$LOG"; } || echo "no log yet"
    ;;
  install)
    command -v container >/dev/null || { echo "install Apple's container tool first: https://github.com/apple/container/releases"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$HERE/run.sh</string>
    <string>--due</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>30</integer></dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
PLIST_EOF
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "installed $LABEL — runs ./run.sh --due daily at 6:30am (at next wake if asleep); log: $LOG"
    echo "run it now:  launchctl kickstart -k $DOMAIN/$LABEL     (or ./run.sh --due in a terminal)"
    echo "remove:      $0 remove"
    ;;
  *)
    echo "usage: $0 [install|remove|status]"; exit 2 ;;
esac
