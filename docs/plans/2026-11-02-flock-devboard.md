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
