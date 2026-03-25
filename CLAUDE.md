# sdr-docker

Multi-SDR streaming spectrogram + packet decoder web application.

## Build & test

```bash
pip install -e ".[dev]"
ruff check src/
python3 run_stream.py          # requires SDR hardware or Docker
```

## Docker

```bash
docker build -t sdr-docker .
docker run -p 8050:8050 sdr-docker
```

## Project structure

- `src/stream_web/config.py` — SDR and display configuration (protocol constants via fast-decoder)
- `src/stream_web/gnuradio_rx.py` — GNU Radio RX flowgraph using gr-soapy
- `src/stream_web/gnuradio_tx.py` — GNU Radio TX flowgraph using gr-soapy (full-duplex)
- `src/stream_web/spectrogram.py` — spectrogram image rendering (PIL/matplotlib)
- `src/stream_web/processor.py` — decode + spectrogram loop (separate OS process)
- `src/stream_web/app.py` — Flask web app, API routes, process orchestration
- `run_stream.py` — entry point

## Key dependencies

- **fast-decoder** — preamble detection, FSK decoding, protocol constants
- **GNU Radio + gr-soapy** — unified SDR RX/TX (system-level, not pip)
- **Flask** — web server and API

## Conventions

- Hatchling build with `src` layout
- Ruff is the sole linter
- Protocol constants come from `fast_decoder.constants`; SDR/display config stays in `config.py`
- `config.py` re-exports protocol constants for backward compatibility
