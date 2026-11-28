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
  done < <(find /proc/[0-9]*/fd -maxdepth 1 -lname "$WATCH_LOCK" 2>/dev/null)
}

# A process's start time (field 22 of /proc/PID/stat), which a reused PID does not share.
start_time() {
  local stat
  stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
  local -a f
  read -r -a f <<< "${stat##*) }"
  [ -n "${f[19]:-}" ] || return 1
  printf '%s\n' "${f[19]}"
}

# Is PID this user's watchdog? Its command line is exactly "bash|sh <launcher> watch". Never a
# pattern match on names.
is_watchdog() {
  local -a argv
  [ -O "/proc/$1" ] || return 1
  mapfile -d '' argv 2>/dev/null < "/proc/$1/cmdline" || return 1
  [ "${#argv[@]}" -eq 3 ] && [ "${argv[2]}" = watch ] || return 1
  case "${argv[0]##*/}" in bash | sh) ;; *) return 1 ;; esac
  case "${argv[1]##*/}" in sigint101-course | start-kiosk.sh) return 0 ;; esac
  return 1
}

# The running watchdogs, as "PID:START", from the lock's holders.
watchdogs() {
  local pid start
  while IFS= read -r pid; do
    is_watchdog "$pid" || continue
    start="$(start_time "$pid")" || continue
    printf '%s:%s\n' "$pid" "$start"
  done < <(lock_holders)
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

# Is PID the kiosk browser? Its command line names the kiosk's profile.
is_kiosk() {
  local a
  local -a argv
  mapfile -d '' argv 2>/dev/null < "/proc/$1/cmdline" || return 1
  for a in "${argv[@]}"; do
    [ "$a" = "--user-data-dir=$HOME/.config/chromium-kiosk" ] && return 0
  done
  return 1
}

# A running watchdog keeps the launcher it started with in memory, so without this a launcher
# change (the window bounds, say) waits for the next login. Stop it by PID and start the
# installed launcher's watchdog in the same desktop session, with that session's environment
# (install.sh is often run over SSH, which has no WAYLAND_DISPLAY). The kiosk browser is never
# touched: a learner may be mid-lesson, so new browser flags apply at its next launch.
restart_watchdog() {
  local pid w
  local -a found holders session_env run
  mapfile -t found < <(watchdogs)
  [ "${#found[@]}" -gt 0 ] || return 0
  # Anything else holding the lock for good (a kiosk browser an older launcher started without
  # closing fd 9) would keep any new watchdog out, so the working one is left as it is.
  mapfile -t holders < <(lock_holders)
  for pid in "${holders[@]}"; do
    for w in "${found[@]}"; do [ "$pid" = "${w%%:*}" ] && continue 2; done
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
  for w in "${found[@]}"; do kill -TERM "${w%%:*}" 2>/dev/null || true; done
  # The lock frees when the watchdog has exited. An older launcher let its sleep or curl inherit
  # the lock, which then lasts up to that command's end, so allow it some seconds.
  if ! flock -w 15 "$WATCH_LOCK" true; then
    # Only what is still the same watchdog: same PID and start time, the lock, the command line.
    mapfile -t holders < <(watchdogs)
    for w in "${found[@]}"; do
      for pid in "${holders[@]}"; do
        [ "$pid" = "$w" ] && kill -KILL "${w%%:*}" 2>/dev/null
      done
    done
    if ! flock -w 5 "$WATCH_LOCK" true; then
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
