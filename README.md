# SIGINT 101

A touch-screen, learn-by-doing course that takes a complete beginner to competent
signals-intelligence fundamentals, running on a SpecFive Nomad 2 Recon paired with a Flipper Zero.

The console *is* the curriculum: each lesson drives the device's real radios — read the spectrum,
tell modulations apart, build an antenna, decode real signals, and learn the law and ethics of
listening — then transmit once you're ready.

Status: in active development (see STATUS.md).

## Setup

### Development, on any Linux machine
You need Python 3.10 or later with its `venv` module (on Debian, the `python3-venv` package), git,
make, jq, and Node.js 20.11+ (21.2+ on the 21 line; 22 and later work). `make selftest` needs Node
for the panels' JavaScript tests and jq for the credential-check tests; the course itself uses
neither.
`./bootstrap.sh` downloads the Python packages, so it needs a network connection.

```
./bootstrap.sh   # makes .venv, installs requirements.txt, arms the git hooks
make selftest    # the whole test suite in parallel, with the 80% coverage gate
make run         # the console at http://127.0.0.1:8008
```

Use `make selftest-serial` to run the same coverage gate in one process.
The native AIS tests need `cc` and GitHub access to fetch the pinned `rtl-ais` source.
To run them offline, set `SIGINT101_RTL_AIS_SOURCE` to an already patched checkout of that pin.

With `make run` active and Chromium installed, run `make render-check` in another terminal for the 800×480 layout checks.
Use `make render-check RENDER_URL=http://127.0.0.1:8009` for a console on a different port.

### A fresh Recon
On the Recon's stock Debian Bookworm, with a network connection, as the user the desktop logs in
as (not root):

```
git clone https://github.com/wittyreference/sigint-101 ~/sigint-101
~/sigint-101/deploy/provision.sh   # asks for sudo; add --check first to see what it would change
sudo reboot
```

`deploy/provision.sh` installs the radio tools, builds the pinned decoders, runs `bootstrap.sh`
and makes the course open full-screen at boot. From there, the quick start,
[docs/quickstart.md](docs/quickstart.md), covers using the course, setting it up for your site,
and updating it.

## Layout
- `sigint101/` — FastAPI console, lesson engine, hardware probe
- `lesson_packs/` — one file per lesson
- `tests/` — unit, integration, end-to-end
- `ff.config.json` — feature-factory project config
