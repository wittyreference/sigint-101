# ABOUTME: Verifies that an exited socket feed retires without another route calling start.
# ABOUTME: Sends final messages over local sockets and counts closure at the transport boundary.

import socket
import threading

import pytest

from sigint101 import adsb, ais
from tests.child_fakes import Child
from tests.test_ais import _udp_pair
from tests.test_receiver_retirement import until
from tests.test_stale_readers import ADSB_1, ADSB_2, AIS_1, AIS_2


class CountingSocket:
    def __init__(self, sock):
        self._sock = sock
        self.closes = 0

    def __getattr__(self, name):
        return getattr(self._sock, name)

    def close(self):
        self.closes += 1
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@pytest.mark.parametrize("module,ctor,lines,field,expected", [
    (adsb, adsb.AdsbDecoder, (ADSB_1, ADSB_2), "hex_ident", ["A1B2C3", "DDEEFF"]),
    (ais, ais.AisDecoder, (AIS_1, AIS_2), "mmsi", [477553000, 265547250]),
], ids=["adsb", "ais"])
def test_idle_socket_feed_drains_and_closes_once(module, ctor, lines, field, expected):
    peer, client = _udp_pair() if module is ais else socket.socketpair()
    with client, peer:
        child = Child(["test-decoder"])
        client.settimeout(module.FEED_POLL_S)
        transport = CountingSocket(client)
        sender = CountingSocket(peer) if module is ais else None
        if sender is not None:
            feed = ais._Feed(child, transport, sender)
            feed.configure()
        else:
            feed = adsb._Feed(child, transport)
        decoder = ctor(spawn=lambda *_: feed, radio_present=lambda: True, which=lambda name: name)
        entered, drain = threading.Event(), threading.Event()
        heard = []

        def receive(item):
            heard.append(item[field])
            if len(heard) == 1:
                entered.set()
                assert drain.wait(5)

        decoder.on_reception = receive
        decoder.start()
        run = decoder._runs.current
        try:
            peer.sendall((lines[0] + "\n").encode())
            assert entered.wait(2)
            peer.sendall((lines[1] + "\n").encode())
            child.code = 7
            drain.set()
            assert until(lambda: decoder._runs.current != run, seconds=2), (
                "the dead feed needed another call to start before retiring")
            assert until(lambda: client.fileno() == -1)
            assert heard == expected
            assert transport.closes == 1
            assert client.fileno() == -1
            if sender is not None:
                assert until(lambda: peer.fileno() == -1)
                assert sender.closes == 1
            decoder.stop()
            assert transport.closes == 1
            if sender is not None:
                assert sender.closes == 1
        finally:
            drain.set()
            decoder.stop()
