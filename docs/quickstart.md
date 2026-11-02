# SIGINT 101: quick start

## Turn it on
Power on the Recon. In a minute or two the course comes up full-screen on the panel by itself.
Everything is driven by touch. **Back** is always in the status bar at the top, and the home
screen lists the units in order.

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
| RTL-SDR Blog V3 (1 ppm clock) | M4: the reference that shows where frequency error comes from |
| Flipper Zero (use a **data** USB cable) | M9 on-off keying |
| USB Wi-Fi adapter (RTL8821CU) | M18 Flock detector. Plug it in and it switches to listen-only monitor mode by itself |
| Orbic hotspot running Rayhunter | M19 IMSI-catcher detection. Power it on and join the box's Wi-Fi to the Orbic's network; the lesson reads its report from there |
| A second Meshtastic node | M11 LoRa and M28 Meshtastic need someone to hear |
| An antenna for the band | ADS-B (1090 MHz), APRS and SSTV (2 m), NOAA APT (137 MHz), AIS (marine VHF). A lesson says when it needs one |

## Getting a terminal
SSH in. Or plug in a keyboard and press **Ctrl+Alt+F2** to log in on a text console;
**Ctrl+Alt+F1** goes back to the panel. The course lives in `~/sigint-101`.

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
- **Powering off:** run `sudo poweroff` before cutting power. The course has no shutdown button
  yet.
