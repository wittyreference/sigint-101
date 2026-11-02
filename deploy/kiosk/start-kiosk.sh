#!/bin/bash
# ABOUTME: Bring up the panel: wait for the lesson server, then labwc with a kiosk Chromium on it.
# ABOUTME: Run from the device's own tty1 login (the panel is seat0); install.sh wires that up.
set -u
COURSE_URL="http://127.0.0.1:8008"

# 1) The lesson server is the sigint101 user service. Nudge it if it is not up, then wait
#    for it so Chromium never opens on a connection-refused page.
if ! curl -sf "$COURSE_URL/api/hardware" >/dev/null 2>&1; then
  systemctl --user start sigint101.service 2>/dev/null || true
fi
for _ in $(seq 1 60); do
  curl -sf "$COURSE_URL/api/hardware" >/dev/null 2>&1 && break
  sleep 1
done

# 2) Compositor + kiosk browser. A DEDICATED profile dir avoids the shared-profile "in use" lock;
#    clear any stale singleton from a previous crash so the dialog never blocks the panel.
PROFILE="$HOME/.config/chromium-kiosk"
rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonCookie" "$PROFILE/SingletonSocket" 2>/dev/null
CFG="$(mktemp -d)"
cp "$HOME/.config/labwc/environment" "$CFG/environment" 2>/dev/null || true
# A watchdog relaunches Chromium only when no kiosk browser is alive, so a crash puts the course
# straight back on the panel. It must check, not wait: the /usr/bin/chromium wrapper returns
# before the browser exits, and a bare relaunch loop hands each new launch to the running
# browser as yet another window (it ran nomad-b to load 20 in minutes).
# --disk-cache-size=1 keeps a stale stylesheet from outliving an update. --password-store=basic
# stops Chromium asking the desktop keyring, which otherwise covers the panel with a
# "choose password for new keyring" dialog that a keyboard-free kiosk can never dismiss.
cat > "$CFG/autostart" <<A
while true; do
  if ! pgrep -f "[u]ser-data-dir=$PROFILE" >/dev/null; then
    chromium --kiosk --ozone-platform=wayland --user-data-dir="$PROFILE" --app=$COURSE_URL \
      --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble \
      --disable-features=TranslateUI --disk-cache-size=1 --password-store=basic &
  fi
  sleep 10
done &
A
exec labwc -C "$CFG"
