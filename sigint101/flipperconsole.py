# ABOUTME: The kiosk-facing Flipper read layer — composes probe_flipper + FlipperCli + the parsers.
# ABOUTME: Reads only; presence/absence/ambiguity and the no-mock refusal come from the transport.

from __future__ import annotations

import glob as _glob

from .deviceinfo import parse_device_info
from .flipper import FlipperCli, FlipperError, probe_flipper

# A capability command issued while an app is open on the Flipper comes back with this phrase
# instead of data. Reading it as a result would show the learner a reading the device never took,
# so the console turns it into an honest refusal — the same class as "no device".
APP_OPEN_MARKER = "cannot be run while an application is open"


class FlipperConsole:
    """What the kiosk routes call to read the attached Flipper.

    The transport (`FlipperCli`, `probe_flipper`) owns the port, the by-id resolution and the
    refusals; this layer only decides which command to run and which parser reads the reply. It
    holds no port of its own — each read opens and releases one short `FlipperCli` session, so a
    lesson the learner is working is never locked out by a stale handle.
    """

    def __init__(self, *, probe=probe_flipper, cli_factory=FlipperCli, glob=_glob.glob):
        self._probe = probe
        self._cli_factory = cli_factory
        self._glob = glob

    def device(self) -> dict:
        """The attached Flipper's identity, or an honest refusal.

        Absence is a `FlipperError`; two Flippers on the bus is a `FlipperSelectionError` raised by
        the probe itself (never a guess). An open app is a `FlipperError` rather than parsed data.
        """
        if not self._probe(glob=self._glob).present:
            raise FlipperError("no Flipper attached")
        with self._cli_factory() as cli:
            reply = cli.command("device_info")
        if APP_OPEN_MARKER in reply:
            raise FlipperError("an app is open on the Flipper; return to the desktop and retry")
        return parse_device_info(reply).as_dict()
