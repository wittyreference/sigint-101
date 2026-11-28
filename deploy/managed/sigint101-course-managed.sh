#!/bin/bash
# ABOUTME: Gate-aware managed kiosk wrapper (`watch` / `open`) built directly on start-kiosk.sh.
# ABOUTME: Holds shared flock on ROOT/locks/mutation.lock while starting, preserving all kiosk fixes.
set -u
# >>> managed-gate >>>
UPDATER_ROOT="${SIGINT101_UPDATER_ROOT:-$HOME/.local/state/sigint101-updater}"
MUTATION_LOCK="$UPDATER_ROOT/locks/mutation.lock"
EXIT_GATE_BUSY=75

# Try to acquire the shared mutation gate on fd 8 without waiting.
# Returns 0 if acquired (caller releases with `exec 8>&-`), or 1 if maintenance holds it.
try_shared_gate() {
  [ -f "$MUTATION_LOCK" ] && [ ! -L "$MUTATION_LOCK" ] || return 1
  exec 8<"$MUTATION_LOCK" || return 1
  if ! flock -s -n 8; then
    exec 8>&-
    return 1
  fi
  return 0
}
# <<< managed-gate <<<
COURSE_URL="http://127.0.0.1:8008"
# >>> managed-gate >>>
COURSE_URL="${SIGINT101_COURSE_URL:-$COURSE_URL}"
# <<< managed-gate <<<
# A DEDICATED profile dir avoids the shared-profile "in use" lock, and is how the kiosk browser is
# told apart from any Chromium the learner opens on the desktop (sigint101/kiosk.py keys on it too).
PROFILE="$HOME/.config/chromium-kiosk"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
# Exit SIGINT 101 writes this flag before it closes the browser. XDG_RUNTIME_DIR is a tmpfs, so a
# reboot clears it and the box boots back into the course.
FLAG="$RUNTIME/sigint101-kiosk-exited"
# The watchdog holds a flock on this for as long as it runs; install.sh finds it by it.
LOCK="$RUNTIME/sigint101-kiosk-watch.lock"

usage() {
  echo "usage: $(basename "$0") watch|open" >&2
}

# The lesson server is the sigint101 user service. Nudge it if it is not up, then wait for it so
# Chromium never opens on a connection-refused page.
wait_for_server() {
  if ! curl -sf "$COURSE_URL/api/hardware" >/dev/null 2>&1; then
    systemctl --user start sigint101.service 2>/dev/null || true
  fi
  # A brace range, not $(seq): no forked shell while the watchdog holds its lock.
  for _ in {1..60}; do
    curl -sf "$COURSE_URL/api/hardware" >/dev/null 2>&1 && break
    sleep 1
  done
}

kiosk_alive() {
  pgrep -f "[u]ser-data-dir=$PROFILE" >/dev/null
}

# The output's logical size as WxH, from wlr-randr when it answers (the first enabled output's
# current mode, divided by its scale, turned for a 90/270 transform), else the reference panel's
# 800x480. No window list is needed, and the reference box has no tool for one.
# It sets OUTPUT_SIZE rather than printing, and reads the pipeline's answer back from a file: run
# under the watchdog's lock, a $(...) subshell would be a forked shell holding the lock's backup
# for as long as wlr-randr takes, so a watchdog killed meanwhile would keep the next one out.
OUTPUT_SIZE=""
output_size() {
  local size="" answer="$RUNTIME/sigint101-kiosk-size.$$"
  if command -v wlr-randr >/dev/null 2>&1; then
    # -k: a wlr-randr that ignores TERM is killed, so it can never hold up a launch.
    timeout -k 2 5 wlr-randr 2>/dev/null | awk '
      function emit() {
        if (on && w > 0 && h > 0) {
          if (tr ~ /^(flipped-)?(90|270)$/) { t = w; w = h; h = t }
          if (sc <= 0) sc = 1
          printf "%dx%d\n", int(w / sc + 0.5), int(h / sc + 0.5)
          found = 1
          exit
        }
      }
      /^[^ \t]/ { emit(); on = 0; w = 0; h = 0; sc = 1; tr = "normal"; next }
      /^[ \t]+Enabled: yes/ { on = 1 }
      / px, .*current/ { split($1, m, "x"); w = m[1] + 0; h = m[2] + 0 }
      /^[ \t]+Scale:/ { sc = $2 + 0 }
      /^[ \t]+Transform:/ { tr = $2 }
      END { if (!found) emit() }' > "$answer" 2>/dev/null
    read -r size < "$answer" 2>/dev/null || size=""
    rm -f "$answer"
  fi
  [[ "$size" =~ ^[1-9][0-9]{0,5}x[1-9][0-9]{0,5}$ ]] || size="800x480"
  # A size no screen has (an odd scale, a bad mode) would make a worse window than the panel's own,
  # so keep to 320x240 up to 7680x4320 (8K), either way up; a Pi drives nothing above 4K.
  local w="${size%x*}" h="${size#*x}" long short
  if [ "$w" -ge "$h" ]; then long="$w" short="$h"; else long="$h" short="$w"; fi
  if [ "$long" -lt 320 ] || [ "$short" -lt 240 ] || [ "$long" -gt 7680 ] || [ "$short" -gt 4320 ]
  then
    size="800x480"
  fi
  OUTPUT_SIZE="$size"
}

# Chromium restores the window bounds it saved at the last close, and a window that does not go
# fullscreen keeps them: a saved 780x460 left a 20 px strip of the desktop showing down the
# reference panel's right edge. Drop the saved bounds and keep everything else. Only safe while no
# kiosk browser runs, which is the only time launch() is called. A missing, unreadable or odd
# Preferences file is left alone and the course launches anyway.
forget_window_placement() {
  local prefs="$PROFILE/Default/Preferences"
  [ -f "$prefs" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  python3 -I - "$prefs" 8>&- 9>&- <<'PY' ||
import json
import os
import sys
import tempfile

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        prefs = json.load(f)
except (OSError, ValueError):
    sys.exit(0)
browser = prefs.get("browser") if isinstance(prefs, dict) else None
if not isinstance(browser, dict):
    sys.exit(0)
gone = [browser.pop(k) for k in ("window_placement", "app_window_placement") if k in browser]
if not gone:
    sys.exit(0)
# Written beside the file and renamed over it, so Chromium never reads half a Preferences.
fd, tmp = tempfile.mkstemp(prefix=".Preferences.", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False, separators=(",", ":"))
    os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    os.replace(tmp, path)
except BaseException:
    os.unlink(tmp)
    raise
PY
    echo "sigint101-course: could not clear the saved window bounds; launching anyway" >&2
}

# Only ever called when no kiosk browser is alive, so a stale singleton from a crash is safe to
# clear and the "profile in use" dialog never blocks the panel.
# Do not count on --kiosk (or --start-fullscreen) for fullscreen: Chromium's source suggests that
# on Linux an --app window is never put in fullscreen, which would explain the strip. What covers
# the output is --window-position and --window-size, which Chromium applies over any bounds it
# saved. Clearing the saved placement alone would not do: Chromium's default window for an
# 800x480 screen is exactly 780x460 at 10,10. On the reference panel this is unverified.
# --disk-cache-size=1 keeps a stale stylesheet from outliving an update. --password-store=basic
# stops Chromium asking the desktop keyring, which otherwise covers the panel with a
# "choose password for new keyring" dialog that a keyboard-free kiosk can never dismiss.
# The browser must not inherit the watchdog's lock (fd 9), or a second watchdog could never start.
launch() {
  local size
  rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonCookie" "$PROFILE/SingletonSocket" 2>/dev/null
  forget_window_placement
  output_size
  size="$OUTPUT_SIZE"
  chromium --kiosk --start-fullscreen --ozone-platform=wayland --user-data-dir="$PROFILE" \
    --app="$COURSE_URL" --window-position=0,0 --window-size="${size%x*},${size#*x}" \
    --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble \
    --disable-features=TranslateUI --disk-cache-size=1 --password-store=basic 8>&- 9>&- &
}

# The desktop icon and the menu entry: the learner asked for the course back, so the exit is over.
open_course() {
  # >>> managed-gate >>>
  if ! try_shared_gate; then
    echo "sigint101-course: maintenance in progress (mutation gate busy); retry shortly" >&2
    return "$EXIT_GATE_BUSY"
  fi
  # <<< managed-gate <<<
  rm -f "$FLAG"
  wait_for_server
  kiosk_alive || launch
  # >>> managed-gate >>>
  exec 8>&-
  # <<< managed-gate <<<
}

# XDG autostart, once per desktop session. It must check, not wait: the /usr/bin/chromium wrapper
# returns before the browser exits, and a bare relaunch loop hands each new launch to the running
# browser as yet another window (it ran the reference Recon to load 20 in minutes).
watch() {
  if [ -z "${WAYLAND_DISPLAY:-}" ] || [ ! -S "$RUNTIME/$WAYLAND_DISPLAY" ]; then
    echo "sigint101-course: no Wayland session to put the course on; not watching" >&2
    exit 1
  fi
  # One watchdog per session, however often autostart runs.
  exec 9>"$LOCK"
  if ! flock -n 9; then
    exec 8>&- 9>&-
    echo "sigint101-course: a watchdog is already running (lock $LOCK open in: $(lock_holders));" \
      "not watching" >&2
    exit 0
  fi
  # Run the loop with fd 9 closed. Bash keeps the lock in a close-on-exec copy of it for the
  # length of the call, so no command the loop starts (sleep, curl, systemctl, pgrep, Chromium)
  # inherits it: a killed watchdog's lock dies with it, and the next one can start at once. A
  # forked shell would keep that copy, so the loop runs no $(...) or other subshell of its own.
  watch_loop 8>&- 9>&-
}

# The processes that have the lock file open, as "PID (name)", for the stand-down message.
lock_holders() {
  local fd pid name out=""
  while IFS= read -r fd; do
    pid="${fd#/proc/}"
    pid="${pid%%/*}"
    name="$(cat "/proc/$pid/comm" 2>/dev/null)"
    out+="${out:+, }$pid (${name:-gone})"
  done < <(find /proc/[0-9]*/fd -maxdepth 1 -lname "$LOCK" 2>/dev/null)
  printf '%s\n' "${out:-none found}"
}

watch_loop() {
  # >>> managed-gate >>>
  if try_shared_gate; then
  # <<< managed-gate <<<
  wait_for_server
  # >>> managed-gate >>>
    exec 8>&-
  fi
  # <<< managed-gate <<<
  # The loop ends with the session: an orphaned watchdog must not outlive its compositor.
  while [ -S "$RUNTIME/$WAYLAND_DISPLAY" ]; do
    # >>> managed-gate >>>
    if try_shared_gate; then
    # <<< managed-gate <<<
    # Browser first, then the flag. Exit writes the flag before it closes the browser, so once no
    # browser is alive because of an Exit, the flag is already there to be seen.
    if ! kiosk_alive && [ ! -e "$FLAG" ]; then
      launch
    fi
    # >>> managed-gate >>>
      exec 8>&-
    fi
    if [ "${SIGINT101_WATCH_ONCE:-0}" = "1" ]; then
      break
    fi
    # <<< managed-gate <<<
    sleep 10
  done
}

case "${1:-}" in
  watch) watch ;;
  open) open_course ;;
  *) usage; exit 2 ;;
esac
