#!/usr/bin/env bash
# ABOUTME: Turns a stock SpecFive Nomad 2 Recon into a SIGINT 101 course box: apt tools, pinned
# ABOUTME: source builds, the course + venv, tcpdump caps, wlan1 monitor mode, kiosk. --check only reports.
#
# Run it as the user the desktop logs in as (the panel's user), never as root; it asks sudo for
# the system steps. Idempotent: every step checks first, then acts or says why it skipped.
#
#   deploy/provision.sh                  provision (or re-run to update)
#   deploy/provision.sh --check          report what would change; changes nothing
#   deploy/provision.sh --with-optional  also build dsd-fme, satdump and sstv (slow; see below)
#   deploy/provision.sh --with-flock-adapter
#                                        monitor mode at boot for the RTL8821CU Wi-Fi adapter;
#                                        only behind a self-powered USB hub (see step_monitor_mode)
#   deploy/provision.sh --branch NAME    follow a course branch other than main
#
# Nothing personal goes on the box: the course comes from the public repo over HTTPS, with no
# keys, no git identity and no tailnet.
set -euo pipefail

# Non-login SSH sessions leave the sbin directories off PATH; iw, getcap and setcap live there.
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
USER="${USER:-$(id -un)}"

COURSE_REPO="https://github.com/wittyreference/sigint-101"
COURSE_DIR="$HOME/sigint-101"
COURSE_BRANCH="main"
SRC_DIR="$HOME/.cache/sigint101-src"
STAMP_DIR="/usr/local/share/sigint101/builds"
PROVISION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK=0
OPTIONAL=0
FLOCK_ADAPTER=0

# --- Source builds, pinned --------------------------------------------------------------------
# Core decoders. Each pin is the commit the reference Recon runs, read from its build
# checkouts on 2026-11-02.
DUMP1090_REPO="https://github.com/flightaware/dump1090"
DUMP1090_PIN="465e06330372ab0586a6a5063c66b60a8750f2b6"  # master just after v11.1 (M13 ADS-B)
ACARSDEC_REPO="https://github.com/TLeconte/acarsdec"
ACARSDEC_PIN="339f63eb91a890cfe5b199ad70814cfe86702d1e"  # 3.7, the archived repo's last commit (M16)
REDSEA_REPO="https://github.com/windytan/redsea"
REDSEA_PIN="7555c9f6259d50718697ee8c9f218ea012c6892c"    # 1.3.1 plus CI-only commits (M12 RDS)
# The reference Recon's rtl-ais checkout is gone. Its binary has the -U option (added
# 2026-08-05) and was built 2026-10-29; master's head (the -U merge) is the only commit that
# fits.
RTL_AIS_REPO="https://github.com/dgiardini/rtl-ais"
RTL_AIS_PIN="d84764bd20d93de68d3275e4914f897adfb353cc"   # master, 2026-08-05 (M26 AIS)

# Optional decoders (--with-optional). The reference Recon has none of them, so its lessons
# answer with an honest 503. These pins are current upstream releases, not anything proven
# on it.
MBELIB_REPO="https://github.com/lwvmobile/mbelib"
MBELIB_PIN="30dc79074ca022366a27d705b8023011d9600339"    # ambe_tones branch, as dsd-fme requires
DSD_FME_REPO="https://github.com/lwvmobile/dsd-fme"
DSD_FME_PIN="69d31151dd2d634cdc02aac4bff0e2a12c756ddb"   # release 20260715 (M27 DMR/P25)
SATDUMP_REPO="https://github.com/SatDump/SatDump"
SATDUMP_PIN="7aef0fe8441bc3eb440b1b6ba053556da5e40991"   # release 1.2.2 (M24 NOAA APT)
SSTV_REPO="https://github.com/colaclanth/sstv"
SSTV_PIN="3e556eee8ad4c4425799cb652bac26ee58f8e113"      # master, its last commit (M25 SSTV)
SSTV_VENV="/opt/sigint101/sstv"

# --- Apt packages -----------------------------------------------------------------------------
# What the course runs: the RTL-SDR tools (rtl_power, rtl_fm, rtl_test), rtl_433, multimon-ng
# (APRS + POCSAG), tcpdump + setcap (Flock), iw (monitor mode), lsusb (the radio probe), a system
# font that carries U+262D (the bundled fonts don't), and the kiosk's browser and compositor.
APT_RUNTIME=(rtl-sdr rtl-433 multimon-ng tcpdump libcap2-bin iw usbutils fonts-dejavu-core
             git curl python3-venv chromium labwc python3-smbus polkitd)
# What the pinned core builds need (the set the reference Recon built them with).
APT_BUILD=(build-essential cmake pkg-config meson ninja-build librtlsdr-dev libncurses-dev
           libsndfile1-dev libliquid-dev)
# What the optional builds need: sox (SSTV), dsd-fme's list, SatDump's headless list.
APT_OPTIONAL=(sox libpulse-dev libfftw3-dev liblapack-dev libcodec2-dev libpng-dev libtiff-dev
              libjemalloc-dev libcurl4-openssl-dev libvolk2-dev libnng-dev libzstd-dev)

# --- Output -----------------------------------------------------------------------------------
CHANGES=0
WARNINGS=0
step()  { printf '\n==> %s\n' "$*"; }
ok()    { printf '  ok     %s\n' "$*"; }
did()   { printf '  did    %s\n' "$*"; CHANGES=$((CHANGES + 1)); }
would() { printf '  would  %s\n' "$*"; CHANGES=$((CHANGES + 1)); }
skip()  { printf '  skip   %s\n' "$*"; }
warn()  { printf '  WARN   %s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }
die()   { printf 'provision: %s\n' "$*" >&2; exit 1; }

# apply "what" command... — in --check say what would happen; otherwise do it and say so.
apply() {
  local what="$1"
  shift
  if [ "$CHECK" = 1 ]; then
    would "$what"
    return 0
  fi
  "$@"
  did "$what"
}

# put_file PATH MODE CONTENT — a root-owned config file, rewritten only when its content differs.
# Sets FILE_CHANGED=1 when it wrote (or, under --check, would write) the file.
FILE_CHANGED=0
put_file() {
  local path="$1" mode="$2" content="$3"
  if [ -f "$path" ] && printf '%s\n' "$content" | cmp -s - "$path"; then
    ok "$path"
    return 0
  fi
  FILE_CHANGED=1
  apply "write $path" write_root_file "$path" "$mode" "$content"
}

write_root_file() {
  sudo mkdir -p "$(dirname "$1")"
  printf '%s\n' "$3" | sudo tee "$1" >/dev/null
  sudo chmod "$2" "$1"
}

pkg_installed() {
  local status
  status="$(dpkg-query -W -f='${db:Status-Abbrev}' "$1" 2>/dev/null)" || return 1
  [ "${status:0:2}" = "ii" ]
}

# --- Steps ------------------------------------------------------------------------------------
step_preflight() {
  step "Preflight"
  [ "$(id -u)" -ne 0 ] || die "run this as the panel's login user, not root (it uses sudo itself)"
  local codename="unknown" arch
  if [ -r /etc/os-release ]; then
    codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-unknown}")"
  fi
  arch="$(uname -m)"
  if [ "$codename" = bookworm ] && [ "$arch" = aarch64 ]; then
    ok "Debian $codename on $arch, user $USER"
  else
    warn "built for Bookworm on aarch64; this is $codename on $arch — continuing"
  fi
  if [ "$CHECK" = 1 ]; then
    ok "check mode: nothing will be changed"
  else
    # `sudo true`, not `sudo -v`: -v demands a password unless every sudoers entry for the user
    # is NOPASSWD (verifypw=all), so it refuses on boxes where real commands run password-free.
    sudo true || die "sudo is needed for the system steps"
  fi
}

# The panel JavaScript tests use mock.timers.enable({ apis: ... }), which landed in Node 21.2 and
# was backported to 20.11. The kiosk itself never runs Node, and Debian Bookworm's apt nodejs is
# 18.x, so step_node warns and carries on (or reports pending under --check) rather than stopping.
NODE_FLOOR_TEXT="Node.js 20.11+ (21.2+ on the 21 line)"

node_version_ok() {
  local major="$1" minor="$2"
  if [ "$major" -gt 21 ]; then
    return 0
  elif [ "$major" -eq 21 ]; then
    [ "$minor" -ge 2 ]
  elif [ "$major" -eq 20 ]; then
    [ "$minor" -ge 11 ]
  else
    return 1
  fi
}

node_report_gap() {
  local detail="$1"
  if [ "$CHECK" = 1 ]; then
    would "need $NODE_FLOOR_TEXT ($detail; only the JS panel tests need it)"
  else
    warn "$detail; only the JS panel tests need $NODE_FLOOR_TEXT (kiosk runs without it) — continuing"
  fi
}

step_node() {
  step "Node.js (only the JS panel tests need $NODE_FLOOR_TEXT)"
  local node_bin ver major minor
  node_bin="$(command -v node || true)"
  if [ -z "$node_bin" ]; then
    node_report_gap "node is not on PATH"
    return 0
  fi
  if ! ver="$("$node_bin" --version 2>&1)"; then
    ver="${ver//$'\r'/}"
    ver="${ver%%$'\n'*}"
    node_report_gap "could not read node version from $node_bin (${ver:-no output})"
    return 0
  fi
  ver="${ver//$'\r'/}"
  if [[ ! "$ver" =~ ^v?([0-9]+)\.([0-9]+)\.[0-9]+([-+].*)?$ ]]; then
    ver="${ver%%$'\n'*}"
    node_report_gap "unreadable node version '$ver' from $node_bin"
    return 0
  fi
  major=$((10#${BASH_REMATCH[1]}))
  minor=$((10#${BASH_REMATCH[2]}))
  if ! node_version_ok "$major" "$minor"; then
    node_report_gap "node $ver at $node_bin is too old"
    return 0
  fi
  ok "node $ver at $node_bin (meets $NODE_FLOOR_TEXT)"
}

step_desktop_autologin() {
  step "Boot to the desktop with autologin (the course opens full-screen on top of it)"
  local target conf="/etc/lightdm/lightdm.conf"
  target="$(systemctl get-default)"
  if [ "$target" = graphical.target ] && grep -qx "autologin-user=$USER" "$conf" 2>/dev/null; then
    ok "boots to $target; the desktop logs in as $USER"
    return 0
  fi
  command -v raspi-config >/dev/null || die "raspi-config is missing; it sets the boot behaviour"
  # B4 is raspi-config's "desktop, autologin" (MC's call, 2026-11-03: desktop + course on top).
  # It also logs tty1 in, so install.sh removes the old console kiosk's ~/.profile hook.
  apply "set desktop autologin as $USER (was $target)" \
    sudo raspi-config nonint do_boot_behaviour B4
}

step_user_groups() {
  step "Device access for $USER (dialout: Flipper and serial; plugdev: the RTL-SDR)"
  local groups missing=()
  groups=" $(id -nG "$USER") "
  for g in dialout plugdev; do
    [[ "$groups" == *" $g "* ]] || missing+=("$g")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    ok "$USER is in dialout and plugdev"
    return 0
  fi
  local list
  list="$(IFS=,; echo "${missing[*]}")"
  apply "add $USER to $list (takes effect at the next login)" sudo usermod -aG "$list" "$USER"
}

# ensure_packages LABEL PACKAGE... — install whichever of the packages are missing, in one go.
ensure_packages() {
  local label="$1" p missing=()
  shift
  for p in "$@"; do
    pkg_installed "$p" || missing+=("$p")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    ok "$label: all $# present"
    return 0
  fi
  apply "apt install ($label): ${missing[*]}" apt_install "${missing[@]}"
}

apt_install() {
  sudo apt-get update -q
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@"
}

step_apt() {
  step "Apt packages"
  ensure_packages "runtime" "${APT_RUNTIME[@]}"
  ensure_packages "build" "${APT_BUILD[@]}"
  if [ "$OPTIONAL" = 1 ]; then
    ensure_packages "optional" "${APT_OPTIONAL[@]}"
  fi
}

step_course() {
  step "Course checkout at $COURSE_DIR (public HTTPS, no keys)"
  if [ ! -d "$COURSE_DIR/.git" ]; then
    apply "clone $COURSE_REPO ($COURSE_BRANCH) into $COURSE_DIR" \
      git clone --branch "$COURSE_BRANCH" "$COURSE_REPO" "$COURSE_DIR"
    return 0
  fi
  local url branch head remote
  url="$(git -C "$COURSE_DIR" remote get-url origin 2>/dev/null)" || url="(none)"
  case "$url" in
    "$COURSE_REPO" | "$COURSE_REPO.git") ok "origin is $url" ;;
    *) warn "origin is $url, not $COURSE_REPO" ;;
  esac
  branch="$(git -C "$COURSE_DIR" rev-parse --abbrev-ref HEAD)"
  head="$(git -C "$COURSE_DIR" rev-parse HEAD)"
  if [ "$branch" != "$COURSE_BRANCH" ]; then
    warn "checkout is on '$branch' @ ${head:0:7}, not '$COURSE_BRANCH'; leaving it alone"
    return 0
  fi
  # --no-optional-locks: a status check must not rewrite the index, even under --check.
  if [ -n "$(git --no-optional-locks -C "$COURSE_DIR" status --porcelain --untracked-files=no)" ]; then
    warn "checkout has local edits; not updating it"
    return 0
  fi
  remote="$(git ls-remote "$COURSE_REPO" "refs/heads/$COURSE_BRANCH" 2>/dev/null | cut -f1)" || remote=""
  if [ -z "$remote" ]; then
    warn "cannot reach $COURSE_REPO; staying on ${head:0:7}"
  elif [ "$remote" = "$head" ]; then
    ok "$COURSE_BRANCH @ ${head:0:7}, up to date"
  else
    apply "update $COURSE_BRANCH ${head:0:7} -> ${remote:0:7} (fast-forward only)" \
      git -C "$COURSE_DIR" pull --ff-only origin "$COURSE_BRANCH"
  fi
}

step_venv() {
  step "Python venv (bootstrap.sh installs requirements.txt, meshtastic included)"
  local req="$COURSE_DIR/requirements.txt" py="$COURSE_DIR/.venv/bin/python"
  if [ ! -f "$req" ]; then
    [ "$CHECK" = 1 ] || die "no $req; the course checkout is incomplete"
    would "run bootstrap.sh once the course is cloned"
    return 0
  fi
  # pip's dry run against installed packages only: offline, and it changes nothing.
  if [ -x "$py" ] && PYTHONDONTWRITEBYTECODE=1 "$py" -m pip install --dry-run --no-index \
       --disable-pip-version-check -q -r "$req" >/dev/null 2>&1; then
    ok "$COURSE_DIR/.venv satisfies requirements.txt"
    return 0
  fi
  apply "run bootstrap.sh (create or refresh .venv)" "$COURSE_DIR/bootstrap.sh"
}

# ensure_build NAME PROBE REPO PIN RECIPE — build a pinned upstream commit into /usr/local.
# PROBE is a command name, or a path for a library. A stamp records what provisioning built, so
# a changed pin rebuilds; a tool that is present with no stamp was built by hand and is left be.
ensure_build() {
  local name="$1" probe="$2" repo="$3" pin="$4" recipe="$5"
  local stamp="$STAMP_DIR/$name" have=""
  if [[ "$probe" == */* ]]; then
    [ -e "$probe" ] && have="$probe"
  else
    have="$(command -v "$probe" || true)"
  fi
  if [ -n "$have" ]; then
    if [ ! -f "$stamp" ]; then
      ok "$name: $have (built before provisioning, no pin stamp; left as is)"
      return 0
    fi
    if [ "$(cat "$stamp")" = "$repo@$pin" ]; then
      ok "$name: $have @ ${pin:0:10}"
      return 0
    fi
  fi
  if [ "$CHECK" = 1 ]; then
    would "build $name from $repo @ ${pin:0:10} into /usr/local"
    return 0
  fi
  fetch_source "$name" "$repo" "$pin"
  (cd "$SRC_DIR/$name" && "$recipe")
  sudo mkdir -p "$STAMP_DIR"
  echo "$repo@$pin" | sudo tee "$stamp" >/dev/null
  did "built $name @ ${pin:0:10}"
}

# Exactly one commit, shallow: GitHub serves a pinned SHA directly.
fetch_source() {
  local dir="$SRC_DIR/$1"
  rm -rf "$dir"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" fetch -q --depth 1 "$2" "$3"
  git -C "$dir" checkout -q FETCH_HEAD
}

recipe_dump1090() {
  make -j"$(nproc)" BLADERF=no DUMP1090_VERSION="git-${DUMP1090_PIN:0:7}"
  sudo install -m 0755 dump1090 /usr/local/bin/dump1090-fa
}

recipe_acarsdec() {
  cmake -B build -Drtl=ON
  make -C build -j"$(nproc)"
  sudo make -C build install
}

recipe_redsea() {
  meson setup build --prefix=/usr/local --buildtype=release
  meson compile -C build
  sudo meson install -C build
}

recipe_rtl_ais() {
  make -j"$(nproc)"
  sudo make install PREFIX=/usr/local
}

ais_companion_capable() {
  local reply
  reply="$(timeout -k 1 2 "$1" --sigint101-capability </dev/null 2>/dev/null)" &&
    [ "$reply" = "sigint101-ais-udp-v1" ]
}

ensure_ais_companion() {
  local name="rtl_ais-sigint101" target="/usr/local/bin/rtl_ais-sigint101"
  local patch_file="$PROVISION_DIR/patches/rtl-ais-companion.patch"
  local stamp="$STAMP_DIR/$name" have digest revision
  local pending="$stamp.pending"
  have="$(command -v "$name" || true)"
  if [ -z "$have" ] && { [ -e "$target" ] || [ -L "$target" ]; }; then
    have="$target"
  fi
  if [ -n "$have" ] && { [ "$have" != "$target" ] || [ -L "$target" ] ||
      { [ ! -f "$stamp" ] && [ ! -f "$pending" ]; }; }; then
    if ais_companion_capable "$have"; then
      ok "$name: $have (unmanaged, compatible; left as is)"
      return 0
    fi
    local reason="$name: incompatible unmanaged executable at $have; move it aside and rerun deploy/provision.sh"
    if [ "$CHECK" = 1 ]; then
      warn "$reason"
      return 0
    fi
    die "$reason"
  fi
  digest="$(sha256sum "$patch_file")"
  revision="$RTL_AIS_REPO@$RTL_AIS_PIN+${digest%% *}"
  if [ -n "$have" ] && [ ! -f "$pending" ] && [ "$(cat "$stamp")" = "$revision" ] &&
      ais_companion_capable "$have"; then
    ok "$name: $have @ ${RTL_AIS_PIN:0:10}, patch ${digest:0:12}"
    return 0
  fi
  if [ "$CHECK" = 1 ]; then
    would "build $name @ ${RTL_AIS_PIN:0:10}, patch ${digest:0:12} (missing, outdated or incompatible)"
    return 0
  fi
  fetch_source "$name" "$RTL_AIS_REPO" "$RTL_AIS_PIN"
  (
    cd "$SRC_DIR/$name"
    patch --batch --forward -p1 -i "$patch_file"
    SIGINT101_RTL_AIS_SOURCE="$PWD" "$COURSE_DIR/.venv/bin/python" -m pytest -q \
      -o cache_dir="$SRC_DIR/$name/.pytest_cache" "$PROVISION_DIR/../tests/test_rtl_ais_native.py"
    make -j"$(nproc)"
    ais_companion_capable ./rtl_ais || die "$name build failed its capability query"
    # Keep an interrupted install managed until a verified binary has its stamp.
    sudo mkdir -p "$STAMP_DIR"
    sudo touch "$pending"
    sudo install -m 0755 rtl_ais "$target"
  )
  ais_companion_capable "$target" || die "$name installation failed its capability query"
  printf '%s\n' "$revision" | sudo tee "$stamp" >/dev/null
  sudo rm -f "$pending"
  did "built $name @ ${RTL_AIS_PIN:0:10}, patch ${digest:0:12}"
}

recipe_mbelib() {
  cmake -B build
  make -C build -j"$(nproc)"
  sudo make -C build install
  sudo ldconfig
}

recipe_dsd_fme() {
  cmake -B build
  make -C build -j"$(nproc)"
  sudo make -C build install
  sudo ldconfig
}

recipe_satdump() {
  cmake -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
    -DBUILD_GUI=OFF -DBUILD_OPENCL=OFF
  # Two jobs: SatDump's C++ units are memory-hungry, and a 4 GB Pi swaps to a halt at four.
  make -C build -j2
  sudo make -C build install
  sudo ldconfig
}

# colaclanth/sstv is a Python CLI; it gets its own venv so its numpy/scipy stay out of the course's.
# pip installs the pinned archive, not the checkout: building in-tree as root would leave
# root-owned build files in the user's source cache.
recipe_sstv() {
  sudo python3 -m venv "$SSTV_VENV"
  sudo "$SSTV_VENV/bin/pip" install -q --disable-pip-version-check \
    "$SSTV_REPO/archive/$SSTV_PIN.tar.gz"
  sudo ln -sf "$SSTV_VENV/bin/sstv" /usr/local/bin/sstv
}

step_builds() {
  step "Decoders built from pinned source"
  ensure_build dump1090-fa dump1090-fa "$DUMP1090_REPO" "$DUMP1090_PIN" recipe_dump1090
  ensure_build acarsdec acarsdec "$ACARSDEC_REPO" "$ACARSDEC_PIN" recipe_acarsdec
  ensure_build redsea redsea "$REDSEA_REPO" "$REDSEA_PIN" recipe_redsea
  ensure_build rtl_ais rtl_ais "$RTL_AIS_REPO" "$RTL_AIS_PIN" recipe_rtl_ais
  ensure_ais_companion
}

step_optional_builds() {
  step "Optional decoders (dsd-fme M27, satdump M24, sstv M25)"
  if [ "$OPTIONAL" != 1 ]; then
    local tool
    for tool in dsd-fme satdump sstv; do
      if command -v "$tool" >/dev/null; then
        ok "$tool: $(command -v "$tool")"
      else
        skip "$tool: not installed; its lesson shows an honest 503 (--with-optional builds it)"
      fi
    done
    return 0
  fi
  ensure_build mbelib /usr/local/lib/libmbe.so "$MBELIB_REPO" "$MBELIB_PIN" recipe_mbelib
  ensure_build dsd-fme dsd-fme "$DSD_FME_REPO" "$DSD_FME_PIN" recipe_dsd_fme
  # Expect one to three hours on a Pi 4.
  ensure_build satdump satdump "$SATDUMP_REPO" "$SATDUMP_PIN" recipe_satdump
  ensure_build sstv sstv "$SSTV_REPO" "$SSTV_PIN" recipe_sstv
}

APT_CAPS_HOOK='// Written by SIGINT 101 deploy/provision.sh. An upgrade replaces /usr/bin/tcpdump and drops
// its file capabilities; put them back so the Flock lesson keeps capturing without root.
DPkg::Post-Invoke { "if [ -x /usr/bin/tcpdump ] && [ -x /usr/sbin/setcap ]; then /usr/sbin/setcap cap_net_raw,cap_net_admin+eip /usr/bin/tcpdump || true; fi"; };'

step_tcpdump_caps() {
  step "tcpdump capture rights (the Flock detector runs it as $USER, not root)"
  local bin="/usr/bin/tcpdump" caps=""
  if [ -x "$bin" ] && command -v getcap >/dev/null; then
    caps="$(getcap "$bin" 2>/dev/null)" || caps=""
  fi
  if [[ "$caps" == *"cap_net_admin,cap_net_raw=eip"* ]]; then
    ok "$bin has cap_net_raw,cap_net_admin+eip"
  else
    apply "setcap cap_net_raw,cap_net_admin+eip $bin" \
      sudo setcap cap_net_raw,cap_net_admin+eip "$bin"
  fi
  put_file /etc/apt/apt.conf.d/80-sigint101-tcpdump-caps 0644 "$APT_CAPS_HOOK"
}

# The RTL8821CU USB adapter is the Flock lesson's listening radio (wlan1). NetworkManager leaves
# it alone; a udev rule starts a oneshot unit that flips it to monitor mode whenever it appears,
# at boot or when plugged in later. No adapter, no event: boot never waits for it.
MONITOR_DRIVERS="rtw_8821cu|rtl8821cu"
MONITOR_NM_CONF='# Written by SIGINT 101 deploy/provision.sh. The RTL8821CU USB adapter is the listening
# radio for the Flock lesson; NetworkManager leaves it alone so it can stay in monitor mode.
[keyfile]
unmanaged-devices=driver:rtw_8821cu;driver:rtl8821cu'
MONITOR_UDEV_RULE='# Written by SIGINT 101 deploy/provision.sh. When the RTL8821CU USB Wi-Fi adapter appears,
# start the unit that puts it in monitor mode. No adapter, no event: boot never waits on it.
ACTION=="add", SUBSYSTEM=="net", KERNEL=="wlan*", DRIVERS=="'"$MONITOR_DRIVERS"'", TAG+="systemd", ENV{SYSTEMD_WANTS}+="sigint101-monitor@%k.service"'
MONITOR_UNIT='# Written by SIGINT 101 deploy/provision.sh. Puts one Wi-Fi interface in monitor mode for the
# Flock lesson; the udev rule starts it when the RTL8821CU adapter appears.
[Unit]
Description=SIGINT 101 monitor mode on %i
BindsTo=sys-subsystem-net-devices-%i.device
After=sys-subsystem-net-devices-%i.device NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=ip link set %i down
ExecStart=iw dev %i set type monitor
ExecStart=ip link set %i up'

# The interfaces whose driver is the monitor adapter's, by name.
monitor_adapters() {
  local dev drv
  for dev in /sys/class/net/*; do
    [ -e "$dev/device/driver" ] || continue
    drv="$(basename "$(readlink -f "$dev/device/driver")")"
    if [[ "$drv" =~ ^($MONITOR_DRIVERS)$ ]]; then
      basename "$dev"
    fi
  done
}

step_monitor_mode() {
  step "Monitor mode for the RTL8821CU adapter at boot (Flock, M18)"
  # Opt-in, because of power. On the reference Recon, bringing the adapter up in monitor mode on
  # the Nomad's own USB port drew more than the port supplies: 192 over-current events in two
  # minutes, each resetting the adapter and the touch panel that shares the hub. It needs a
  # self-powered USB hub, so only a box that has one asks for it with --with-flock-adapter.
  if [ "$FLOCK_ADAPTER" != 1 ]; then
    skip "not enabled: the adapter needs a powered USB hub (--with-flock-adapter turns it on)"
    return 0
  fi
  FILE_CHANGED=0
  put_file /etc/NetworkManager/conf.d/50-sigint101-monitor.conf 0644 "$MONITOR_NM_CONF"
  put_file /etc/udev/rules.d/70-sigint101-monitor.rules 0644 "$MONITOR_UDEV_RULE"
  put_file /etc/systemd/system/sigint101-monitor@.service 0644 "$MONITOR_UNIT"
  if [ "$FILE_CHANGED" = 1 ]; then
    apply "reload systemd, udev and NetworkManager config" reload_monitor_config
  fi
  local adapters iface type
  adapters="$(monitor_adapters)"
  if [ -z "$adapters" ]; then
    skip "no RTL8821CU adapter plugged in; the rule acts when one appears"
    return 0
  fi
  for iface in $adapters; do
    [ "$iface" = wlan1 ] || warn "the adapter is $iface, but the course listens on wlan1"
    type="$(iw dev "$iface" info 2>/dev/null | awk '$1 == "type" { print $2 }')" || type=""
    if [ "$type" = monitor ]; then
      ok "$iface is in monitor mode"
    else
      apply "start sigint101-monitor@$iface ($iface is ${type:-unknown} now)" \
        sudo systemctl restart "sigint101-monitor@$iface.service"
    fi
  done
}

reload_monitor_config() {
  sudo systemctl daemon-reload
  sudo udevadm control --reload-rules
  if command -v nmcli >/dev/null; then
    sudo nmcli general reload conf
  fi
}

step_glyph_font() {
  step "A system font for the hammer and sickle (U+262D; the bundled fonts don't carry it)"
  local fonts=""
  if command -v fc-list >/dev/null; then
    fonts="$(fc-list ':charset=262d' family 2>/dev/null | sort -u | paste -sd ';' -)" || fonts=""
  fi
  if [[ "$fonts" == *"DejaVu Sans Mono"* ]]; then
    ok "U+262D renders from DejaVu Sans Mono (also: $fonts)"
  elif [ "$CHECK" = 1 ] && ! pkg_installed fonts-dejavu-core; then
    would "get it from fonts-dejavu-core (the apt step above)"
  else
    warn "no DejaVu Sans Mono with U+262D; the glyph may show as a box (found: ${fonts:-none})"
  fi
}

# install.sh owns the boot service, the course on the desktop and the journal; this only decides
# whether to call it, by comparing what it installs with the checkout's copies.
step_kiosk() {
  step "Boot service, the course on the desktop and persistent journal (deploy/install.sh)"
  local d="$COURSE_DIR/deploy"
  if [ ! -f "$d/install.sh" ]; then
    if [ "$CHECK" = 1 ] && [ ! -d "$COURSE_DIR/.git" ]; then
      would "run deploy/install.sh once the course is cloned"
      return 0
    fi
    [ "$CHECK" = 1 ] || die "$d/install.sh is missing; is $COURSE_DIR on $COURSE_BRANCH?"
    warn "$d/install.sh is missing in this checkout, so the kiosk can't be compared"
    return 0
  fi
  local stale=()
  cmp -s "$d/kiosk/sigint101.service" "$HOME/.config/systemd/user/sigint101.service" ||
    stale+=("server unit")
  cmp -s "$d/kiosk/start-kiosk.sh" /usr/local/bin/sigint101-course || stale+=("course launcher")
  cmp -s "$d/kiosk/sigint101.desktop" /usr/local/share/applications/sigint101.desktop ||
    stale+=("menu entry")
  cmp -s "$d/kiosk/sigint101.svg" /usr/local/share/icons/hicolor/scalable/apps/sigint101.svg ||
    stale+=("icon")
  cmp -s "$d/kiosk/sigint101-autostart.desktop" "$HOME/.config/autostart/sigint101.desktop" ||
    stale+=("autostart entry")
  cmp -s "$d/kiosk/sigint101-link.desktop" "$HOME/Desktop/sigint101.desktop" ||
    stale+=("desktop icon")
  cmp -s "$d/system/journald-sigint101.conf" /etc/systemd/journald.conf.d/50-sigint101.conf ||
    stale+=("journald drop-in")
  # The console kiosk's pieces must be gone, or desktop autologin's tty1 login starts a second
  # compositor.
  if grep -qF "# >>> sigint101 kiosk >>>" "$HOME/.profile" 2>/dev/null; then
    stale+=("old tty1 hook")
  fi
  [ ! -e "$HOME/.local/bin/sigint101-kiosk" ] || stale+=("old tty1 launcher")
  [ -e "/var/lib/systemd/linger/$USER" ] || stale+=("linger")
  systemctl --user --quiet is-enabled sigint101.service 2>/dev/null || stale+=("service enabled")
  if [ ${#stale[@]} -eq 0 ]; then
    ok "server unit, course launcher, desktop entries, linger and journal match the checkout"
    return 0
  fi
  apply "run deploy/install.sh (differs: ${stale[*]})" "$d/install.sh"
}

ensure_power_user() {
  if command -v systemd-sysusers >/dev/null; then
    sudo systemd-sysusers /usr/local/lib/sysusers.d/sigint101-power.conf
  else
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin --user-group sigint101-power
  fi
}

install_power_dropin() {
  local dst="$1" content="$2" tmp
  tmp="$(mktemp)"
  printf '%s\n' "$content" > "$tmp"
  sudo install -D -m 0644 "$tmp" "$dst"
  rm -f "$tmp"
}

enable_power_service() {
  sudo systemctl daemon-reload
  sudo systemctl enable --now sigint101-power.service
}

restart_power_service() {
  sudo systemctl daemon-reload
  sudo systemctl restart sigint101-power.service
}

# Installs the non-root low-battery power watcher on legacy (non-managed) boxes. Never creates
# or overwrites /etc/sigint101/power.conf, and respects any non-enabled owner unit state.
step_power() {
  step "Low-battery power watcher (sigint101-power.service; ships disarmed)"
  local updater_root="${SIGINT101_UPDATER_ROOT:-$HOME/.local/state/sigint101-updater}"
  if [ -e "$updater_root/current.json" ] || [ -e "$updater_root/control.json" ]; then
    warn "skip: managed box, the updater owns the watcher"
    return 0
  fi

  local d="$COURSE_DIR/deploy/system" p="$COURSE_DIR/sigint101"
  local req_files=(
    "$p/__init__.py"
    "$p/battery.py"
    "$p/power.py"
    "$d/sigint101-power.service"
    "$d/sigint101-power"
    "$d/50-sigint101-power.rules"
    "$d/sigint101-power.sysusers.conf"
  )
  local f
  for f in "${req_files[@]}"; do
    if [ ! -f "$f" ]; then
      if [ "$CHECK" = 1 ] && [ ! -d "$COURSE_DIR/.git" ]; then
        would "install sigint101-power once the course is cloned"
        return 0
      fi
      warn "power watcher source $f is missing; skipping power watcher"
      return 0
    fi
  done

  local unit_dst="/etc/systemd/system/sigint101-power.service"
  local unit_existed=0 changed=0 unit_changed=0
  if cmp -s "$unit_dst" "$unit_dst" 2>/dev/null; then
    unit_existed=1
  fi

  local pairs=(
    "0644|$d/sigint101-power.sysusers.conf|/usr/local/lib/sysusers.d/sigint101-power.conf|power watcher sysusers"
    "0644|$p/__init__.py|/usr/local/lib/sigint101-power/sigint101/__init__.py|power watcher package init"
    "0644|$p/battery.py|/usr/local/lib/sigint101-power/sigint101/battery.py|power watcher battery helper"
    "0644|$p/power.py|/usr/local/lib/sigint101-power/sigint101/power.py|power watcher module"
    "0755|$d/sigint101-power|/usr/local/bin/sigint101-power|power watcher launcher"
    "0644|$d/50-sigint101-power.rules|/etc/polkit-1/rules.d/50-sigint101-power.rules|power watcher polkit rule"
    "0644|$d/sigint101-power.service|$unit_dst|power watcher unit"
  )
  local entry mode src dst label
  for entry in "${pairs[@]}"; do
    IFS='|' read -r mode src dst label <<< "$entry"
    if cmp -s "$src" "$dst" 2>/dev/null; then
      ok "$dst"
    else
      changed=1
      [ "$dst" != "$unit_dst" ] || unit_changed=1
      apply "install $label ($dst)" sudo install -D -m "$mode" "$src" "$dst"
    fi
  done

  local have_user=0
  if command -v getent >/dev/null && getent passwd sigint101-power >/dev/null 2>&1; then
    have_user=1
  elif ! command -v getent >/dev/null && grep -q '^sigint101-power:' /etc/passwd 2>/dev/null; then
    have_user=1
  fi
  if [ "$have_user" = 1 ]; then
    ok "system user sigint101-power"
  else
    apply "create system user sigint101-power" ensure_power_user
  fi

  local grp="" gid="" dropin_dst="/etc/systemd/system/sigint101-power.service.d/10-i2c-group.conf"
  if command -v stat >/dev/null; then
    grp="$(stat -c %G /dev/i2c-1 2>/dev/null)" || grp=""
  fi
  if [ -z "$grp" ]; then
    warn "I2C not enabled; /dev/i2c-1 missing, the watcher cannot read"
  elif [ "$grp" = "i2c" ]; then
    ok "/dev/i2c-1 group is i2c"
  else
    if command -v getent >/dev/null; then
      gid="$(getent group "$grp" 2>/dev/null | cut -d: -f3)" || gid=""
    fi
    if [ "$grp" = "root" ] || [[ ! "$gid" =~ ^[0-9]+$ ]] || (( gid < 100 )); then
      warn "/dev/i2c-1 group '$grp' (gid ${gid:-unknown}) refused; leaving unit without a group drop-in"
    else
      local dropin_content
      dropin_content="$(printf '[Service]\nSupplementaryGroups=\nSupplementaryGroups=%s\n' "$grp")"
      if printf '%s\n' "$dropin_content" | cmp -s - "$dropin_dst" 2>/dev/null; then
        ok "$dropin_dst"
      else
        changed=1
        unit_changed=1
        apply "install I2C group drop-in ($dropin_dst for $grp)" \
          install_power_dropin "$dropin_dst" "$dropin_content"
      fi
    fi
  fi

  if [ "$unit_existed" = 0 ]; then
    apply "enable and start sigint101-power.service" enable_power_service
  else
    local state
    state="$(systemctl is-enabled sigint101-power.service 2>/dev/null || true)"
    if [ "$state" != "enabled" ]; then
      if [ "$unit_changed" = 1 ]; then
        apply "reload systemd units" sudo systemctl daemon-reload
      fi
      skip "sigint101-power.service turned off by the owner (${state:-unknown})"
    elif [ "$changed" = 1 ]; then
      apply "reload and restart sigint101-power.service" restart_power_service
    else
      ok "sigint101-power.service enabled and up to date"
    fi
  fi
}

# FCC grant 2ALPH-E22900M22S (2021-10-29), Ebyte E22-900M22S: 0.0076 W conducted
# (about 8.8 dBm). Use 8 dBm as the provisioning ceiling for US Long Turbo.
readonly MESH_TX_POWER_CEILING_DBM=8
readonly MESH_TX_RESTORE_S=30

validate_mesh_tx_power() {
  local name="$1" value="$2"
  # Validate decimal syntax before arithmetic, then use the SX1262 range.
  if [[ "$value" =~ ^-?[1-9][0-9]?$ ]] &&
     (( value >= -9 && value <= 22 )); then
    return 0
  fi
  die "$name must be a nonzero whole number from -9 to 22 dBm (SX1262 range, 0 uses the radio default)"
}

read_mesh_lora() {
  local settings
  settings="$("$@" --get lora.region --get lora.modem_preset --get lora.use_preset \
                   --get lora.tx_power --get lora.tx_enabled)" || return 1
  # The CLI prints enum numbers and Python's True/False for individual preferences.
  awk '
    /^lora\.region: [0-9]+$/ { region = $2 }
    /^lora\.modem_preset: [0-9]+$/ { preset = $2 }
    /^lora\.use_preset: (True|False)$/ { use = $2 }
    /^lora\.tx_power: (0|-?[1-9][0-9]*)$/ && length($2) <= 11 { power = $2 }
    /^lora\.tx_enabled: (True|False)$/ { enabled = $2 }
    END {
      if (region == "" || preset == "" || use == "" || power == "" || enabled == "") exit 1
      print region, preset, use, power, enabled
    }
  ' <<< "$settings"
}

step_meshtastic_defaults() {
  step "Meshtastic LoRa defaults (M11 LoRa, M28 Meshtastic)"
  local ceiling="$1" requested="$2"
  local mesh="$COURSE_DIR/.venv/bin/meshtastic"
  local marker="$HOME/.local/state/sigint101/meshtastic-defaults"
  local tx_pending="$marker-tx-off-pending"
  local cli=("$mesh" --host 127.0.0.1 --timeout 5)
  local restore_hint="Transmit may be on now. Rerun provisioning to turn it back off."
  local settings region preset use power enabled region_name preset_name power_name read_error=""
  if ! systemctl --quiet is-active meshtasticd 2>/dev/null; then
    warn "meshtasticd is not running; the LoRa lessons read its API on port 4403"
    if [ -d "$tx_pending" ]; then
      warn "TX restoration pending. Transmit may be on when meshtasticd starts. Rerun provisioning then."
    fi
    return 0
  elif [ ! -x "$mesh" ]; then
    read_error="the course venv has no meshtastic CLI; LoRa settings not checked"
  elif ! settings="$(read_mesh_lora timeout 15 "${cli[@]}" 2>/dev/null)"; then
    read_error="could not read LoRa settings from local meshtasticd"
  fi
  if [ -n "$read_error" ]; then
    [ ! -d "$tx_pending" ] || die "$read_error. TX restoration pending. $restore_hint"
    warn "$read_error"
    return 0
  fi
  read -r region preset use power enabled <<< "$settings"
  # Values from Meshtastic's LoRaConfig.RegionCode and ModemPreset enums.
  region_name="$region"
  case "$region" in 0) region_name=UNSET ;; 1) region_name=US ;; esac
  local presets=(LONG_FAST LONG_SLOW VERY_LONG_SLOW MEDIUM_SLOW MEDIUM_FAST SHORT_SLOW SHORT_FAST
                 LONG_MODERATE SHORT_TURBO LONG_TURBO LITE_FAST LITE_SLOW NARROW_FAST NARROW_SLOW)
  preset_name="$preset"
  if [[ "$preset" =~ ^([0-9]|1[0-3])$ ]]; then
    preset_name="${presets[preset]}"
  fi
  power_name="$power dBm"
  [ "$power" != 0 ] || power_name="radio default (0)"
  ok "meshtasticd LoRa region $region_name, modem preset $preset_name (use preset: $use), configured tx_power $power_name"

  # Automatic power can exceed the ceiling. Otherwise preserve power unless explicitly requested.
  local target_power="$power"
  if [ -n "$requested" ]; then
    target_power="$requested"
  elif [ "$power" = 0 ] || (( power > ceiling )); then
    target_power="$ceiling"
  fi

  # Region and preset are one-time defaults.
  local changes=() target_profile="$region $preset $use"
  local goal="tx_power $target_power dBm" action="tx_power to $target_power dBm"
  if [ ! -d "$marker" ]; then
    if [ "$target_profile" != "1 9 True" ]; then
      changes=(--set lora.region US --set lora.modem_preset LONG_TURBO --set lora.use_preset true)
      target_profile="1 9 True"
    fi
    goal="US / LONG_TURBO with $goal"
    action="defaults to US / LONG_TURBO"
    [ "$power" = "$target_power" ] || action="defaults to $goal"
  fi
  [ "$power" = "$target_power" ] || changes+=(--set lora.tx_power "$target_power")

  # On the first region write the firmware enables TX even if it was off. Keep the
  # original off state across a failed restoration or a restart between the writes.
  local target_enabled="$enabled" restore_tx=0
  [ ! -d "$tx_pending" ] || target_enabled=False
  if [ "$target_enabled" = False ] &&
     { [ "$enabled" = True ] || { [ "$region" = 0 ] && [ ! -d "$marker" ]; }; }; then
    restore_tx=1
  fi
  if [ "$CHECK" = 1 ]; then
    [ "${#changes[@]}" -eq 0 ] || would "set local meshtasticd LoRa $action"
    if [ "$restore_tx" = 1 ]; then
      if [ "$enabled" = True ]; then
        would "turn transmit back off (transmit is on now)"
      else
        would "turn transmit back off (firmware may briefly enable transmit when setting the region)"
      fi
    fi
    return 0
  fi

  if [ "$restore_tx" = 1 ]; then
    mkdir -p "$tx_pending"
  fi
  if [ "${#changes[@]}" -ne 0 ]; then
    timeout 15 "${cli[@]}" "${changes[@]}" >/dev/null 2>&1 || true
  fi
  if [ "$restore_tx" = 1 ]; then
    local deadline=$(( SECONDS + MESH_TX_RESTORE_S )) remaining attempted_restore=0
    # The first region write can restart meshtasticd. Read a fresh, set region before each
    # restore because the CLI writes the whole LoRa config. Keep trying within one time budget.
    while (( (remaining = deadline - SECONDS) > 0 )); do
      settings="$(read_mesh_lora timeout -k 1 "$remaining" "${cli[@]}" 2>/dev/null)" ||
        settings=""
      case "$settings" in
        [1-9]*" False") break ;;
        [1-9]*" True")
          if (( (remaining = deadline - SECONDS) > 0 )); then
            attempted_restore=1
            timeout -k 1 "$remaining" "${cli[@]}" --set lora.tx_enabled false >/dev/null 2>&1 || true
          fi ;;
      esac
      if (( SECONDS < deadline )); then
        sleep 1
      fi
    done
    [[ "$settings" == [1-9]*" False" ]] ||
      die "could not confirm lora.tx_enabled False on meshtasticd. TX restoration pending. $restore_hint"
    [ "$attempted_restore" = 0 ] || did "restore local meshtasticd LoRa tx_enabled to false"
  elif [ "${#changes[@]}" -ne 0 ]; then
    settings="$(read_mesh_lora timeout 15 "${cli[@]}" 2>/dev/null)" || settings=""
  fi
  if [ -n "$settings" ] && [ "${settings##* }" != "$target_enabled" ]; then
    if [ "$target_enabled" = False ]; then
      die "could not confirm lora.tx_enabled False on meshtasticd. Transmit may be on now. Check the node before use."
    fi
    die "could not confirm lora.tx_enabled $target_enabled on meshtasticd. Check the node before use."
  fi

  [ ! -d "$tx_pending" ] || rm -d -- "$tx_pending"
  if [ "$settings" != "$target_profile $target_power $target_enabled" ]; then
    warn "could not confirm $goal on meshtasticd (retry on the next run)"
    return 0
  fi
  [ "${#changes[@]}" -eq 0 ] || did "set local meshtasticd LoRa $action"
  if [ ! -d "$marker" ]; then
    mkdir -p "$marker"
  fi
}

step_stock_report() {
  step "Stock Recon pieces the course relies on (reported, never changed)"
  if command -v vcgencmd >/dev/null; then
    ok "vcgencmd (power/throttle provenance)"
  else
    warn "no vcgencmd; the throttle readout will say it can't read"
  fi
  local usb=""
  if command -v lsusb >/dev/null; then
    usb="$(lsusb 2>/dev/null)" || usb=""
  fi
  if [[ "$usb" == *"0bda:2838"* ]]; then
    ok "RTL2832U radio on the USB bus"
  else
    warn "no RTL2832U (0bda:2838) on the USB bus"
  fi
}

# Hand-over hygiene: the box must carry nothing of the person who provisioned it.
step_handover_report() {
  step "Hand-over hygiene (reported, never changed)"
  if [ -n "$(git config --global user.email 2>/dev/null || true)" ]; then
    warn "a global git identity is set (git config --global --unset user.email / user.name)"
  else
    ok "no global git identity"
  fi
  local keys=0
  if [ -f "$HOME/.ssh/authorized_keys" ]; then
    keys="$(grep -c -v -e '^#' -e '^$' "$HOME/.ssh/authorized_keys" || true)"
  fi
  if [ "$keys" -gt 0 ]; then
    warn "$keys authorized SSH key(s) in ~/.ssh/authorized_keys; keep only the owner's"
  else
    ok "no authorized SSH keys"
  fi
  if command -v tailscale >/dev/null; then
    warn "tailscale is installed; log it out and remove it unless the owner wants it"
  else
    ok "no tailscale"
  fi
  if command -v nmcli >/dev/null; then
    local wifi
    wifi="$(nmcli -t -f TYPE connection show 2>/dev/null | grep -c '^802-11-wireless$' || true)"
    if [ "$wifi" -gt 0 ]; then
      warn "$wifi saved Wi-Fi network(s); delete any that aren't the owner's"
    else
      ok "no saved Wi-Fi networks"
    fi
  fi
}

usage() {
  cat <<'U'
usage: provision.sh [--check] [--with-optional] [--with-flock-adapter] [--branch NAME]
  --check               report what would change; change nothing
  --with-optional       also build dsd-fme, satdump and sstv (hours on a Pi 4)
  --with-flock-adapter  monitor mode at boot for the RTL8821CU adapter (needs a powered USB hub)
  --branch NAME         follow a course branch other than main

  SIGINT101_MESHTASTIC_TX_POWER_DBM      request an exact power at or below the ceiling
  SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM  use the module's conducted power from its own FCC grant

  The default ceiling is 8 dBm for the 500 kHz Long Turbo setup (Ebyte E22-900M22S,
  FCC ID 2ALPH-E22900M22S, 0.0076 W conducted). With no exact request, only automatic
  power (0) and values above the ceiling are changed. Both variables take nonzero
  whole numbers from -9 to 22 dBm, the SX1262 range.
U
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --check) CHECK=1 ;;
      --with-optional) OPTIONAL=1 ;;
      --with-flock-adapter) FLOCK_ADAPTER=1 ;;
      --branch)
        [ $# -ge 2 ] || die "--branch needs a name"
        COURSE_BRANCH="$2"
        shift
        ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "unknown option: $1" ;;
    esac
    shift
  done

  local mesh_ceiling="${SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM-$MESH_TX_POWER_CEILING_DBM}"
  local mesh_target="${SIGINT101_MESHTASTIC_TX_POWER_DBM-}"
  validate_mesh_tx_power SIGINT101_MESHTASTIC_TX_POWER_MAX_DBM "$mesh_ceiling"
  if [ "${SIGINT101_MESHTASTIC_TX_POWER_DBM+x}" = x ]; then
    validate_mesh_tx_power SIGINT101_MESHTASTIC_TX_POWER_DBM "$mesh_target"
    if (( mesh_target > mesh_ceiling )); then
      die "requested Meshtastic tx_power $mesh_target dBm exceeds the $mesh_ceiling dBm ceiling"
    fi
  fi

  step_preflight
  step_node
  step_desktop_autologin
  step_user_groups
  step_apt
  step_course
  step_venv
  step_builds
  step_optional_builds
  step_tcpdump_caps
  step_monitor_mode
  step_glyph_font
  step_kiosk
  step_power
  step_meshtastic_defaults "$mesh_ceiling" "$mesh_target"
  step_stock_report
  step_handover_report

  printf '\n'
  if [ "$CHECK" = 1 ]; then
    echo "check: $CHANGES change(s) pending, $WARNINGS warning(s); nothing was changed"
  else
    echo "provision: $CHANGES change(s) made, $WARNINGS warning(s)"
    [ "$CHANGES" -eq 0 ] || echo "provision: reboot to bring the course up on the panel"
  fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
