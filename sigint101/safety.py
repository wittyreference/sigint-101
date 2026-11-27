# ABOUTME: Holds the course's one hardware-protection notice (antenna or 50-ohm load before TX).
# ABOUTME: Keeps the notice non-blocking and authorizes one radio call per rack Send request.

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from secrets import compare_digest

# One plain sentence (<= 20 words), shown wherever the LoRa radio is live or the learner swaps
# antennas. Hardware protection for the PA, never a legal gate or a disabled control (D16).
ANTENNA_NOTICE = (
    "LoRa transmits on its own: keep an antenna or 50-ohm load on the jack when powered."
)

# Lessons where the learner swaps antennas on the SDR jack (M6 Resonance, M7 Build, Aim & Clean Up).
ANTENNA_LESSONS = frozenset({6, 7})
_rack_authorized = ContextVar("rack_lora_authorized", default=False)


@contextmanager
def rack_lora_send(request, permit):
    """Authorize one call in the rack POST's request context."""
    supplied = request.headers.get("X-LoRa-Send", "")
    if (request.method != "POST" or getattr(request.scope.get("route"), "name", None) != "lora_send"
            or not compare_digest(supplied.encode(), permit.encode())):
        raise PermissionError("Use Send in the LoRa rack.")
    token = _rack_authorized.set(True)
    try:
        yield
    finally:
        _rack_authorized.reset(token)


def require_rack_send() -> None:
    if not _rack_authorized.get():
        raise PermissionError("Use Send in the LoRa rack.")


class LoRaQueueFull(RuntimeError):
    """The daemon reports no room for another packet."""


@dataclass
class _WriteReceipt:
    interface: object
    packet_id: int | None = None


_write_receipt = ContextVar("lora_write_receipt", default=None)
_RADIO_TIMEOUT_S = 5


def _queue_slot(interface):
    status = getattr(interface, "queueStatus", None)
    if status is not None and status.free <= 0:
        raise LoRaQueueFull("The radio reports its transmit queue is full.")
    return status


class _BoundedTCP:
    """Use the SDK's framing, with one immediate, verifiable write per rack tap."""

    def __init__(self, **kwargs):
        self._wire = threading.Lock()
        super().__init__(**kwargs)

    def myConnect(self):
        self.socket = socket.create_connection((self.hostname, self.portNumber), _RADIO_TIMEOUT_S)
        self.socket.setblocking(True)  # Reads can wait for traffic. Writes use MSG_DONTWAIT.

    def _waitConnected(self, timeout=_RADIO_TIMEOUT_S):
        return super()._waitConnected(timeout)

    def _readBytes(self, length):
        # The listener reopens the interface on the next poll. A retired connection can never
        # carry a tap across the disconnect onto a replacement socket.
        conn = self.socket
        if conn is None:
            return None
        data = conn.recv(length)
        if not data:
            self._wantExit = True
        return data

    def _handleQueueStatusFromRadio(self, status):
        # The SDK also keeps a host retry queue. The console submits one frame per tap and uses the
        # daemon's routing result, so only its reported capacity needs to be retained here.
        self.queueStatus = status

    def _sendToRadio(self, frame):
        if frame.HasField("packet"):
            receipt = _write_receipt.get()
            if receipt is None or receipt.interface is not self or receipt.packet_id is not None:
                raise PermissionError("Use Send in the LoRa rack.")
            status = _queue_slot(self)
            if status is not None:
                status.free -= 1
            self._sendToRadioImpl(frame)
            receipt.packet_id = frame.packet.id
        elif frame.HasField("want_config_id"):
            self._sendToRadioImpl(frame)
        elif frame.HasField("heartbeat"):
            try:
                self._sendToRadioImpl(frame)
            except OSError:
                # The idle poll reconnects after a failed keepalive.
                if self.heartbeatTimer is not None:
                    self.heartbeatTimer.cancel()
        else:
            raise PermissionError("Unsupported LoRa control frame.")

    def _writeBytes(self, data):
        with self._wire:
            conn = self.socket
            if conn is None or self._wantExit:
                raise ConnectionError("The radio connection was lost.")
            try:
                if conn.send(data, socket.MSG_DONTWAIT) != len(data):
                    raise ConnectionError("The radio connection wrote an incomplete frame.")
            except OSError:
                # A frame prefix cannot be followed by another frame on this stream.
                self._wantExit = True
                self.isConnected.clear()
                raise

    def close(self):
        with self._wire:
            self._wantExit = True
            conn, self.socket = self.socket, None
        if self.heartbeatTimer is not None:
            self.heartbeatTimer.cancel()
        self.isConnected.clear()
        if conn is not None:
            with suppress(OSError):
                conn.shutdown(socket.SHUT_WR)
            self._wait_for_reader_exit(0.25)
            with suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            conn.close()


def connect_lora(host, port):
    from meshtastic.tcp_interface import TCPInterface

    class RadioTCP(_BoundedTCP, TCPInterface):
        pass

    return RadioTCP(hostname=host, portNumber=port, timeout=_RADIO_TIMEOUT_S)


def send_lora_text(interface, text: str) -> dict:
    """Hand one valid UTF-8 text to the radio, requesting its broadcast acknowledgement."""
    if not 1 <= len(text.encode("utf-8")) <= 233:
        raise ValueError("LoRa text must be 1 to 233 UTF-8 bytes.")
    require_rack_send()
    _rack_authorized.set(False)
    _queue_slot(interface)
    receipt = _WriteReceipt(interface)
    token = _write_receipt.set(receipt)
    try:
        packet = interface.sendText(text, destinationId="^all", wantAck=True, channelIndex=0)
        if isinstance(interface, _BoundedTCP) and receipt.packet_id != packet.id:
            raise ConnectionError("The radio connection did not write the frame.")
        return {"packet_id": packet.id, "notice": ANTENNA_NOTICE}
    finally:
        _write_receipt.reset(token)


def notice_for(*, lesson=None, instrument=None) -> str | None:
    """Return the antenna notice text when a lesson or rack view needs it, else None.

    Returns text only, never a boolean or flag, so templates render it directly without gating any
    control behind it.
    """
    if lesson is not None:
        if getattr(lesson, "requires_tx", False):
            return ANTENNA_NOTICE
        panels = getattr(lesson, "panels", None) or ()
        if getattr(lesson, "uses_lora", False) or "lora" in panels:
            return ANTENNA_NOTICE
        if getattr(lesson, "id", None) in ANTENNA_LESSONS:
            return ANTENNA_NOTICE
    if instrument is not None:
        if instrument == "lora":
            return ANTENNA_NOTICE
        if (getattr(instrument, "key", None) == "lora"
                or getattr(instrument, "radio", None) == "lora"):
            return ANTENNA_NOTICE
    return None
