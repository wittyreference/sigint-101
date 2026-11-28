#!/bin/bash
# ABOUTME: Box setup: the lesson server as a boot service, the course full-screen on the Pi desktop
# ABOUTME: (watchdog, icon, menu entry), and a persistent journal. Idempotent; safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# The console kiosk's ~/.profile hook, which this retires.
MARK_BEGIN="# >>> sigint101 kiosk >>>"
MARK_END="# <<< sigint101 kiosk <<<"
ICON="/usr/local/share/icons/hicolor/scalable/apps/sigint101.svg"
COURSE_BIN="/usr/local/bin/sigint101-course"
# The running course watchdog holds a flock on this (deploy/kiosk/start-kiosk.sh).
WATCH_LOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/sigint101-kiosk-watch.lock"

# The processes holding the watchdog's flock, one PID a line: those with an fd on the lock file
# whose fdinfo lists the lock. A process that has merely opened the file holds nothing.
lock_holders() {
  [ -e "$WATCH_LOCK" ] || return 0
  local fd pid seen=" "
  while IFS= read -r fd; do
    pid="${fd#/proc/}"
    pid="${pid%%/*}"
    case "$seen" in *" $pid "*) continue ;; esac
    grep -q '^lock:.*FLOCK' "/proc/$pid/fdinfo/${fd##*/}" 2>/dev/null || continue
    seen+="$pid "
    printf '%s\n' "$pid"
  done < <(within_scan_limit find /proc/[0-9]*/fd -maxdepth 1 -lname "$WATCH_LOCK" 2>/dev/null)
}

# Run a command, cut off after SCAN_LIMIT seconds when that is set (wait_for_lock's time left).
SCAN_LIMIT=""
within_scan_limit() {
  if [ -n "$SCAN_LIMIT" ]; then
    timeout -s KILL "$SCAN_LIMIT" "$@"
  else
    "$@"
  fi
}

# The launchers a watchdog may run: the installed one, or this checkout's own.
CHECKOUT_LAUNCHER="$HERE/kiosk/start-kiosk.sh"

# watchdog_helper FIND PID... | TERM|KILL PID:START... — deploy/watchdog_helper.py, the one place
# that decides what is the watchdog and the only way anything is signalled (through a pidfd,
# never by number). Its docstring says what it checks and what window is left.
watchdog_helper() {
  within_scan_limit env -i PATH="$PATH" LANG=C.UTF-8 python3 -I "$HERE/watchdog_helper.py" "$1" \
    "$WATCH_LOCK" "$COURSE_BIN" "$CHECKOUT_LAUNCHER" "${@:2}"
}

NO_PIDFD="the course watchdog could not be pinned (no pidfd: Linux 5.3+ and Python 3.9+ are needed)"

# The running watchdogs, as "PID:START", from the lock's holders.
watchdogs() {
  local -a holders
  mapfile -t holders < <(lock_holders)
  [ "${#holders[@]}" -gt 0 ] || return 0
  watchdog_helper FIND "${holders[@]}"
}

# Does PID descend from one of the watchdogs given (as "PID:START"), or has it already exited?
# An older launcher's sleep, curl or systemctl holds the lock that way, and ends within seconds
# of the watchdog.
passing_holder() {
  local pid="$1" ppid stat w hops
  shift
  [ -e "/proc/$pid" ] || return 0
  for hops in 1 2 3 4 5 6 7 8; do
    if ! stat="$(cat "/proc/$pid/stat" 2>/dev/null)"; then
      [ "$hops" = 1 ] && return 0
      return 1
    fi
    stat="${stat##*) }"
    read -r _ ppid _ <<< "$stat"
    for w in "$@"; do [ "$ppid" = "${w%%:*}" ] && return 0; done
    [ "$ppid" -gt 1 ] 2>/dev/null || return 1
    pid="$ppid"
  done
  return 1
}

# Is PID the kiosk browser? Its command line names the kiosk's profile: the whole flag as an
# argument of its own, or the whole flag between spaces (or an end) inside one joined string, as
# Chromium rewrites its title. Never split on whitespace: the profile path may contain a space.
is_kiosk() {
  local a flag="--user-data-dir=$HOME/.config/chromium-kiosk"
  local -a argv
  mapfile -d '' argv 2>/dev/null < "/proc/$1/cmdline" || return 1
  for a in "${argv[@]}"; do
    [ "$a" = "$flag" ] && return 0
    [[ " $a " == *" $flag "* ]] && return 0
  done
  return 1
}

# wait_for_lock SECONDS, once the watchdogs in restart_watchdog's `found` were stopped: 0 when
# the lock is free, 2 when a watchdog not among them already holds it (another install, run at
# the same time, finished the hand-over first), 1 when the time ran out.
# One deadline on the monotonic clock (/proc/uptime, in hundredths) covers the waits and the
# scans, and each step gets at most the time left, so SECONDS is the whole wait.
wait_for_lock() {
  local deadline left step w
  local -a now
  deadline=$(( $(uptime_cs) + $1 * 100 ))
  while :; do
    left=$(( deadline - $(uptime_cs) ))
    [ "$left" -gt 0 ] || return 1
    step=$(( left < 100 ? left : 100 ))
    flock -w "$(cs_to_s "$step")" "$WATCH_LOCK" true && return 0
    left=$(( deadline - $(uptime_cs) ))
    [ "$left" -gt 0 ] || return 1
    SCAN_LIMIT="$(cs_to_s "$left")"
    mapfile -t now < <(watchdogs)
    SCAN_LIMIT=""
    for w in "${now[@]}"; do
      [[ " ${found[*]} " == *" $w "* ]] || return 2
    done
  done
}

# Hundredths of a second since boot, from the monotonic /proc/uptime.
uptime_cs() {
  local up rest
  read -r up rest < /proc/uptime
  printf '%s\n' "$(( 10#${up%.*} * 100 + 10#${up#*.} ))"
}

cs_to_s() {
  printf '%d.%02d\n' "$(( $1 / 100 ))" "$(( $1 % 100 ))"
}

# Is PID a watchdog that is not one of `found`: the one another install just started?
a_new_watchdog() {
  local w
  local -a seen
  mapfile -t seen < <(watchdog_helper FIND "$1")
  for w in "${seen[@]}"; do
    [[ " ${found[*]} " == *" $w "* ]] || return 0
  done
  return 1
}

HANDED_OVER="install: another install has already started the new course watchdog"

# A running watchdog keeps the launcher it started with in memory, so without this a launcher
# change (the window bounds, say) waits for the next login. Stop it through a pidfd and start the
# installed launcher's watchdog in the same desktop session, with that session's environment
# (install.sh is often run over SSH, which has no WAYLAND_DISPLAY). The kiosk browser is never
# touched: a learner may be mid-lesson, so new browser flags apply at its next launch.
restart_watchdog() {
  local pid w
  local -a found holders session_env run
  mapfile -t found < <(watchdogs)
  if [ "${#found[@]}" -eq 0 ]; then
    # Held, yet no watchdog holds it: a lookalike, or a kernel whose fdinfo lists no locks. Say
    # so, or the launcher change would go unapplied without a word.
    if [ -e "$WATCH_LOCK" ] && ! flock -n "$WATCH_LOCK" true; then
      echo "install: WARN the course watchdog's lock is held, but no watchdog holding it was" \
        "found; it was not restarted, and the new launcher applies at the next login" >&2
    fi
    return 0
  fi
  # Anything else holding the lock for good (a kiosk browser an older launcher started without
  # closing fd 9) would keep any new watchdog out, so the working one is left as it is.
  mapfile -t holders < <(lock_holders)
  for pid in "${holders[@]}"; do
    for w in "${found[@]}"; do [ "$pid" = "${w%%:*}" ] && continue 2; done
    # A watchdog that was not there a moment ago: another install got here first and has
    # already started the new one. Nothing to warn about, and nothing to stop.
    if a_new_watchdog "$pid"; then
      echo "$HANDED_OVER"
      return 0
    fi
    if is_kiosk "$pid" || ! passing_holder "$pid" "${found[@]}"; then
      echo "install: WARN the course watchdog was left running: $pid" \
        "($(cat "/proc/$pid/comm" 2>/dev/null)) holds its lock; it restarts at the next login" >&2
      return 0
    fi
  done
  if ! mapfile -d '' session_env 2>/dev/null < "/proc/${found[0]%%:*}/environ"; then
    echo "install: WARN could not read the course watchdog's session; it restarts at the next login" >&2
    return 0
  fi
  # Through pidfds only. Without one, nothing is signalled and the working watchdog stays.
  if ! watchdog_helper TERM "${found[@]}" >/dev/null; then
    echo "install: WARN $NO_PIDFD; it was left running and restarts at the next login" >&2
    return 0
  fi
  # The lock frees when the watchdog has exited. An older launcher let its sleep or curl inherit
  # the lock, which then lasts up to that command's end, so allow it some seconds.
  local waited=0
  wait_for_lock 15 || waited=$?
  if [ "$waited" = 2 ]; then
    echo "$HANDED_OVER"
    return 0
  fi
  if [ "$waited" != 0 ]; then
    # Re-checked after a fresh pin: only what is still the same watchdog is killed, and only if
    # it runs a reviewed launcher from a file its user cannot rewrite (else "kept PID REASON").
    local killed
    if ! killed="$(watchdog_helper KILL "${found[@]}")"; then
      echo "install: WARN $NO_PIDFD for its SIGKILL; it restarts at the next login" >&2
      return 0
    fi
    if [[ "$killed" == *kept* ]]; then
      local reason="${killed#*kept }"
      reason="${reason#* }"
      echo "install: WARN the course watchdog ignored TERM and was not killed: ${reason%%$'\n'*};" \
        "it restarts at the next login" >&2
      return 0
    fi
    waited=0
    wait_for_lock 5 || waited=$?
    if [ "$waited" = 2 ]; then
      echo "$HANDED_OVER"
      return 0
    fi
    if [ "$waited" != 0 ]; then
      echo "install: WARN the course watchdog's lock is still held; it restarts at the next login" >&2
      return 0
    fi
  fi
  run=("$COURSE_BIN" watch)
  if command -v systemd-cat >/dev/null 2>&1; then
    run=(systemd-cat -t sigint101-course "${run[@]}")
  fi
  setsid -f env -i "${session_env[@]}" "${run[@]}" </dev/null >/dev/null 2>&1
}

# A provision run can pull this checkout and keep its loaded build recipes. It invokes
# this installer from the course checkout when the server unit changes.
if [[ "$HERE" -ef "$HOME/sigint-101/deploy" && -x "$HERE/../.venv/bin/python" ]]; then
  (
    . "$HERE/provision.sh"
    ensure_ais_companion
  )
fi

# 1) Lesson server: a user unit plus lingering, so it runs from boot with nobody logged in.
install -D -m 0644 "$HERE/kiosk/sigint101.service" "$HOME/.config/systemd/user/sigint101.service"
systemctl --user daemon-reload
systemctl --user enable --now sigint101.service
sudo loginctl enable-linger "$USER"

# 2) The course on the Pi desktop (provision.sh boots it to the desktop with autologin). The
#    launcher sits at a stable path, so switching the course checkout's branch never breaks the
#    entries that call it. The menu entry and icon are system-wide because pcmanfm opens a desktop
#    Link without its "executable script?" prompt only when the target is under /usr/local/share/.
sudo install -D -m 0755 "$HERE/kiosk/start-kiosk.sh" /usr/local/bin/sigint101-course
sudo install -D -m 0644 "$HERE/kiosk/sigint101.svg" "$ICON"
sudo install -D -m 0644 "$HERE/kiosk/sigint101.desktop" \
  /usr/local/share/applications/sigint101.desktop
#    The session's XDG autostart runs the watchdog; the desktop icon reopens the course after Exit.
install -D -m 0644 "$HERE/kiosk/sigint101-autostart.desktop" \
  "$HOME/.config/autostart/sigint101.desktop"
install -D -m 0644 "$HERE/kiosk/sigint101-link.desktop" "$HOME/Desktop/sigint101.desktop"

# 3) Retire the console kiosk: raspi-config's desktop autologin also logs tty1 in, and the old
#    ~/.profile hook would start a second compositor there. Remove the hook and its launcher.
if [ -f "$HOME/.profile" ] && grep -qF "$MARK_BEGIN" "$HOME/.profile"; then
  sed -i "\|^$MARK_BEGIN\$|,\|^$MARK_END\$|d" "$HOME/.profile"
fi
rm -f "$HOME/.local/bin/sigint101-kiosk"

# 4) Persistent journal, capped, so a crash leaves evidence without wearing the SD card down.
sudo install -D -m 0644 "$HERE/system/journald-sigint101.conf" \
  /etc/systemd/journald.conf.d/50-sigint101.conf
sudo mkdir -p /var/log/journal
sudo systemctl restart systemd-journald

# 5) The desktop itself in the course's phosphor-terminal look: fonts, bar, frames, wallpaper,
#    labels and terminal (MC's call, 2026-11-03). Per-user; reloads whatever is running.
bash "$HERE/desktop/apply-theme.sh"

# 6) Hand the course watchdog over to the launcher just installed. The open kiosk stays open.
restart_watchdog

echo "install: server service enabled, course on the desktop with Exit and icon, journal persistent"
echo "install: window and launcher changes reach the kiosk at its next launch; to apply them now," \
  "Exit the course and reopen it from the SIGINT 101 icon (or sigint101-course open)"
