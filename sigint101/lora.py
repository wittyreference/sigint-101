# ABOUTME: Reads receptions and routes rack send reports from the local meshtasticd API.
# ABOUTME: Keeps radio settings and routing outcomes tied to the current local interface.

from __future__ import annotations

import math
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass

from .safety import ANTENNA_NOTICE, LoRaQueueFull, connect_lora, require_rack_send, send_lora_text

# meshtasticd's default TCP API. The Nomad's own LoRa radio (a MeshAdv sx1262) is driven by the
# meshtasticd service; the console uses its API and does not claim an SDR device.
DEFAULT_LORA_HOST = "localhost"
DEFAULT_LORA_PORT = 4403
DEFAULT_HISTORY = 40
SEND_HISTORY = 20
OPERATION_WAIT_S = 6


class LoRaUnavailable(RuntimeError):
    """The LoRa radio cannot be read. Never a reason to synthesise a packet — the panel says so."""


class LoRaSendLost(LoRaUnavailable):
    def __init__(self, send):
        super().__init__(send["reason"])
        self.send = send


class LoRaTxDisabled(RuntimeError):
    """The radio explicitly reports that transmit is disabled."""


def local_node_num(interface):
    return getattr(getattr(interface, "myInfo", None), "my_node_num", None) or None


def radio_settings(interface) -> dict:
    """Return only settings held by the interface, with MHz converted only for the override."""
    config = getattr(getattr(interface, "localNode", None), "localConfig", None)
    if config is None or not config.HasField("lora"):
        return dict.fromkeys(("local_node_num", "region", "preset", "use_preset", "hop_limit",
                              "tx_enabled", "tx_power", "channel_num", "freq_hz",
                              "override_frequency_mhz", "frequency_offset_mhz"),
                             None) | {"settings_read": False}
    lora = config.lora
    fields = lora.DESCRIPTOR.fields_by_name

    def name(field, value):
        enum_value = fields[field].enum_type.values_by_number.get(value)
        return enum_value.name if enum_value is not None else value

    override = lora.override_frequency if math.isfinite(lora.override_frequency) else None
    offset = lora.frequency_offset if math.isfinite(lora.frequency_offset) else None
    return {
        "settings_read": True, "local_node_num": local_node_num(interface),
        "region": name("region", lora.region),
        "preset": name("modem_preset", lora.modem_preset) if lora.use_preset else None,
        "use_preset": lora.use_preset, "hop_limit": lora.hop_limit, "tx_enabled": lora.tx_enabled,
        "tx_power": lora.tx_power, "channel_num": lora.channel_num,
        "freq_hz": round(override * 1_000_000) if override and override > 0 else None,
        "override_frequency_mhz": override, "frequency_offset_mhz": offset,
    }


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


class LoRaListener:
    """Subscribes to meshtasticd and keeps a bounded buffer of recently received packets.

    A sibling of the Sweeper/Decoder in spirit — a long-lived connection feeding a buffer off the
    request path — but it reads an existing daemon rather than owning a device, so it never contends
    with the RTL-SDR and needs no release/resume.
    """

    def __init__(self, *, hostname=DEFAULT_LORA_HOST, port=DEFAULT_LORA_PORT,
                 history=DEFAULT_HISTORY, connect=connect_lora):
        self.hostname = hostname
        self.port = int(port)
        self._connect = connect
        self._packets = deque(maxlen=history)
        # Called with each packet received over the air, as a plain dict, from meshtastic's
        # receive callback only and outside the buffer lock. The console's hook logs it as a
        # reception and never raises, so the call is made directly.
        self.on_reception = None
        self.on_transmission = None
        # Reserve one operation at a time, but release the mutex before SDK calls or callbacks.
        self._life = threading.Condition()
        self._operating = False
        self._lock = threading.Lock()
        self._interface = None
        self._subscribed = False
        self._lost = False
        self._sends = deque(maxlen=SEND_HISTORY)
        self._early = None

    @property
    def running(self) -> bool:
        """True while connected to meshtasticd — the connection itself, re-checked every time.

        After EOF or a failed write, the next request opens a separate interface. The old handle
        can remain until then, so the panel checks the SDK's connection flag and loss callback.
        """
        iface = self._interface
        return iface is not None and not self._lost and iface.isConnected.is_set()

    @contextmanager
    def _operation(self):
        with self._life:
            if not self._life.wait_for(lambda: not self._operating, timeout=OPERATION_WAIT_S):
                raise LoRaUnavailable("The radio is busy with another request.")
            self._operating = True
        try:
            yield
        finally:
            with self._life:
                self._operating = False
                self._life.notify_all()

    def start(self) -> None:
        """Connect to meshtasticd and start buffering received packets.

        A daemon that is not there is an honest LoRaUnavailable, not a quiet empty panel — exactly
        like the SDR routes refuse rather than fake a sweep.
        """
        with self._operation():
            self._start()

    def _start(self):
        if self.running:
            return
        if self._interface is not None:
            self._stop()
        try:
            iface = self._connect(self.hostname, self.port)
        except Exception as exc:  # connection refused, timeout, or the lib missing
            raise LoRaUnavailable(
                f"no LoRa radio — meshtasticd is not reachable at {self.hostname}:{self.port} "
                f"({exc})") from exc
        from pubsub import pub
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        self._subscribed = True
        with self._lock:
            self._lost = False
            self._interface = iface

    def _on_lost(self, interface=None) -> None:
        """pubsub callback when meshtastic gives up on the daemon; the next poll reconnects."""
        with self._lock:
            if interface is self._interface:
                self._lost = True

    def _on_receive(self, packet=None, interface=None) -> None:
        """pubsub callback for every received packet; buffers the ones that parse as receptions."""
        with self._lock:
            if interface is None or interface is not self._interface:
                return
            decoded = packet.get("decoded") if isinstance(packet, dict) else None
            handled, outcome = False, None
            if isinstance(decoded, dict) and decoded.get("portnum") == "ROUTING_APP":
                handled, outcome = self._route_outcome(interface, packet, decoded)
            parsed = None if handled else self._buffer_reception(packet)
        if outcome is not None:
            self._report("transmission_outcome", outcome)
        self._heard(parsed)

    def _buffer_reception(self, packet):
        parsed = parse_lora_packet(packet)
        if parsed is not None:
            self._packets.append(parsed)
        return parsed

    def _heard(self, parsed):
        if parsed is not None and self.on_reception is not None:
            self.on_reception(parsed.as_dict())

    def _route_outcome(self, interface, packet, decoded):
        """Resolve a matched reply, or defer classification until the current send has an ID."""
        routing, packet_id = decoded.get("routing"), decoded.get("requestId")
        sender = packet.get("from")
        if (not isinstance(routing, dict) or not isinstance(packet_id, int)
                or not isinstance(sender, int) or not sender):
            return False, None
        reason = routing.get("errorReason", "NONE")
        local = local_node_num(interface)
        if reason == "NONE":
            if local is None or sender != local:
                return False, None
            status = "repeated"
        elif reason == "MAX_RETRANSMIT" and sender == local:
            status = "no-repeat"
        else:
            status = "error"
        notice = {"packet_id": packet_id, "sender_node_num": sender,
                  "status": status, "reason": reason}
        for owner, record in reversed(self._sends):
            if owner is interface and record["packet_id"] == packet_id:
                return True, self._settle(record, notice)
        if self._early is not None:
            self._early.append((notice, packet))
            return True, None
        return False, None

    @staticmethod
    def _settle(record, outcome):
        if record["status"] != "handed":
            return None
        record.update(status=outcome["status"], reason=outcome["reason"])
        return {**outcome, "text": record["text"]}

    def _report(self, kind, payload):
        if self.on_transmission is not None:
            self.on_transmission(kind, payload)

    def send_state(self) -> dict:
        with self._lock:
            return {"radio": radio_settings(self._interface if self.running else None),
                    "sends": [dict(row) for _, row in self._sends]}

    def send(self, text: str) -> dict:
        """Hand one text to the current connection and keep its terminal report when it arrives."""
        require_rack_send()
        from meshtastic.mesh_interface import MeshInterface

        with self._operation():
            self._start()
            iface = self._interface
            settings = radio_settings(iface)
            if settings["tx_enabled"] is False:
                raise LoRaTxDisabled("The radio reports that transmit is disabled.")
            with self._lock:
                self._early = []
            record = {"packet_id": None, "text": text, "status": "handed", "reason": None,
                      "channel": 0, "destination": "^all", "want_ack": True,
                      "notice": ANTENNA_NOTICE, "radio": settings}
            try:
                record.update(send_lora_text(iface, text))
            except LoRaQueueFull as exc:
                raise LoRaUnavailable(str(exc)) from exc
            except (OSError, RuntimeError, MeshInterface.MeshInterfaceError) as exc:
                record.update(status="lost", reason=str(exc) or "The radio connection was lost.")
                self._finish_send(iface, record)
                self._stop()
                raise LoRaSendLost(record) from exc
            else:
                return self._finish_send(iface, record)
            finally:
                self._resolve_early()

    def _finish_send(self, interface, record):
        # A racing reply stays buffered until the transmission is logged and its ID registered.
        self._report("transmission", dict(record))
        return self._resolve_early(interface, record)

    def _resolve_early(self, interface=None, record=None):
        with self._lock:
            early, self._early = self._early, None
            if record is not None:
                self._sends.append((interface, record))
            receptions = []
            outcome = None
            for notice, packet in early or ():
                if record is not None and notice["packet_id"] == record["packet_id"]:
                    settled = self._settle(record, notice)
                    outcome = settled or outcome
                else:
                    receptions.append(self._buffer_reception(packet))
            result = dict(record) if record is not None else None
        if outcome is not None:
            self._report("transmission_outcome", outcome)
        for parsed in receptions:
            self._heard(parsed)
        return result

    def stop(self) -> None:
        """Disconnect from meshtasticd and stop buffering."""
        with self._operation():
            self._stop()

    def _stop(self):
        with self._lock:
            iface, self._interface = self._interface, None
        if self._subscribed:
            from pubsub import pub
            for listener, topic in ((self._on_receive, "meshtastic.receive"),
                                    (self._on_lost, "meshtastic.connection.lost")):
                try:
                    pub.unsubscribe(listener, topic)
                except Exception:
                    pass
            self._subscribed = False
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
