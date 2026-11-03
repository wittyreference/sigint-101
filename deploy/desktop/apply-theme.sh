#!/bin/bash
# ABOUTME: Applies the SIGINT 101 phosphor-terminal look to the Pi desktop: fonts, GTK theme, bar,
# ABOUTME: window frames, wallpaper, desktop labels and terminal. Per-user, idempotent, live-reloaded.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
GREEN_RGB="rgb(46,230,106)"
FONT="PT Mono 11"

# set_key FILE SECTION KEY VALUE: an ini key set in place, or added under its section.
set_key() {
  local file="$1" section="$2" key="$3" value="$4"
  mkdir -p "$(dirname "$file")"
  [ -f "$file" ] || printf '[%s]\n' "$section" > "$file"
  grep -qF "[$section]" "$file" || printf '\n[%s]\n' "$section" >> "$file"
  if grep -q "^$key=" "$file"; then
    sed -i "s|^$key=.*|$key=$value|" "$file"
  else
    sed -i "/^\[$(printf '%s' "$section" | sed 's/[]*[]/\\&/g')\]/a $key=$value" "$file"
  fi
}

# 1) Fonts: the console's own PT Mono and Russo One, so the desktop speaks the same type.
install -d "$HOME/.local/share/fonts/sigint101"
install -m 0644 "$REPO"/sigint101/static/fonts/{ptmono,russoone}-*.woff2 \
  "$REPO/sigint101/static/fonts/OFL.txt" "$HOME/.local/share/fonts/sigint101/"
fc-cache -f "$HOME/.local/share/fonts" >/dev/null

# 2) GTK theme (bar, menus, dialogs). It lives in ~/.themes because the session start-up script
#    deletes ~/.config/gtk-3.0/gtk.css on every login.
install -D -m 0644 "$HERE/gtk-3.0/gtk.css" "$HOME/.themes/SIGINT101/gtk-3.0/gtk.css"
install -D -m 0644 "$HERE/index.theme" "$HOME/.themes/SIGINT101/index.theme"
gsettings set org.gnome.desktop.interface gtk-theme 'SIGINT101'
if fc-list | grep -q "PT Mono"; then
  gsettings set org.gnome.desktop.interface font-name "$FONT"
fi

# 3) Window frames and the compositor's menus.
install -D -m 0644 "$HERE/themerc-override" "$HOME/.config/labwc/themerc-override"

# 4) Wallpaper and desktop labels: black ground, green labels, no shadow.
install -D -m 0644 "$HERE/sigint101-wallpaper.png" "$HOME/.local/share/sigint101/wallpaper.png"
DESK="$HOME/.config/pcmanfm/LXDE-pi/desktop-items-0.conf"
if [ ! -f "$DESK" ] && [ -f /etc/xdg/pcmanfm/LXDE-pi/desktop-items-0.conf ]; then
  install -D -m 0644 /etc/xdg/pcmanfm/LXDE-pi/desktop-items-0.conf "$DESK"
fi
set_key "$DESK" "*" wallpaper "$HOME/.local/share/sigint101/wallpaper.png"
set_key "$DESK" "*" wallpaper_mode crop
set_key "$DESK" "*" wallpaper_common 1
set_key "$DESK" "*" desktop_bg "#000000000000"
set_key "$DESK" "*" desktop_shadow "#000000000000"
set_key "$DESK" "*" desktop_fg "#2e2ee6e66a6a"
set_key "$DESK" "*" desktop_font "$FONT"

# 5) The terminal: phosphor green on black, in PT Mono.
TERMCONF="$HOME/.config/lxterminal/lxterminal.conf"
set_key "$TERMCONF" general fontname "$FONT"
set_key "$TERMCONF" general fgcolor "$GREEN_RGB"
set_key "$TERMCONF" general bgcolor "rgb(0,0,0)"
set_key "$TERMCONF" general cursorcolor "$GREEN_RGB"

# 6) Reload whatever is running now. With no desktop session (provisioning, SSH before first
#    boot) each step finds nothing to reload and the next login picks the files up.
if pgrep -x labwc >/dev/null; then
  kill -HUP "$(pgrep -x labwc | head -1)"
fi
WAYLAND="$(ls "$RUNTIME" 2>/dev/null | grep -E '^wayland-[0-9]+$' | head -1 || true)"
if [ -n "$WAYLAND" ] && pgrep -f "[p]cmanfm --desktop" >/dev/null; then
  XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$WAYLAND" pcmanfm --reconfigure || true
fi
# The bar is restarted by lwrespawn, so a theme change shows at once.
if pgrep -x wf-panel-pi >/dev/null; then
  pkill -x wf-panel-pi || true
fi

echo "desktop theme: SIGINT101 applied (fonts, bar, frames, wallpaper, labels, terminal)"
