#!/bin/bash
# ABOUTME: Box setup for the course: the lesson server as a boot service, the panel kiosk on
# ABOUTME: tty1 autologin, and a persistent (size-capped) journal. Idempotent; safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MARK_BEGIN="# >>> sigint101 kiosk >>>"
MARK_END="# <<< sigint101 kiosk <<<"

# 1) Lesson server: a user unit plus lingering, so it runs from boot with nobody logged in.
install -D -m 0644 "$HERE/kiosk/sigint101.service" "$HOME/.config/systemd/user/sigint101.service"
systemctl --user daemon-reload
systemctl --user enable --now sigint101.service
sudo loginctl enable-linger "$USER"

# 2) Kiosk launcher at a stable path, so switching the course checkout's branch never breaks
#    the boot hook that calls it.
install -D -m 0755 "$HERE/kiosk/start-kiosk.sh" "$HOME/.local/bin/sigint101-kiosk"

# 3) tty1 autologin runs the login profile; on tty1 only (never SSH), hand it to the kiosk.
#    Bash reads ~/.profile when there is no ~/.bash_profile, which is the Pi OS default.
if ! grep -qF "$MARK_BEGIN" "$HOME/.profile"; then
  cat >> "$HOME/.profile" <<EOF

$MARK_BEGIN
if [ "\$(tty)" = /dev/tty1 ] && [ -z "\${WAYLAND_DISPLAY:-}" ]; then
  exec "\$HOME/.local/bin/sigint101-kiosk"
fi
$MARK_END
EOF
fi

# 4) Persistent journal, capped, so a crash leaves evidence without wearing the SD card down.
sudo install -D -m 0644 "$HERE/system/journald-sigint101.conf" \
  /etc/systemd/journald.conf.d/50-sigint101.conf
sudo mkdir -p /var/log/journal
sudo systemctl restart systemd-journald

echo "install: server service enabled, kiosk on tty1, journal persistent"
