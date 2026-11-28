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

# This user's running watchdogs: the processes that have the lock file open and whose command
# line is exactly "<shell> <launcher> watch". Never a pattern match on names: the watchdog's own
# children and the kiosk browser may have the file open too, and they are left alone.
watchdog_pids() {
  [ -e "$WATCH_LOCK" ] || return 0
  local fd pid seen=" "
  local -a argv
  while IFS= read -r fd; do
    pid="${fd#/proc/}"
    pid="${pid%%/*}"
    case "$seen" in *" $pid "*) continue ;; esac
    seen+="$pid "
    [ -O "/proc/$pid" ] || continue
    mapfile -d '' argv 2>/dev/null < "/proc/$pid/cmdline" || continue
    [ "${#argv[@]}" -eq 3 ] && [ "${argv[2]}" = watch ] || continue
    case "${argv[1]##*/}" in
      sigint101-course | start-kiosk.sh) printf '%s\n' "$pid" ;;
    esac
  done < <(find /proc/[0-9]*/fd -maxdepth 1 -lname "$WATCH_LOCK" 2>/dev/null)
}

# A running watchdog keeps the launcher it started with in memory, so without this a launcher
# change (the window bounds, say) waits for the next login. Stop it by PID and start the
# installed launcher's watchdog in the same desktop session, with that session's environment
# (install.sh is often run over SSH, which has no WAYLAND_DISPLAY). The kiosk browser is never
# touched: a learner may be mid-lesson, so new browser flags apply at its next launch.
restart_watchdog() {
  local pid
  local -a pids session_env run
  mapfile -t pids < <(watchdog_pids)
  [ "${#pids[@]}" -gt 0 ] || return 0
  if ! mapfile -d '' session_env 2>/dev/null < "/proc/${pids[0]}/environ"; then
    echo "install: WARN could not read the course watchdog's session; it restarts at the next login" >&2
    return 0
  fi
  kill -TERM "${pids[@]}" 2>/dev/null || true
  # The lock frees when the watchdog has exited. An older launcher let its sleep or curl inherit
  # the lock, which then lasts up to that command's end, so allow it some seconds.
  if ! flock -w 15 "$WATCH_LOCK" true; then
    kill -KILL "${pids[@]}" 2>/dev/null || true
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
