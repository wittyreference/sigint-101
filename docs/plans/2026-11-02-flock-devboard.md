# Flock capture on the Flipper Wi-Fi devboard (plan, 2026-11-02)

**Why.** The RTL8821CU USB dongle can't run on the Nomad's own USB. In monitor mode it browned
out the bus: 192 over-current events in two minutes, and the touch panel reset 23 times (see
learnings). MC asked to use the Flipper's Wi-Fi devboard as the monitor radio instead and drop
the dongle.

**MC's calls, verbatim (2026-11-02):**
- the path: **"Through the Flipper's UART bridge"**;
- this swap is added to the 11-08 cut line.

**Shape.** The devboard (ESP32-S2) runs ESP32 Marauder and sniffs 2.4 GHz management frames.
The devboard sits on the Flipper's GPIO. The Flipper runs its built-in GPIO → **USB-UART
Bridge** on USB channel 1, so nomad-b sees the ESP32's serial as a second CDC port, with the
Flipper CLI staying on the first.

A new capture source in `sigint101/flock.py` reads Marauder's raw-frame stream and feeds the
**same** `parse_management_frame → match_signatures` detector that already exists. This keeps
MC's 10-29 call: our algorithm on our radio, never another device's verdicts.

## Steps
1. **Probe (read-only).** With the Flipper in the USB-UART bridge, identify the devboard's
   current firmware: Blackmagic as shipped, or Marauder already.
2. **Back up (checksummed).** If a flash is needed, read the whole ESP32-S2 flash with
   `esptool read_flash`, record its sha256, and keep it in the scratchpad, plus its
   path/hash in this doc. That backup is the rollback.
3. **Flash.** Use the official ESP32 Marauder release for the Flipper Wi-Fi devboard (S2),
   pinned by version and sha256.
   - Download mode needs a physical step: hold BOOT while tapping RESET on the devboard. That
     is MC's one tap.
   - Confirm afterwards: Marauder's `info`/version over the bridge.
4. **Capture source (TDD).**
   - Capture real Marauder raw-frame output from the air on nomad-b first, and use it as the
     fixture.
   - Parse Marauder's serial pcap framing (LINKTYPE 802.11, no radiotap) into frames.
   - Hop channels the way the detector needs.
   - Report honestly:
     - no bridge → an honest "no monitor radio" state;
     - bridge but no Marauder → a stopped state with the reason;
     - frames flowing → live.
   - Keep the tcpdump/wlan1 source as an alternative. Select the source at provisioning.
5. **Lesson and kit.**
   - M18's copy names the devboard and the bridge step: tap GPIO → USB-UART Bridge on the
     Flipper.
   - In the quick-start kit table, the devboard replaces the dongle and the powered hub.
   - `provision.sh` gains nothing system-level for this path, since there is no
     NetworkManager or udev work.
6. **HIL on nomad-b.**
   - Real management frames flow from the air into `/api/flock`.
   - The over-current count stays flat for the whole run.
   - The panel renders at 800x480.

## Step 4 as built (2026-11-03, branch `flock-marauder`)

**Status:** the code and tests are done, with no hardware. The fixture is **constructed from
Marauder's source, not captured**: the 63 real probe requests in `tests/fixtures/flock_ch1.pcap`,
wrapped as `sniffprobe -serial` would emit them. A real on-air capture through the bridge replaces
it at HIL (step 6). Nothing here has run against the devboard.

**Confirmed from Marauder's source** (justcallmekoko/ESP32Marauder, tag `v1.20.0` = `511bb36`,
`esp32_marauder/`):
- **Serial pcap.** `-serial` on a scan command sets `save_serial`, but only while nothing is
  scanning (`CommandLine.cpp:759-761`). `startPcap` then opens the buffer with no filesystem when
  there is no SD card (`WiFiScan.cpp:3794-3803`). Each main-loop pass (`esp32_marauder.ino:552`,
  then `delay(50)` on a screenless board, `:584`) sends the whole buffer as ONE `Serial.write`
  between `[BUF/BEGIN]` and `[BUF/CLOSE]` (`Buffer.cpp:228-267`). Console text falls between
  chunks. A record is never split across chunks (`Buffer.cpp:96-120`).
- **Header and records.** The global header is magic `a1b2c3d4` (little-endian), version 2.4,
  snaplen `SNAP_LEN`, **linktype 105, bare 802.11, no radiotap** (`PcapHeader.cpp:20-29`). It
  is written once, when the pcap opens (`Buffer.cpp:38-51`), so the first chunk carries it even
  before any frame. The Flipper build (`MARAUDER_FLIPPER`, PSRAM on: `build_parallel.yml:27`,
  `configs.h:517-532`) gets `BUF_SIZE` 8 KiB and `SNAP_LEN` 4096 (`configs.h:3373-3375`). A
  record is `{secs, usecs, len, len}` from `micros()` and then the payload (`Buffer.cpp:122-139`).
  For management frames that is `len = sig_len - 4`, so the FCS is dropped (`WiFiScan.cpp:9013`).
- **The command.** `sniffprobe` buffers only probe requests (first octet `0x40`), and prints a
  console line for each one (`WiFiScan.cpp:9088-9144`). `sniffbeacon` buffers beacons only
  (`:9038-9086`). `sniffraw` buffers every management AND data frame (`:9149-9177`, filter
  `WiFiScan.h:526`). We use `sniffprobe -serial`.
- **Channel and stop.** `channel -s N` sets the channel, and works mid-scan
  (`CommandLine.cpp:642-652`). Boot default is channel 1 (`WiFiScan.h:1022`). Hopping happens only
  if the `ChanHop` setting is on (`WiFiScan.cpp:10997-11006`). It is off by default
  (`settings.cpp:583-585`), and when on, it moves one channel a second (`configs.h:3538`).
  `stopscan` stops any scan and prints "Stopping WiFi tran/recv" (`CommandLine.cpp:374-392`).
- **The link.** Marauder's console is `Serial.begin(115200)` (`esp32_marauder.ino:336`). On the
  Flipper (firmware 1.5.1), the bridge's default baud is "Host", so our termios speed sets the
  Flipper's UART (`gpio_scene_usb_uart.c:37`, `usb_uart_bridge.c:146-158`). **Its default USB
  channel is 0** (`gpio_scene_usb_uart.c:33`). Channel 1 switches to dual CDC and puts the bridge
  on USB interface 2 (`usb_uart_bridge.c:107-115`, `furi_hal_usb_cdc.c:294-305`), which is
  `/dev/serial/by-id/*Flipper*-if02`.

**What the code does** (`sigint101/flock.py`):
- `iter_pcap_frames` reads the linktype. 127 strips radiotap, 105 passes 802.11 through, and
  anything else is a ValueError. The tcpdump path is locked byte-for-byte by a sha256 of the
  frames `flock_ch1.pcap` yielded before the change.
- `iter_marauder_frames` pulls the pcap out of the console stream. It reads by record length, so a
  marker inside an SSID cannot cut a record. It drops a false start (a probe for an SSID named
  `[BUF/BEGIN]`) back to scanning, and skips chunks heard before any global header.
- `MarauderFlockDetector` opens the port raw at 115200 with the stdlib only (`os`, `termios`,
  `tty`, `select`; no pyserial). It types `stopscan`, `channel -s 1` and `sniffprobe -serial`, and
  sends `stopscan` on `stop()`. The frames go through the same `parse_management_frame` →
  `match_signatures` → roster as wlan1's, never through Marauder's verdicts. `live_command()` is
  `sniffprobe -serial`.
- **Selection** (read like the site's FM stations):
  - `SIGINT101_FLOCK_SOURCE=marauder` picks the devboard; unset or anything else keeps
    tcpdump/wlan1, the default.
  - `SIGINT101_MARAUDER_TTY` pins the port. It defaults to the by-id glob
    `/dev/serial/by-id/*Flipper*-if02`, not a ttyACM number, because ACM numbers move with plug
    order and the Nomad has other CDC devices. Two matches are refused, not guessed.
- **Honest states:**
  - No port → "no monitor radio", naming GPIO → USB-UART Bridge, USB Channel 1.
  - A port, but no pcap chunk within 5 s → STOPPED, "no answer from Marauder on <port>".
  - Link closed → STOPPED with the errno. When the port vanishes with the bridge, the next poll
    says "no monitor radio".
  - The devboard reboots mid-sniff (its "ESP-IDF version is:" boot line) → STOPPED. Otherwise a
    rebooted, idle board would read LIVE on a quiet link.
  - A stream it cannot read → STOPPED, after sending `stopscan`.
  - It reads STARTING until the first chunk arrives, then LIVE.
  - `childproc.exit_reason` now uses a stage's own `end_reason`, so a serial link never shows an
    invented "exit status".

**Where it differs from wlan1:**
- **Frames.** tcpdump keeps every management frame (`type mgt`); `sniffprobe` keeps only probe
  requests. Tiers 4, 3 and 0 work as before. Tier 2 fires only on probe requests, and tier 1 (a
  Flock receiver or BSSID) cannot fire at all.
- **Why probe-only.** The link is 115200 baud, about 11.5 KB/s. While a chunk is being written,
  Marauder drops every new frame (`Buffer.cpp:100-111`). A beacon or `sniffraw` stream in a busy
  area would saturate the link and drop frames at random, the probe requests included.
- **Channel.** There is no hopping, as on wlan1 (which never sets a channel, and the fixture is
  channel 1). The devboard is pinned to channel 1. A client that scans actively sends probe
  requests on every channel, so one channel still hears it. Beacon-only devices on other channels
  are missed, which is the lesson's "a channel you are not on".

**Still to do** (deferred: another agent is editing lesson copy):
- M18's copy: name the devboard and the bridge step (GPIO → USB-UART Bridge, **USB Channel 1**).
- The quick-start kit table: the devboard replaces the dongle and the powered hub.
- Rows in the quick-start's environment table for `SIGINT101_FLOCK_SOURCE` and
  `SIGINT101_MARAUDER_TTY`.

**NOT VERIFIED (needs the device, step 6):**
- that the bridge's port is `-if02` on nomad-b;
- that Marauder streams over the bridge as its source says;
- the 5 s answer budget;
- the boot-line detection;
- real frames from the air into `/api/flock`;
- the over-current count;
- the panel at 800x480 with this source.
