#!/bin/bash
# ABOUTME: Box setup: the lesson server as a boot service, the course full-screen on the Pi desktop
# ABOUTME: (watchdog, icon, menu entry), and a persistent journal. Idempotent; safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# The console kiosk's ~/.profile hook, which this retires.
MARK_BEGIN="# >>> sigint101 kiosk >>>"
MARK_END="# <<< sigint101 kiosk <<<"
ICON="/usr/local/share/icons/hicolor/scalable/apps/sigint101.svg"

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

echo "install: server service enabled, course on the desktop with Exit and icon, journal persistent"
