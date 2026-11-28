#!/bin/bash
# ABOUTME: Gate-aware managed kiosk wrapper (`watch` / `open`) that stands down during maintenance.
# ABOUTME: Holds shared flock on ROOT/locks/mutation.lock while nudging the service, never leaking FDs.
set -u

COURSE_URL="${SIGINT101_COURSE_URL:-http://127.0.0.1:8008}"
PROFILE="$HOME/.config/chromium-kiosk"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
FLAG="$RUNTIME/sigint101-kiosk-exited"
UPDATER_ROOT="${SIGINT101_UPDATER_ROOT:-$HOME/.local/state/sigint101-updater}"
MUTATION_LOCK="$UPDATER_ROOT/locks/mutation.lock"
EXIT_GATE_BUSY=75

usage() {
  echo "usage: $(basename "$0") watch|open" >&2
}

kiosk_alive() {
  pgrep -f "[u]ser-data-dir=$PROFILE" >/dev/null
}

# Try to acquire the shared mutation gate on fd 8 without waiting.
# Returns 0 if acquired (caller must release with `exec 8>&-`), or 1 if maintenance holds it.
try_shared_gate() {
  [ -f "$MUTATION_LOCK" ] && [ ! -L "$MUTATION_LOCK" ] || return 1
  exec 8<"$MUTATION_LOCK" || return 1
  if ! flock -s -n 8; then
    exec 8>&-
    return 1
  fi
  return 0
}

wait_for_server() {
  if ! curl -sf "$COURSE_URL/api/hardware" >/dev/null 2>&1; then
    systemctl --user start sigint101.service 2>/dev/null || true
  fi
  for _ in $(seq 1 60); do
    curl -sf "$COURSE_URL/api/hardware" >/dev/null 2>&1 && break
    sleep 1
  done
}

launch() {
  rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonCookie" "$PROFILE/SingletonSocket" 2>/dev/null
  # Close both the watchdog lock (fd 9) and the shared gate (fd 8) so Chromium inherits neither.
  chromium --kiosk --start-fullscreen --ozone-platform=wayland --user-data-dir="$PROFILE" \
    --app="$COURSE_URL" --window-position=0,0 --window-size=800,480 \
    --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble \
    --disable-features=TranslateUI --disk-cache-size=1 --password-store=basic 8>&- 9>&- &
}

open_course() {
  if ! try_shared_gate; then
    echo "sigint101-course: maintenance in progress (mutation gate busy); retry shortly" >&2
    return "$EXIT_GATE_BUSY"
  fi
  rm -f "$FLAG"
  wait_for_server
  kiosk_alive || launch
  exec 8>&-
}

watch() {
  if [ -z "${WAYLAND_DISPLAY:-}" ] || [ ! -S "$RUNTIME/$WAYLAND_DISPLAY" ]; then
    echo "sigint101-course: no Wayland session to put the course on; not watching" >&2
    exit 1
  fi
  exec 9>"$RUNTIME/sigint101-kiosk-watch.lock"
  flock -n 9 || exit 0
  while [ -S "$RUNTIME/$WAYLAND_DISPLAY" ]; do
    if try_shared_gate; then
      if ! kiosk_alive && [ ! -e "$FLAG" ]; then
        wait_for_server
        kiosk_alive || launch
      fi
      exec 8>&-
    fi
    if [ "${SIGINT101_WATCH_ONCE:-0}" = "1" ]; then
      break
    fi
    sleep 10
  done
}

case "${1:-}" in
  watch) watch ;;
  open) open_course ;;
  *) usage; exit 2 ;;
esac
