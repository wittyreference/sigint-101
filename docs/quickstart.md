# SIGINT 101: quick start

## Turn it on
Power on the Recon. In a minute or two the Raspberry Pi desktop starts and the course opens
full-screen on top of it, by itself. Everything is driven by touch. **Back** is in the status bar at
the top of every screen except home, and the home screen lists the units in order.

**The check.** Each lesson asks you to predict before you watch, and that call is never scored.
Unit 1's lessons then end on a one-question **check**: a new situation on the same idea, scored
Right or Not yet with the reason, and **Complete lesson** appears once you answer it.

## Leaving the course, and coming back
- **Exit.** On the home screen, the top-left control is **Exit SIGINT 101**. Tap it to close the
  course window. You are left on the Pi desktop, and your progress is kept.
- **The desktop.** Everything a Pi desktop has is there:
  - the menu (top left), with a terminal, the file manager and **Shutdown**;
  - the network menu (top right, the Wi-Fi icon).
- **Joining Wi-Fi.**
  1. Tap the Wi-Fi icon at the top right and pick your network.
  2. The on-screen keyboard comes up for the password. If it doesn't, tap the keyboard icon in the
     top panel.
  3. Type the password and connect.
- **Back to the course.** Double-tap the **SIGINT 101** icon on the desktop, or pick **SIGINT 101**
  from the menu (under Education). It opens full-screen again.
- **If the course crashes,** it reopens by itself within about ten seconds. After you Exit, it stays
  closed until you open it again or restart the box. A restart always comes back into the course.

**Before the box is powered, put an antenna on the LoRa jack, and leave it on.** The LoRa radio
runs Meshtastic and can transmit by itself, and transmitting into an open jack can damage it.

## What's in the box
- **A Raspberry Pi 4** running Debian Bookworm. That is the computer, and the course runs on it.
- **The built-in RTL-SDR receiver** (RTL2832U, about 24 MHz to 1.7 GHz). It is the radio behind
  the waterfall and most of the decoders.
- **The LoRa HAT** (900 MHz) running `meshtasticd`. It is a Meshtastic node of its own.
- **The 800×480 touch panel.**

The course never draws a signal it didn't receive. An empty band is the honest answer when
nothing is on the air, or when the antenna doesn't suit the band.

## Themes
Tap the theme button in the status bar to cycle **dark → sunlight → red-night**. Sunlight is
for outdoors. Red-night keeps your night vision. The box remembers your choice.

## Licence exam trainer
Tap **Licence exam** under the unit grid on the home screen. It has study cards drawn from the
Technician question pool, and a 35-question practice exam. Receiving is open to anyone, but
transmitting on most bands takes a licence, and this is the on-ramp to one.

## Kit pieces some lessons use
| Piece | Lessons |
|---|---|
| Telescopic dipole | M6–M7 antennas. It also helps FM reception for M4 and M12 |
| RTL-SDR Blog V3 (1 ppm clock) | Optional. M4 explains why its clock would read near zero; the console itself uses the built-in radio |
| Flipper Zero (use a **data** USB cable) | M9 on-off keying |
| USB Wi-Fi adapter (RTL8821CU) + a **self-powered USB hub** | M18 Flock detector. Plug the adapter into the powered hub, never straight into the Recon: the Recon's own ports can't feed it in monitor mode and brown out, which resets the touch panel too. Then run `provision.sh --with-flock-adapter` once, and it switches to listen-only monitor mode by itself |
| Orbic hotspot running Rayhunter | M19 IMSI-catcher detection. Power it on and join the box's Wi-Fi to the Orbic's network; the lesson reads its report from there |
| A second Meshtastic node | M11 LoRa and M28 Meshtastic need someone to hear |
| An antenna for the band | ADS-B (1090 MHz), APRS (2 m), ISS SSTV (437.550 MHz), NOAA APT (137 MHz), AIS (marine VHF), pager framing (929 MHz). A lesson says when it needs one |

## Set it up for your site
A few lessons need facts about where the box lives. Set them once, at provisioning. Each one has a
default, but the defaults describe the place this course was built, not yours.

| Variable | What it is | Default | Used by |
|---|---|---|---|
| `SIGINT101_RDS_HZ` | Your strongest local FM station, in Hz (e.g. `94700000`) | `106500000` | M12: the station redsea decodes RDS from |
| `SIGINT101_REF_HZ` | A steady local FM station, in Hz: the reference a baseline is measured against | `106500000` | M3: the provenance panel's reference level |
| `SIGINT101_RX_LAT`, `SIGINT101_RX_LON` | The box's latitude and longitude, in decimal degrees | not set | M13: places aircraft on the radar by range and bearing |

- **Finding your stations.** Open Lesson 1 and look at the FM band. The brightest stripe is your
  strongest station. Tap it, or step to it with − and +, and read its frequency off the dial. Pick
  a station that stays bright night after night for the reference. Both must be real US FM
  channels: 88.1 to 107.9 MHz on an odd tenth, such as 94.7. Any other value is ignored, and the
  lesson's dial shows which station is actually in use.
- **Without a location,** the ADS-B scope says "receiver location not set" and still counts
  aircraft. It never guesses where you are.

To set them, add a drop-in to the lesson server. It survives updates:

```
systemctl --user edit sigint101
```

In the editor that opens, add:

```
[Service]
Environment=SIGINT101_RDS_HZ=94700000
Environment=SIGINT101_REF_HZ=94700000
Environment=SIGINT101_RX_LAT=40.7128
Environment=SIGINT101_RX_LON=-74.0060
```

Save it, then run `systemctl --user restart sigint101`.

## Getting a terminal
Exit the course and open **Terminal** from the desktop menu, or SSH in. The course lives in
`~/sigint-101`.

## Updating
Connect the box to a network first, then run:

```
~/sigint-101/deploy/provision.sh --check   # preview what would change
~/sigint-101/deploy/provision.sh           # pull the latest course and apply it
sudo reboot
```

The script can be re-run safely, and it skips anything already in place. Your progress lives in
`~/sigint-101/data` and survives updates.

Three decoders are not installed by default: digital voice (M27), NOAA APT (M24) and SSTV
(M25). Until they are, those lessons say so. To build them, run
`~/sigint-101/deploy/provision.sh --with-optional` (expect a few hours on a Pi 4).

## When something's off
- **The panel is blank or shows an error:** run `systemctl --user restart sigint101`. The panel
  reconnects on its own.
- **Logs:** run `journalctl --user -u sigint101 -b`. The journal is kept across reboots.
- **Powering off:** exit the course and use **Shutdown** in the desktop menu (or run
  `sudo poweroff`) before cutting power.
