# ABOUTME: Exercises the rack Send route with a radio-interface double and real protobufs.
# ABOUTME: Pins send admission, radio readback, local routing outcomes, and event ordering.

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.protobuf.json_format import MessageToDict
from meshtastic.protobuf import config_pb2, localonly_pb2, mesh_pb2
from pubsub import pub

from sigint101 import safety
from sigint101.app import create_app
from sigint101.events import read_events
from sigint101.lora import LoRaListener

NODE = 123
SEND = "/api/lora/send"
LoRaConfig = config_pb2.Config.LoRaConfig


class Radio:
    def __init__(self, lora=None, *, error=None, fast=None):
        self.myInfo = mesh_pb2.MyNodeInfo(my_node_num=NODE)
        self.localNode = SimpleNamespace(localConfig=localonly_pb2.LocalConfig())
        if lora is not None:
            self.localNode.localConfig.lora.CopyFrom(lora)
        self.isConnected = threading.Event()
        self.isConnected.set()
        self.error = error
        self.fast = fast
        self.calls = []
        self.closed = False

    def sendText(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self.error:
            raise self.error
        packet = mesh_pb2.MeshPacket(id=len(self.calls))
        if self.fast:
            self.routing(packet.id, self.fast)
        return packet

    def close(self):
        self.closed = True

    def routing(self, packet_id, reason="NONE", source=NODE):
        routing = mesh_pb2.Routing(error_reason=mesh_pb2.Routing.Error.Value(reason))
        decoded = MessageToDict(mesh_pb2.Routing.FromString(routing.SerializeToString()))
        packet = {"from": source, "rxSnr": 2.0, "decoded": {
            "portnum": "ROUTING_APP", "requestId": packet_id, "routing": decoded}}
        pub.sendMessage("meshtastic.receive", packet=packet, interface=self)


def console(tmp_path, connect):
    return TestClient(create_app(data_dir=tmp_path,
                                 lora_factory=lambda: LoRaListener(connect=connect)))


def rack_headers(client):
    page = client.get("/operate/lora").text
    nonce = re.search(r'data-lora-send-permit="([^"]+)"', page)
    assert nonce, "the rack has no Send control"
    assert page.count(safety.ANTENNA_NOTICE) == 1
    return {"X-LoRa-Send": nonce[1]}


def events(tmp_path):
    return [event for event in read_events(tmp_path / "events.jsonl")
            if event["kind"] in ("transmission", "transmission_outcome", "reception")]


def test_rack_taps_send_exactly_one_fixed_broadcast_each(tmp_path):
    radio = Radio()
    with console(tmp_path, lambda *_: radio) as client:
        headers = rack_headers(client)
        assert client.post(SEND).status_code == 403
        assert 'data-lora-send-permit' not in client.get("/operate/ism").text
        assert 'data-lora-send-permit' not in client.get("/lesson/28").text
        for _ in range(25):
            result = client.post(SEND, json={"text": "overridden"}, headers=headers)
            assert result.status_code == 200, result.text
            assert result.json()["send"]["status"] == "handed"
        sends = client.get("/api/lora").json()["sends"]
    assert [row["packet_id"] for row in sends] == list(range(6, 26))
    assert len(radio.calls) == 25
    assert all(re.fullmatch(r"SIGINT 101 test [0-9a-f]{4}", text) for text, _ in radio.calls)
    assert all(kwargs == {"destinationId": "^all", "wantAck": True, "channelIndex": 0}
               for _, kwargs in radio.calls)
    logged = events(tmp_path)
    assert [event["kind"] for event in logged] == ["transmission"] * 25
    first = logged[0]["payload"]
    assert set(first) == {"packet_id", "text", "status", "reason", "channel", "destination",
                          "want_ack", "notice", "radio"}
    assert all(value is None for key, value in first["radio"].items() if key != "settings_read")
    assert first["radio"]["settings_read"] is False


def test_settings_report_only_read_fields_with_correct_units(tmp_path):
    radio = Radio(LoRaConfig(region=LoRaConfig.US, modem_preset=LoRaConfig.LONG_TURBO,
                             override_frequency=908.75, frequency_offset=0.125,
                             hop_limit=4, channel_num=14, tx_enabled=True))
    with console(tmp_path, lambda *_: radio) as client:
        sent = client.post(SEND, headers=rack_headers(client)).json()["send"]
        readback = client.get("/api/lora").json()["radio"]
    assert readback == sent["radio"] == events(tmp_path)[0]["payload"]["radio"]
    assert readback == {"settings_read": True, "local_node_num": NODE, "region": "US",
                        "preset": None, "use_preset": False, "hop_limit": 4, "tx_enabled": True,
                        "tx_power": 0, "channel_num": 14, "freq_hz": 908_750_000,
                        "override_frequency_mhz": 908.75, "frequency_offset_mhz": 0.125}


def test_radio_can_refuse_tx_only_after_settings_arrive(tmp_path):
    radio = Radio(LoRaConfig(tx_enabled=False, use_preset=True, modem_preset=LoRaConfig.LONG_TURBO))
    with console(tmp_path, lambda *_: radio) as client:
        response = client.post(SEND, headers=rack_headers(client))
        assert response.status_code == 409
        assert client.get("/api/lora").json()["radio"]["preset"] == "LONG_TURBO"
    assert not radio.calls and not events(tmp_path)


@pytest.mark.parametrize("reason,status", [
    ("NONE", "repeated"), ("MAX_RETRANSMIT", "no-repeat"), ("NO_CHANNEL", "error"),
])
def test_wire_outcome_matches_connection_request_and_local_node(tmp_path, reason, status):
    radio = Radio()
    with console(tmp_path, lambda *_: radio) as client:
        assert client.post(SEND, headers=rack_headers(client)).status_code == 200
        Radio().routing(1)
        radio.routing(2)
        radio.routing(1, source=NODE + 1)
        radio.myInfo = None
        radio.routing(1, source=None)
        assert client.get("/api/lora").json()["sends"][0]["status"] == "handed"
        radio.myInfo = mesh_pb2.MyNodeInfo(my_node_num=NODE)
        radio.routing(1, reason)
        radio.routing(1, "NO_ROUTE")
        assert client.get("/api/lora").json()["sends"][0]["status"] == status
    logged = events(tmp_path)
    assert [event["kind"] for event in logged] == ["transmission", "transmission_outcome"]
    assert logged[-1]["payload"] == {"packet_id": 1, "reason": reason, "status": status,
                                     "sender_node_num": NODE, "text": logged[0]["payload"]["text"]}


def test_fast_routing_reply_is_not_lost_before_send_returns(tmp_path):
    radio = Radio(fast="NO_CHANNEL")
    with console(tmp_path, lambda *_: radio) as client:
        send = client.post(SEND, headers=rack_headers(client)).json()["send"]
    assert (send["status"], send["reason"]) == ("error", "NO_CHANNEL")
    assert [event["kind"] for event in events(tmp_path)] == [
        "transmission", "transmission_outcome"]


def test_mid_send_socket_failure_returns_503_and_logs_lost(tmp_path):
    old, replacement = Radio(error=OSError("pipe closed")), Radio()
    radios = iter([old, replacement])
    with console(tmp_path, lambda *_: next(radios)) as client:
        response = client.post(SEND, headers=rack_headers(client))
        assert response.status_code == 503
        assert "pipe closed" in response.json()["reason"]
        assert response.json()["send"]["status"] == "lost"
        assert client.get("/api/lora").json()["sends"][0]["status"] == "lost"
    assert old.closed and not replacement.calls
    assert events(tmp_path)[0]["payload"]["status"] == "lost"


def test_send_starts_lazily_and_refuses_absent_daemon(tmp_path):
    def absent(*_):
        raise OSError("connection refused")

    with console(tmp_path, absent) as client:
        response = client.post(SEND, headers=rack_headers(client))
        assert response.status_code == 503
        assert "connection refused" in response.json()["reason"]
    assert not events(tmp_path)


def test_default_console_cannot_reach_a_radio_from_tests(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        response = client.post(SEND, headers=rack_headers(client))
        assert response.status_code == 503
        assert "unavailable in tests" in response.json()["reason"]


def test_stop_waits_for_send_to_leave_the_interface(tmp_path):
    entered, release, stopping = threading.Event(), threading.Event(), threading.Event()

    class HeldRadio(Radio):
        def sendText(self, text, **kwargs):
            entered.set()
            assert release.wait(5)
            assert not self.closed
            return super().sendText(text, **kwargs)

    radio = HeldRadio(fast="NO_CHANNEL")
    with console(tmp_path, lambda *_: radio) as client, ThreadPoolExecutor(2) as workers:
        headers = rack_headers(client)
        sent = workers.submit(client.post, SEND, headers=headers)

        def stop():
            stopping.set()
            client.app.state.lora.stop()

        try:
            assert entered.wait(5)
            ended = workers.submit(stop)
            assert stopping.wait(5)
            with pytest.raises(TimeoutError):
                ended.result(timeout=0.05)
        finally:
            release.set()
        assert sent.result(timeout=5).status_code == 200
        ended.result(timeout=5)
    assert radio.closed


def test_outcome_logging_error_does_not_break_receiver(tmp_path):
    radio = Radio()
    with console(tmp_path, lambda *_: radio) as client:
        assert client.post(SEND, headers=rack_headers(client)).status_code == 200
        path = tmp_path / "events.jsonl"
        path.rename(tmp_path / "events.saved")
        path.mkdir()
        radio.routing(1)
        assert client.get("/api/lora").json()["sends"][0]["status"] == "repeated"


def test_direct_safety_caller_cannot_transmit():
    radio = Radio()
    with pytest.raises(PermissionError):
        safety.send_lora_text(radio, "SIGINT 101 test abcd")
    assert radio.calls == []


@pytest.mark.parametrize("text", ["", "é" * 117, "\ud800"])
def test_safety_validates_utf8_payload_size_before_accessing_radio(text):
    radio = Radio()
    with pytest.raises(ValueError):
        safety.send_lora_text(radio, text)
    assert radio.calls == []
