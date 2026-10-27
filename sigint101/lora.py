# ABOUTME: Reads LoRa packets from the local meshtasticd API for M11, with the SNR each decoded at.
# ABOUTME: No meshtasticd, no radio, no packet — this reports absence, it never invents a reception.

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

# meshtasticd's default TCP API. The Nomad's own LoRa radio (a MeshAdv sx1262) is driven by the
# meshtasticd service; the console reads what it already received rather than touching the radio,
# so the LoRa lesson and the SDR lessons never contend for a device.
DEFAULT_LORA_HOST = "localhost"
DEFAULT_LORA_PORT = 4403
DEFAULT_HISTORY = 40


class LoRaUnavailable(RuntimeError):
    """The LoRa radio cannot be read. Never a reason to synthesise a packet — the panel says so."""


@dataclass(frozen=True)
class LoRaPacket:
    """One packet the Nomad's LoRa radio received, and the SNR it decoded it at.

    `below_floor` is the whole lesson: LoRa's chirp spreads the signal so far that a packet decodes
    cleanly at a negative SNR — below the thermal noise floor — where a plain carrier would be lost.
    `summary` is the readable message when the packet is on our channel, or an honest note that it
    is not; it is never invented text.
    """

    ts: int | None
    from_id: str
    snr_db: float | None
    rssi_dbm: int | None
    portnum: str
    summary: str
    hop_limit: int | None
    below_floor: bool

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "from_id": self.from_id,
            "snr_db": self.snr_db,
            "rssi_dbm": self.rssi_dbm,
            "portnum": self.portnum,
            "summary": self.summary,
            "hop_limit": self.hop_limit,
            "below_floor": self.below_floor,
        }


def parse_lora_packet(packet) -> LoRaPacket | None:
    """Turn a meshtastic MeshPacket dict into a LoRaPacket, or None if it is not a reception.

    A packet is a reception only when it carries a sender (`from`/`fromId`). A packet with no
    `decoded` block is a real reception on a channel we cannot read — reported as ENCRYPTED with an
    honest note, never decoded into invented text.
    """
    if not isinstance(packet, dict):
        return None
    if "from" not in packet and "fromId" not in packet:
        return None

    from_id = packet.get("fromId")
    if not from_id:
        num = packet.get("from")
        from_id = f"!{num:08x}" if isinstance(num, int) else str(num)

    snr = packet.get("rxSnr")
    snr_db = float(snr) if isinstance(snr, (int, float)) else None
    rssi = packet.get("rxRssi")
    rssi_dbm = int(rssi) if isinstance(rssi, (int, float)) else None

    # A genuine over-the-air reception always carries the SNR the radio measured it at. A packet
    # with no rxSnr is locally originated — the node's own telemetry/nodeinfo looping through the
    # API — not received from the air, and the whole lesson is about that SNR, so it is not a
    # reception to show. Skipping it keeps the panel to real receptions, never the node itself.
    if snr_db is None:
        return None

    decoded = packet.get("decoded")
    if isinstance(decoded, dict) and decoded.get("portnum"):
        portnum = decoded["portnum"]
        text = decoded.get("text")
        if text:
            summary = text
        else:
            # Name the kind of packet rather than invent content (telemetry, position, nodeinfo...).
            summary = portnum.removesuffix("_APP").replace("_", " ").lower()
    else:
        portnum = "ENCRYPTED"
        summary = "(encrypted — not on our channel)"

    return LoRaPacket(
        ts=packet.get("rxTime"),
        from_id=from_id,
        snr_db=snr_db,
        rssi_dbm=rssi_dbm,
        portnum=portnum,
        summary=summary,
        hop_limit=packet.get("hopLimit"),
        below_floor=snr_db is not None and snr_db < 0,
    )


def _default_connect(host: str, port: int):
    """Open the meshtastic TCP interface to the local daemon. Imported lazily so the module (and its
    pure parser) load without the meshtastic library, and only a real connection pulls it in."""
    from meshtastic.tcp_interface import TCPInterface
    return TCPInterface(hostname=host, portNumber=port)


class LoRaListener:
    """Subscribes to meshtasticd and keeps a bounded buffer of recently received packets.

    A sibling of the Sweeper/Decoder in spirit — a long-lived connection feeding a buffer off the
    request path — but it reads an existing daemon rather than owning a device, so it never contends
    with the RTL-SDR and needs no release/resume.
    """

    def __init__(self, *, hostname=DEFAULT_LORA_HOST, port=DEFAULT_LORA_PORT,
                 history=DEFAULT_HISTORY, connect=_default_connect):
        self.hostname = hostname
        self.port = int(port)
        self._connect = connect
        self._packets = deque(maxlen=history)
        self._lock = threading.Lock()
        self._interface = None
        self._subscribed = False

    @property
    def running(self) -> bool:
        """True while connected to meshtasticd."""
        return self._interface is not None

    def start(self) -> None:
        """Connect to meshtasticd and start buffering received packets.

        A daemon that is not there is an honest LoRaUnavailable, not a quiet empty panel — exactly
        like the SDR routes refuse rather than fake a sweep.
        """
        if self.running:
            return
        try:
            self._interface = self._connect(self.hostname, self.port)
        except Exception as exc:  # connection refused, timeout, or the lib missing
            self._interface = None
            raise LoRaUnavailable(
                f"no LoRa radio — meshtasticd is not reachable at {self.hostname}:{self.port} "
                f"({exc})") from exc
        from pubsub import pub
        pub.subscribe(self._on_receive, "meshtastic.receive")
        self._subscribed = True

    def _on_receive(self, packet=None, interface=None) -> None:
        """pubsub callback for every received packet; buffers the ones that parse as receptions."""
        parsed = parse_lora_packet(packet)
        if parsed is not None:
            with self._lock:
                self._packets.append(parsed)

    def stop(self) -> None:
        """Disconnect from meshtasticd and stop buffering."""
        if self._subscribed:
            from pubsub import pub
            try:
                pub.unsubscribe(self._on_receive, "meshtastic.receive")
            except Exception:
                pass
            self._subscribed = False
        iface, self._interface = self._interface, None
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass

    def recent(self, limit: int | None = None) -> list:
        """The most recent packets, oldest first; `limit` keeps only the newest that many."""
        with self._lock:
            packets = list(self._packets)
        if limit is not None:
            packets = packets[-limit:]
        return packets
