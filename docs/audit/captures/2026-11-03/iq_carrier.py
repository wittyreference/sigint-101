# ABOUTME: Reduces an rtl_sdr I/Q recording of one airband channel to 50 ms frames: the AM carrier
# ABOUTME: line, the voice sidebands, and the channel's 42 kHz waterfall bin, for the L8 key check.
"""L8 ground truth: does the carrier hold through pauses between words, and do the waterfall's dark
rows fall where a transmission ended?

The console's waterfall draws a row a second, too slow to see a word gap, so the check needs a
finer look at the same radio: an I/Q recording (rtl_sdr, 8-bit unsigned I/Q) of a channel found
busy in the waterfall capture. Each 50 ms frame is Fourier transformed (20 Hz bins) and reduced to:

    t_s           frame start, seconds
    carrier_db    the strongest 20 Hz line within 2 kHz of the channel, +-60 Hz, over the median
                  20 Hz bin of the frame: the AM carrier while a transmitter is keyed
    voice_db      the sidebands 300-3000 Hz either side of that line, per 20 Hz bin, over the same
                  median: the speech itself, which falls away between words
    chan_lin      mean power in the 42.4 kHz around the channel (one waterfall bin), linear
    bins_lin      median of the 42.4 kHz bins across the recording, linear (the row's median)

    iq_carrier.py IQ.cu8 RATE CHANNEL_OFFSET_HZ OUT.csv

Needs numpy; nomad-b's system python3 has it. The recording itself is not kept in the repo.
"""

import csv
import sys

import numpy as np

FRAME_S = 0.05
WATERFALL_BIN_HZ = 42_400
SEARCH_HZ = 2_000
CARRIER_HZ = 60
VOICE_HZ = (300, 3_000)


def main(path, rate, offset_hz, out):
    rate, offset_hz = int(rate), float(offset_hz)
    n = int(rate * FRAME_S)
    window = np.hanning(n)
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1 / rate))
    res = rate / n
    near = np.abs(freqs - offset_hz) <= SEARCH_HZ
    chan = np.abs(freqs - offset_hz) <= WATERFALL_BIN_HZ / 2
    edges = np.arange(freqs[0], freqs[-1], WATERFALL_BIN_HZ)
    which = np.digitize(freqs, edges)
    raw = np.memmap(path, dtype=np.uint8, mode="r")
    frames = len(raw) // (2 * n)
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        for k in range(frames):
            chunk = raw[2 * n * k: 2 * n * (k + 1)].astype(np.float32) - 127.5
            x = (chunk[0::2] + 1j * chunk[1::2]) * window
            p = np.abs(np.fft.fftshift(np.fft.fft(x))) ** 2
            floor = np.median(p)
            peak = np.flatnonzero(near)[np.argmax(p[near])]
            span = int(round(CARRIER_HZ / res))
            carrier = p[peak - span: peak + span + 1].mean()
            off = np.abs(freqs - freqs[peak])
            voice = p[(off >= VOICE_HZ[0]) & (off <= VOICE_HZ[1])].mean()
            bins = [p[which == b].mean() for b in np.unique(which)]
            w.writerow([f"{k * FRAME_S:.2f}", f"{10 * np.log10(carrier / floor):.2f}",
                        f"{10 * np.log10(voice / floor):.2f}", f"{p[chan].mean():.6g}",
                        f"{np.median(bins):.6g}"])


if __name__ == "__main__":
    main(*sys.argv[1:5])
