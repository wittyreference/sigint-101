# ABOUTME: Serves the render check's scratch console on a loopback socket the check hands it.
# ABOUTME: Its own data folder and no radio: the RTL-SDR probe reports none and ppm refuses.

import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import uvicorn  # noqa: E402

from sigint101.app import create_app  # noqa: E402
from sigint101.hardware import RadioProbe  # noqa: E402
from sigint101.ppm import PpmUnavailable  # noqa: E402

RENDER_CHECK = None

# The console has no switch that turns its hardware off, so the scratch one is built without it:
# every RTL-SDR receiver, the tune and gain routes and the bus survey ask this probe first, and
# the ppm capture, which does not, is handed a measurement that refuses. The LoRa, Flipper, Flock
# and Rayhunter devices are reached only through /api/*, which the checks block.
NO_RADIO = "the render check's scratch console never uses a radio"


def no_radio():
    return RadioProbe(False, "render check", NO_RADIO)


def no_devices():
    return ()


def no_ppm(**_):
    raise PpmUnavailable(NO_RADIO)


def main(fd, data_dir):
    # The check bound this socket before starting us, so no other server can hold the port it
    # waits on. Built on the socket, uvicorn binds nothing of its own.
    listener = socket.socket(fileno=int(fd))
    app = create_app(data_dir=data_dir, probe=no_radio, list_devices=no_devices,
                     ppm_measure=no_ppm)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])


if __name__ == "__main__":
    main(*sys.argv[1:3])
