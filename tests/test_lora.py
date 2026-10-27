# ABOUTME: Tests the LoRa backend — parsing meshtasticd packets and the listener lifecycle.
# ABOUTME: The meshtastic interface is a fake at the boundary; no radio is an honest refusal.

import pytest

from sigint101.lora import (
    DEFAULT_LORA_PORT,
    LoRaListener,
    LoRaUnavailable,
    parse_lora_packet,
)

# Real meshtastic MeshPacket dict shapes (what pub "meshtastic.receive" delivers). A text message
# heard below the noise floor (rxSnr negative) — the whole point of M11 — and a telemetry packet
# with no text payload. These are the library's documented asDict shape.
TEXT_PKT = {
    "from": 1128074276, "to": 4294967295, "channel": 0,
    "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"p", "text": "ping from node B"},
    "id": 998877, "rxTime": 1730000000, "rxSnr": -11.25, "rxRssi": -119,
    "hopLimit": 3, "hopStart": 3, "fromId": "!433d1234", "toId": "^all",
}
TELEM_PKT = {
    "from": 1128074276,
    "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {"deviceMetrics": {"batteryLevel": 84}}},
    "id": 998878, "rxTime": 1730000005, "rxSnr": 6.5, "rxRssi": -92,
    "hopLimit": 3, "fromId": "!433d1234",
}
ENCRYPTED_PKT = {
    "from": 222333444, "id": 998879, "rxTime": 1730000009, "rxSnr": -3.0, "rxRssi": -110,
    "fromId": "!0d3e5f60", "encrypted": b"\x00\x01\x02",
}


def test_parse_reads_a_text_packet_heard_below_the_noise_floor():
    p = parse_lora_packet(TEXT_PKT)
    assert p is not None
    assert p.from_id == "!433d1234"
    assert p.snr_db == pytest.approx(-11.25)
    assert p.rssi_dbm == -119
    assert p.portnum == "TEXT_MESSAGE_APP"
    assert "ping from node B" in p.summary
    assert p.below_floor is True          # negative SNR = decoded below the noise floor
    assert p.hop_limit == 3


def test_parse_a_positive_snr_packet_is_not_below_the_floor():
    p = parse_lora_packet(TELEM_PKT)
    assert p.below_floor is False
    assert p.portnum == "TELEMETRY_APP"
    # No text payload, so the summary names the kind of packet rather than inventing content.
    assert "TELEMETRY" in p.summary.upper()


def test_parse_an_encrypted_packet_is_a_real_reception_we_cannot_read():
    # An encrypted packet (not our channel) is still a reception with an SNR; it is reported
    # honestly as unreadable, never decoded into invented text.
    p = parse_lora_packet(ENCRYPTED_PKT)
    assert p is not None
    assert p.from_id == "!0d3e5f60"
    assert p.snr_db == pytest.approx(-3.0)
    assert p.portnum == "ENCRYPTED"
    assert "encrypted" in p.summary.lower()


def test_parse_skips_a_non_packet():
    assert parse_lora_packet({}) is None
    assert parse_lora_packet({"foo": "bar"}) is None
    assert parse_lora_packet("not a dict") is None


def test_parse_skips_a_locally_originated_packet_with_no_snr():
    # The node's own telemetry/nodeinfo loops through the API with a sender but no rxSnr — it was
    # not received over the air, so it is not a reception the SNR lesson should show.
    local = {"from": 123, "fromId": "!0000007b",
             "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {}}}
    assert parse_lora_packet(local) is None


def test_as_dict_carries_what_the_api_sends():
    out = parse_lora_packet(TEXT_PKT).as_dict()
    assert out["from_id"] == "!433d1234"
    assert out["snr_db"] == pytest.approx(-11.25)
    assert out["below_floor"] is True
    assert out["portnum"] == "TEXT_MESSAGE_APP"


class FakeInterface:
    """Stands in for a meshtastic TCPInterface at the app boundary — no real radio."""

    def __init__(self, *args, **kwargs):
        self.closed = False

    def close(self):
        self.closed = True


def _listener(**kw):
    kw.setdefault("connect", lambda host, port: FakeInterface())
    return LoRaListener(**kw)


def test_listener_buffers_received_packets():
    lis = _listener()
    lis.start()
    lis._on_receive(TEXT_PKT)
    lis._on_receive(TELEM_PKT)
    reads = lis.recent()
    assert [r.portnum for r in reads] == ["TEXT_MESSAGE_APP", "TELEMETRY_APP"]


def test_listener_buffer_is_bounded_to_newest():
    lis = _listener(history=3)
    lis.start()
    for n in range(8):
        pkt = dict(TEXT_PKT, id=n, rxSnr=-float(n))
        lis._on_receive(pkt)
    reads = lis.recent()
    assert len(reads) == 3
    assert reads[-1].snr_db == pytest.approx(-7.0)


def test_listener_refuses_when_meshtasticd_is_unreachable():
    def refuse(host, port):
        raise OSError("connection refused")
    lis = _listener(connect=refuse)
    with pytest.raises(LoRaUnavailable):
        lis.start()


def test_listener_running_and_stop():
    iface = FakeInterface()
    lis = _listener(connect=lambda host, port: iface)
    assert lis.running is False
    lis.start()
    assert lis.running is True
    lis.stop()
    assert lis.running is False
    assert iface.closed is True


def test_default_port_is_the_meshtasticd_api():
    assert DEFAULT_LORA_PORT == 4403
