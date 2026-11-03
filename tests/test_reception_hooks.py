# ABOUTME: Tests the receivers' operator hooks: retune on the single-channel decoders, and the
# ABOUTME: on_reception call each one makes once per live-decoded item, never from history.

import base64
import io
import json
import os
import struct
import threading

import pytest

from sigint101 import (
    acars,
    adsb,
    ais,
    aprs,
    apt,
    decode,
    dmr,
    flock,
    lora,
    pocsag,
    rayhunter,
    rds,
    sstv,
)
from tests.child_fakes import Child

# --- real-shaped tool output, copied from each module's own tests ------------------------------

# rtl_433 -F json: an ASK weather sensor and an FSK one (tests/test_decode.py).
RTL433_ASK = ('{"time" : "2026-10-27 13:04:11", "model" : "Acurite-Tower", "id" : 16262, '
              '"channel" : "A", "battery_ok" : 1, "temperature_C" : 18.000, "humidity" : 51, '
              '"mic" : "CHECKSUM", "mod" : "ASK", "freq" : 433.950, "rssi" : -1.112, '
              '"snr" : 36.261, "noise" : -37.373}')
RTL433_FSK = ('{"time" : "2026-10-27 13:10:02", "model" : "LaCrosse-TX141THBv2", "id" : 252, '
              '"channel" : 0, "battery_ok" : 1, "temperature_C" : 21.3, "humidity" : 45, '
              '"mod" : "FSK", "freq1" : 433.882, "freq2" : 433.958, "rssi" : -8.1, '
              '"snr" : 22.4, "noise" : -30.5}')

# multimon-ng APRS frames (tests/test_aprs.py).
APRS_POS = "APRS: KD2ABC-9>APU25N,WIDE1-1,WIDE2-1:!4042.00N/07400.00W>Test de KD2ABC"
APRS_MSG = "APRS: K9XYZ>APRS::KD2ABC   :Hello there{001"

# multimon-ng POCSAG pages (tests/test_pocsag.py).
POCSAG_ALPHA = "POCSAG1200: Address: 1234567  Function: 3  Alpha:   CALL EXT 2210 URGENT"
POCSAG_TONE = "POCSAG512: Address: 1000000  Function: 1 "
SECRET = "CALL EXT 2210 URGENT"

# redsea groups (tests/test_rds.py), plus a second station's PI on the same shape.
RDS_0A = ('{"pi":"0x6201","group":"0A","tp":false,"prog_type":"Serious classical",'
          '"di":{"stereo":true},"ta":true,"is_music":true}')
RDS_1A = '{"pi":"0x6201","group":"1A","tp":false,"prog_type":"Serious classical"}'
RDS_PS = '{"pi":"0x6201","group":"0A","ps":"YLEKLAS1"}'
RDS_RT = '{"pi":"0x6201","group":"2A","radiotext":"Now playing: Sibelius - Finlandia"}'
RDS_OTHER_PI = '{"pi":"0x1234","group":"0A","tp":false}'

# acarsdec -o 4 JSON (tests/test_acars.py).
ACARS_WITH_TEXT = (
    '{"timestamp":1793151825.5348461,"station_id":"spec5b","channel":0,"freq":131.550,'
    '"level":-8.5,"error":0,"mode":"G","label":"H1","block_id":"3","ack":false,'
    '"tail":"F-GTAE","flight":"AF7728","msgno":"D65C",'
    '"text":"#DFB00000/V206,05,124,183,02,00,00000/V7044,078,00081,22222222222111/",'
    '"app":{"name":"acarsdec","ver":"3.7"}}')
ACARS_NO_TEXT = (
    '{"timestamp":1793151825.53618,"station_id":"spec5b","channel":1,"freq":131.725,'
    '"level":-23.0,"error":0,"mode":"E","label":"5V","block_id":"4","ack":false,'
    '"tail":"PH-BXR","flight":"KL1681","msgno":"S53A","app":{"name":"acarsdec","ver":"3.7"}}')

# dsd-fme signaling lines (tests/test_dmr.py).
DMR_CALL = (
    "16:00:15 Sync: +DMR  slot1 [slot2]  | Color Code=03 | VLC SLOT 2 "
    "TGT=21001 SRC=21002 FLCO=0x07 FID=0x10 SVC=0x00 Cap+ Private Call Cap+ R-Ch 3")
DMR_SYNC_ONLY = "13:50:20 Sync: +P25p1 "
P25_GROUP = "13:50:22 Sync: +P25p1  Group Voice Channel User - Group 2001 Source 1234567"

# AIVDM sentences from the gpsd corpus (tests/test_ais.py): MMSI 477553000, then 351759000.
AIS_TYPE1 = "!AIVDM,1,1,,B,177KQJ5000G?tO`K>RA1wUbN0TKH,0*5C"
AIS_TYPE5_FRAG1 = ("!AIVDM,2,1,1,A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6"
                   "ClRp8,0*1C")
AIS_TYPE5_FRAG2 = "!AIVDM,2,2,1,A,88888888880,2*25"

# dump1090 BaseStation lines (tests/test_adsb.py): A1B2C3 three times, then DDEEFF.
SBS_IDENT = ("MSG,1,1,1,A1B2C3,1,2026/10/28,10:15:00.000,2026/10/28,10:15:00.100,"
             "UAL123  ,,,,,,,,0,0,0,0")
SBS_POSITION = ("MSG,3,1,1,A1B2C3,1,2026/10/28,10:15:01.000,2026/10/28,10:15:01.100,"
                ",37000,,,37.61950,-122.38680,,,0,0,0,0")
SBS_VELOCITY = ("MSG,4,1,1,A1B2C3,1,2026/10/28,10:15:02.000,2026/10/28,10:15:02.100,"
                ",,450,118,,,-832,,0,0,0,0")
SBS_OTHER = ("MSG,3,1,1,DDEEFF,1,2026/10/28,10:15:03.000,2026/10/28,10:15:03.100,"
             ",39000H,,,37.80000,-122.40000,,,0,0,0,0")

# meshtasticd MeshPacket dicts (tests/test_lora.py).
LORA_TEXT = {
    "from": 1128074276, "to": 4294967295, "channel": 0,
    "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"p", "text": "ping from node B"},
    "id": 998877, "rxTime": 1730000000, "rxSnr": -11.25, "rxRssi": -119,
    "hopLimit": 3, "hopStart": 3, "fromId": "!433d1234", "toId": "^all",
}
LORA_ENCRYPTED = {
    "from": 222333444, "id": 998879, "rxTime": 1730000009, "rxSnr": -3.0, "rxRssi": -110,
    "fromId": "!0d3e5f60", "encrypted": b"\x00\x01\x02",
}

# A real 1x1 PNG (tests/test_sstv.py): what an image decoder leaves on disk.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _common():
    return {"radio_present": lambda: True, "which": lambda name: "/usr/bin/" + name}


class Hook:
    """The console's reception hook, recorded: each item, and whether the buffer lock was free."""

    def __init__(self, owner):
        self.calls = []
        self.lock_free = []
        self._owner = owner

    def __call__(self, item):
        self.calls.append(item)
        lock = getattr(self._owner, "_lock", None)
        if lock is not None:
            free = lock.acquire(blocking=False)
            if free:
                lock.release()
            self.lock_free.append(free)


def _hook(receiver):
    hook = Hook(receiver)
    receiver.on_reception = hook
    return hook


def _talking(*lines):
    """A running tool that has printed these lines on stdout."""
    return Child(["tool"], said="\n".join(lines))


# --- every receiver starts with no hook ---------------------------------------------------------


def _all_receivers(tmp_path):
    return [
        decode.Decoder(**_common()),
        aprs.AprsDecoder(**_common()),
        pocsag.PocsagDecoder(**_common()),
        rds.RDSDecoder(freq_hz=106_500_000, **_common()),
        acars.AcarsDecoder(**_common()),
        dmr.DmrDecoder(**_common()),
        ais.AisDecoder(**_common()),
        adsb.AdsbDecoder(**_common()),
        apt.AptDecoder(output_dir=tmp_path / "apt", **_common()),
        sstv.SstvDecoder(output_dir=tmp_path / "sstv", data_dir=tmp_path, **_common()),
        lora.LoRaListener(connect=lambda host, port: None),
        flock.FlockDetector(**_common()),
        rayhunter.RayhunterMonitor(fetch=lambda url, timeout: (404, b"")),
    ]


def test_every_receiver_starts_with_no_reception_hook(tmp_path):
    receivers = _all_receivers(tmp_path)
    assert len(receivers) == 13
    for receiver in receivers:
        assert receiver.on_reception is None, type(receiver).__name__


# --- retune on the six single-channel decoders --------------------------------------------------

# name -> (class, how its spawn is called, a line it decodes, what it has heard, another freq)
SINGLE_CHANNEL = {
    "decode": (decode.Decoder, "argv", RTL433_ASK, lambda d: d.recent(), 315_000_000),
    "aprs": (aprs.AprsDecoder, "freq", APRS_POS, lambda d: d.stations(), 144_800_000),
    "pocsag": (pocsag.PocsagDecoder, "freq", POCSAG_ALPHA, lambda d: d.pages(), 439_987_500),
    "rds": (rds.RDSDecoder, "freq", RDS_PS, lambda d: d.station().group_count, 99_100_000),
    "acars": (acars.AcarsDecoder, "freq", ACARS_WITH_TEXT, lambda d: d.messages(), 131_725_000),
    "dmr": (dmr.DmrDecoder, "freq", DMR_CALL, lambda d: d.calls(), 451_000_000),
}


def _single(name, *, freq_hz, lines=()):
    """One single-channel decoder whose spawns are recorded as (frequency, child)."""
    cls, style, _, _, _ = SINGLE_CHANNEL[name]
    spawned = []

    def launch(freq):
        child = _talking(*lines)
        spawned.append((freq, child))
        return child

    def spawn_argv(argv):
        return launch(int(argv[argv.index("-f") + 1]))

    def spawn_freq(freq, device):
        return launch(int(freq))

    dec = cls(freq_hz=freq_hz, spawn=spawn_argv if style == "argv" else spawn_freq, **_common())
    return dec, spawned


@pytest.mark.parametrize("name", sorted(SINGLE_CHANNEL))
def test_home_hz_is_the_frequency_the_decoder_was_built_with(name):
    other = SINGLE_CHANNEL[name][4]
    dec, _ = _single(name, freq_hz=106_500_000)
    assert dec.home_hz == 106_500_000
    dec.retune(other)
    assert dec.freq_hz == other
    assert dec.home_hz == 106_500_000        # home stays where the lesson is restored to


@pytest.mark.parametrize("name", sorted(SINGLE_CHANNEL))
def test_retune_stops_the_child_and_the_next_start_spawns_on_the_new_frequency(name):
    other = SINGLE_CHANNEL[name][4]
    dec, spawned = _single(name, freq_hz=106_500_000)
    dec.start()
    first = spawned[0][1]
    dec.retune(other)
    assert first.sent == ["TERM"]            # the old child is stopped, deliberately
    assert dec.running is False
    assert dec.watch.stopped is False        # ...and so is not reported as a death
    assert dec.released is False             # retuning does not hand the dongle away
    dec.start()                              # no back-off, no resume needed
    assert [freq for freq, _ in spawned] == [106_500_000, other]
    dec.stop()


@pytest.mark.parametrize("name", sorted(SINGLE_CHANNEL))
def test_retune_to_the_frequency_already_tuned_leaves_the_child_alone(name):
    dec, spawned = _single(name, freq_hz=106_500_000)
    dec.start()
    dec.retune(106_500_000)
    dec.retune(106_500_000.0)
    assert spawned[0][1].sent == []
    assert dec.running is True
    dec.start()
    assert len(spawned) == 1
    dec.stop()


@pytest.mark.parametrize("name", sorted(SINGLE_CHANNEL))
def test_retune_clears_what_was_heard_on_the_old_frequency(name):
    _, _, line, heard, other = SINGLE_CHANNEL[name]
    dec, _ = _single(name, freq_hz=106_500_000, lines=[line])
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert heard(dec)
    reported = len(hook.calls)
    dec.retune(other)
    assert not heard(dec)
    assert dec.released is False
    assert len(hook.calls) == reported       # clearing the history reports nothing


# --- POCSAG: the words' fate is bound to the band the child was spawned on (M15) ----------------


def _pocsag(freq_hz, stdout=None, lines=()):
    children = []

    def spawn(freq, device):
        child = _talking(*lines)
        if stdout is not None:
            child.stdout = stdout()
        children.append(child)
        return child

    return pocsag.PocsagDecoder(freq_hz=freq_hz, spawn=spawn, **_common())


def test_a_child_spawned_off_the_paging_band_keeps_the_words():
    dec = _pocsag(152_000_000, lines=[POCSAG_ALPHA])
    dec.start()
    dec.drain()
    assert dec.pages()[0]["text"] == SECRET
    dec.stop()


@pytest.mark.parametrize("spawned_hz,moved_to_hz,text", [
    (929_612_500, 152_000_000, None),        # heard on the paging band: withheld, always
    (152_000_000, 929_612_500, SECRET),      # heard off it: the dial moving later changes nothing
])
def test_the_band_rule_is_the_one_the_child_was_spawned_under(spawned_hz, moved_to_hz, text):
    dec = None

    def stream():
        yield POCSAG_ALPHA
        dec.freq_hz = moved_to_hz            # the dial moves while this child is still talking
        yield POCSAG_ALPHA

    dec = _pocsag(spawned_hz, stdout=stream)
    dec.start()
    dec.drain()
    assert [p["text"] for p in dec.pages()] == [text, text]
    assert all(p["text_withheld"] is (text is None) for p in dec.pages())
    dec.stop()


def test_after_a_retune_onto_the_paging_band_the_next_child_withholds():
    dec = _pocsag(152_000_000, lines=[POCSAG_ALPHA])
    dec.start()
    dec.drain()
    assert dec.pages()[0]["text"] == SECRET
    dec.retune(930_000_000)
    assert dec.pages() == []
    dec.start()
    dec.drain()
    page = dec.pages()[0]
    assert page["text"] is None
    assert page["text_withheld"] is True
    assert SECRET not in repr(dec.pages())
    dec.stop()


# --- on_reception: once per live-decoded item, outside the buffer lock --------------------------


def _decoder_with(cls, lines, **kw):
    """A decoder whose every spawn is a running tool that has printed `lines`."""
    def spawn(*_args):
        return _talking(*lines)
    return cls(spawn=spawn, **_common(), **kw)


def test_rtl433_reports_each_sensor_read():
    dec = _decoder_with(decode.Decoder, [RTL433_ASK, "Tuned to 433.920MHz.", RTL433_FSK])
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert hook.calls == [d.as_dict() for d in dec.recent()]
    assert [c["model"] for c in hook.calls] == ["Acurite-Tower", "LaCrosse-TX141THBv2"]
    assert all(hook.lock_free)
    dec.recent()
    dec.release()                            # reading or clearing history reports nothing
    assert len(hook.calls) == 2


def test_aprs_reports_the_updated_station_for_each_packet():
    dec = _decoder_with(aprs.AprsDecoder, [APRS_POS, "noise", APRS_MSG, APRS_POS])
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert [c["source"] for c in hook.calls] == ["KD2ABC-9", "K9XYZ", "KD2ABC-9"]
    assert [c["packet_count"] for c in hook.calls] == [1, 1, 2]
    roster = {s["source"]: s for s in dec.stations()}
    assert hook.calls[2] == roster["KD2ABC-9"]
    assert hook.calls[1] == roster["K9XYZ"]
    assert all(hook.lock_free)
    dec.stations()
    assert len(hook.calls) == 3


def test_pocsag_reports_each_page_with_the_words_already_withheld():
    dec = _decoder_with(pocsag.PocsagDecoder, [POCSAG_ALPHA, "noise", POCSAG_TONE])
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert hook.calls == list(reversed(dec.pages()))
    assert hook.calls[0]["text"] is None
    assert hook.calls[0]["text_withheld"] is True
    assert SECRET not in repr(hook.calls)
    assert all(hook.lock_free)


def test_rds_reports_the_station_when_its_identity_is_learned_or_changes():
    lines = [RDS_0A, RDS_1A, RDS_PS, RDS_PS, RDS_RT, RDS_OTHER_PI]
    dec = _decoder_with(rds.RDSDecoder, lines, freq_hz=106_500_000)
    hook = _hook(dec)
    dec.start()
    dec.drain()
    # A new PI, then the PS name arriving, then another PI — not every group (they come ~11/s).
    assert [(c["pi"], c["ps"]) for c in hook.calls] == [
        ("0x6201", None), ("0x6201", "YLEKLAS1"), ("0x1234", "YLEKLAS1")]
    assert [c["group_count"] for c in hook.calls] == [1, 3, 6]
    assert hook.calls[-1] == dec.station().as_dict()
    assert all(hook.lock_free)


def test_rds_does_not_log_a_scrolling_ps_name_as_new_stations():
    # Many US stations scroll song titles through the PS field a few seconds apart. That is one
    # station still: its name is reported once per PI, so the log is not a line per scroll.
    scroll = ['{"pi":"0x6201","group":"0A","ps":"%s"}' % name
              for name in ("YLEKLAS1", "NOW PLAY", "SIBELIUS", "YLEKLAS1")]
    dec = _decoder_with(rds.RDSDecoder, [RDS_0A, *scroll], freq_hz=106_500_000)
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert [(c["pi"], c["ps"]) for c in hook.calls] == [("0x6201", None), ("0x6201", "YLEKLAS1")]


def test_acars_reports_each_message():
    dec = _decoder_with(acars.AcarsDecoder, [ACARS_WITH_TEXT, "not json", ACARS_NO_TEXT])
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert hook.calls == list(reversed(dec.messages()))
    assert [c["flight"] for c in hook.calls] == ["AF7728", "KL1681"]
    assert all(hook.lock_free)


def test_dmr_reports_each_call():
    dec = _decoder_with(dmr.DmrDecoder, [DMR_CALL, DMR_SYNC_ONLY, P25_GROUP])
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert hook.calls == list(reversed(dec.calls()))
    assert [c["protocol"] for c in hook.calls] == ["DMR", "P25"]
    assert all(hook.lock_free)


def test_ais_reports_each_vessel_the_first_time_it_is_heard_since_start():
    lines = [AIS_TYPE1, AIS_TYPE1, "garbage", AIS_TYPE5_FRAG1, AIS_TYPE5_FRAG2, AIS_TYPE1]
    dec = _decoder_with(ais.AisDecoder, lines)
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert [c["mmsi"] for c in hook.calls] == [477553000, 351759000]
    assert hook.calls[0]["message_count"] == 1           # the contact as first heard
    assert hook.calls[1]["name"] == "EVER DIADEM"
    assert set(hook.calls[0]) == set(dec.vessels()[0])
    assert all(hook.lock_free)
    dec.stop()
    dec.start()                                          # a fresh run: each is a new contact
    dec.drain()
    assert [c["mmsi"] for c in hook.calls] == [477553000, 351759000] * 2
    dec.stop()


def test_adsb_reports_each_aircraft_the_first_time_it_is_heard_since_start():
    lines = [SBS_IDENT, SBS_POSITION, SBS_VELOCITY, SBS_OTHER]
    dec = _decoder_with(adsb.AdsbDecoder, lines, receiver_lat=37.62, receiver_lon=-122.38)
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert [c["hex_ident"] for c in hook.calls] == ["A1B2C3", "DDEEFF"]
    assert hook.calls[0]["callsign"] == "UAL123"
    assert hook.calls[0]["message_count"] == 1
    assert hook.calls[1]["range_nm"] is not None         # placed on the scope like the API does
    assert set(hook.calls[0]) == set(dec.aircraft()[0])
    assert all(hook.lock_free)
    dec.stop()
    dec.start()
    dec.drain()
    assert [c["hex_ident"] for c in hook.calls] == ["A1B2C3", "DDEEFF"] * 2
    dec.stop()


# Flock frames (tests/test_flock.py): the camera's probe-request fingerprint, and a bare OUI hit.
_FLOCK_OUI = bytes.fromhex("b41e52")
_BROADCAST = b"\xff" * 6
_FLOCK_IES = bytes.fromhex(
    "0000" "020100" "0c0100" "7f080000000000000000" "dd07506f9a16030103"
    "2d1a" + "00" * 26 + "bf0c" + "00" * 12 + "dd070050f208000000")


def _mgmt_frame(subtype, addr1, addr2, addr3, ies=b""):
    return bytes([subtype << 4, 0x00]) + b"\x00\x00" + addr1 + addr2 + addr3 + b"\x00\x00" + ies


def _pcap(frames, rt_len=36):
    out = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 512, 127)
    for dot11 in frames:
        rec = struct.pack("<BBH", 0, 0, rt_len) + b"\x00" * (rt_len - 4) + dot11
        out += struct.pack("<IIII", 0, 0, len(rec), len(rec)) + rec
    return out


def test_flock_reports_each_device_the_first_time_it_is_detected_since_start():
    probe = _mgmt_frame(4, _BROADCAST, _FLOCK_OUI + b"\x00\x00\x08", _BROADCAST, _FLOCK_IES)
    beacon = _mgmt_frame(8, _BROADCAST, _FLOCK_OUI + b"\x00\x00\x09", _BROADCAST)
    data = _pcap([probe, probe, beacon, probe])

    def spawn(interface):
        child = Child(["tcpdump"])
        child.stdout = io.BytesIO(data)
        return child

    dec = flock.FlockDetector(spawn=spawn, **_common())
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert [c["mac"] for c in hook.calls] == ["b4:1e:52:00:00:08", "b4:1e:52:00:00:09"]
    assert hook.calls[0]["tier"] == 4
    assert hook.calls[0]["count"] == 1
    assert set(hook.calls[0]) == set(dec.detections()[0])
    assert all(hook.lock_free)
    dec.stop()


def test_lora_reports_each_packet_received_over_the_air():
    class Interface:
        def __init__(self):
            self.isConnected = threading.Event()
            self.isConnected.set()

        def close(self):
            pass

    lis = lora.LoRaListener(connect=lambda host, port: Interface())
    hook = _hook(lis)
    lis.start()
    try:
        lis._on_receive(packet=LORA_TEXT)
        lis._on_receive(packet={"from": 123, "fromId": "!0000007b"})     # the node itself: no
        lis._on_receive(packet=LORA_ENCRYPTED)
    finally:
        lis.stop()
    assert hook.calls == [p.as_dict() for p in lis.recent()]
    assert [c["from_id"] for c in hook.calls] == ["!433d1234", "!0d3e5f60"]
    assert all(hook.lock_free)


def _png(path):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(PNG_BYTES)


def test_apt_reports_each_image_its_own_pass_wrote_once_the_pass_ends(tmp_path):
    out = tmp_path / "apt"
    passes = []

    def spawn(freq, device, output_dir):
        child = Child(["satdump"])
        passes.append(child)
        return child

    dec = apt.AptDecoder(output_dir=out, spawn=spawn, **_common())
    _png(out / "earlier" / "old.png")                    # an image from before: never reported
    hook = _hook(dec)
    dec.start()
    _png(out / "NOAA-19" / "avhrr_3_rgb.png")            # what satdump writes during the pass
    _png(out / "NOAA-19" / "raw.png")
    assert hook.calls == []                              # nothing until the pass is done
    passes[0].code = 0                                   # the pass times out cleanly
    dec.start()                                          # the next poll reaps it, starts another
    rows = {row["path"]: row for row in dec.images()}
    assert sorted(c["path"] for c in hook.calls) == ["apt/NOAA-19/avhrr_3_rgb.png",
                                                     "apt/NOAA-19/raw.png"]
    for call in hook.calls:
        assert call == rows[call["path"]]
    dec.stop()                                           # the second pass wrote nothing
    dec.release()
    assert len(hook.calls) == 2


def test_sstv_reports_the_picture_its_capture_decoded(tmp_path):
    out = tmp_path / "sstv"

    def spawn(freq, device, output_path):
        _png(output_path)                                # sstv's picture for this capture
        return _talking("Searching for calibration header... Found!",
                        "Detected SSTV mode Robot 36", "Decoding image... done")

    _png(out / "20250101T000000-000.png")                # a picture from before: never reported
    dec = sstv.SstvDecoder(output_dir=out, data_dir=tmp_path, spawn=spawn, **_common())
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert len(hook.calls) == 1
    assert hook.calls[0]["mode"] == "Robot 36"
    assert hook.calls[0] == next(i for i in dec.images() if i["path"] == hook.calls[0]["path"])
    assert hook.calls[0]["path"] != "sstv/20250101T000000-000.png"
    dec.stop()


def test_sstv_reports_nothing_for_a_capture_that_decoded_no_picture(tmp_path):
    dec = sstv.SstvDecoder(output_dir=tmp_path / "sstv", data_dir=tmp_path,
                           spawn=lambda f, d, p: _talking("Searching for calibration header..."),
                           **_common())
    hook = _hook(dec)
    dec.start()
    dec.drain()
    assert hook.calls == []
    dec.stop()


_RH_META = {"analyzers": [{"name": "IMSI Requested"},
                          {"name": "Connection Release/Redirected Carrier 2G Downgrade"},
                          {"name": "Incomplete SIB"}],
            "report_version": 2}
_RH_DOWNGRADE = {"packet_timestamp": "2026-10-29T11:02:03-07:00", "events": [
    None, {"event_type": "High",
           "message": "Connection released and redirected to a 2G cell (packet 4213)"}, None]}
_RH_IMSI = {"packet_timestamp": "2026-10-29T11:03:04-07:00", "events": [
    {"event_type": "Low", "message": "IMSI requested without authentication (packet 5102)"},
    None, {"event_type": "Informational", "message": "Connected to cell (packet 5103)"}]}
_RH_SIB = {"packet_timestamp": "2026-10-29T11:04:05-07:00", "events": [
    None, None, {"event_type": "Medium", "message": "SIB1 missing scheduling (packet 6001)"}]}
_RH_MANIFEST = json.dumps({"entries": [], "current_entry": {"name": "1730300000"}}).encode()


def test_rayhunter_reports_each_warning_the_first_time_it_is_parsed():
    rows = [_RH_DOWNGRADE, _RH_IMSI]

    def fetch(url, timeout):
        if url.endswith("/api/qmdl-manifest"):
            return 200, _RH_MANIFEST
        lines = [json.dumps(_RH_META)] + [json.dumps(r) for r in rows]
        return 200, ("\n".join(lines) + "\n").encode()

    mon = rayhunter.RayhunterMonitor(fetch=fetch)
    hook = _hook(mon)
    mon.start()
    assert mon.status() == "warning"
    # In capture order, as the analyser raised them; the Informational note is not a warning.
    assert [c["severity"] for c in hook.calls] == ["high", "low"]
    assert hook.calls == list(reversed(mon.warnings()))
    mon.warnings()
    mon.status()
    assert len(hook.calls) == 2                          # polled again: nothing new
    rows.append(_RH_SIB)                                 # the Orbic flags one more
    mon.warnings()
    assert [c["severity"] for c in hook.calls] == ["high", "low", "medium"]
    assert all(hook.lock_free)
