# SIGINT 101

A touch-screen, learn-by-doing course that takes a complete beginner to competent
signals-intelligence fundamentals, running on a SpecFive Nomad 2 Recon paired with a Flipper Zero.

The console *is* the curriculum: each lesson drives the device's real radios — read the spectrum,
tell modulations apart, build an antenna, decode real signals, and learn the law and ethics of
listening — then transmit once you're ready.

Status: in active development (see STATUS.md).

## Layout
- `sigint101/` — FastAPI console, lesson engine, hardware probe
- `lesson_packs/` — one file per lesson
- `tests/` — unit, integration, end-to-end
- `ff.config.json` — feature-factory project config
