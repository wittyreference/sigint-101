#!/bin/bash
# ABOUTME: The course full-screen on the Pi desktop. `watch` (XDG autostart) reopens it after a crash,
# ABOUTME: never after Exit; `open` (desktop icon, menu) clears Exit and opens it. install.sh wires both.
set -u
COURSE_URL="http://127.0.0.1:8008"
# A DEDICATED profile dir avoids the shared-profile "in use" lock, and is how the kiosk browser is
# told apart from any Chromium the learner opens on the desktop (sigint101/kiosk.py keys on it too).
PROFILE="$HOME/.config/chromium-kiosk"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
# Exit SIGINT 101 writes this flag before it closes the browser. XDG_RUNTIME_DIR is a tmpfs, so a
# reboot clears it and the box boots back into the course.
FLAG="$RUNTIME/sigint101-kiosk-exited"

usage() {
  echo "usage: $(basename "$0") watch|open" >&2
}

# The lesson server is the sigint101 user service. Nudge it if it is not up, then wait for it so
# Chromium never opens on a connection-refused page.
wait_for_server() {
  if ! curl -sf "$COURSE_URL/api/hardware" >/dev/null 2>&1; then
    systemctl --user start sigint101.service 2>/dev/null || true
  fi
  for _ in $(seq 1 60); do
    curl -sf "$COURSE_URL/api/hardware" >/dev/null 2>&1 && break
    sleep 1
  done
}

kiosk_alive() {
  pgrep -f "[u]ser-data-dir=$PROFILE" >/dev/null
}

# Only ever called when no kiosk browser is alive, so a stale singleton from a crash is safe to
# clear and the "profile in use" dialog never blocks the panel.
# --disk-cache-size=1 keeps a stale stylesheet from outliving an update. --password-store=basic
# stops Chromium asking the desktop keyring, which otherwise covers the panel with a
# "choose password for new keyring" dialog that a keyboard-free kiosk can never dismiss.
# The browser must not inherit the watchdog's lock (fd 9), or a second watchdog could never start.
launch() {
  rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonCookie" "$PROFILE/SingletonSocket" 2>/dev/null
  chromium --kiosk --ozone-platform=wayland --user-data-dir="$PROFILE" --app="$COURSE_URL" \
    --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble \
    --disable-features=TranslateUI --disk-cache-size=1 --password-store=basic 9>&- &
}

# The desktop icon and the menu entry: the learner asked for the course back, so the exit is over.
open_course() {
  rm -f "$FLAG"
  wait_for_server
  kiosk_alive || launch
}

# XDG autostart, once per desktop session. It must check, not wait: the /usr/bin/chromium wrapper
# returns before the browser exits, and a bare relaunch loop hands each new launch to the running
# browser as yet another window (it ran nomad-b to load 20 in minutes).
watch() {
  if [ -z "${WAYLAND_DISPLAY:-}" ] || [ ! -S "$RUNTIME/$WAYLAND_DISPLAY" ]; then
    echo "sigint101-course: no Wayland session to put the course on; not watching" >&2
    exit 1
  fi
  # One watchdog per session, however often autostart runs.
  exec 9>"$RUNTIME/sigint101-kiosk-watch.lock"
  flock -n 9 || exit 0
  wait_for_server
  # The loop ends with the session: an orphaned watchdog must not outlive its compositor.
  while [ -S "$RUNTIME/$WAYLAND_DISPLAY" ]; do
    # Browser first, then the flag. Exit writes the flag before it closes the browser, so once no
    # browser is alive because of an Exit, the flag is already there to be seen.
    if ! kiosk_alive && [ ! -e "$FLAG" ]; then
      launch
    fi
    sleep 10
  done
}

case "${1:-}" in
  watch) watch ;;
  open) open_course ;;
  *) usage; exit 2 ;;
esac
