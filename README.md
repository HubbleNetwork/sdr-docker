# pluto-sdr-docker

Docker container for the PlutoSDR that streams IQ data, displays a live rolling
spectrogram, and decodes packets in real time. Results are served as a web
dashboard on port **8050**.

## Architecture

| Component      | Thread     | Description                                                        |
|----------------|------------|--------------------------------------------------------------------|
| **PlutoSDR RX** | background | Reads IQ samples into a 2 s circular buffer                       |
| **Processor**   | background | Every 0.5 s: compute spectrogram chunk, render 10 s image, decode  |
| **Flask server** | main      | Serves web page with live spectrogram + decoded device table       |

### Data flow

```
PlutoSDR  ──RX──>  IQ circular buffer (2 s)
                        │
                        ├──> 0.5 s chunk ──> vis spectrogram (NFFT=4096)
                        │                        │
                        │                   deque of 20 chunks ──> JPEG image
                        │
                        └──> 1.0 s chunk ──> detection spectrogram (NFFT=625)
                                                 │
                                            template matching + NMS
                                                 │
                                            FSK demodulation + RS decode
                                                 │
                                            decoded device IDs + seq nums
```

### Project structure

```
src/stream_web/
├── config.py          # All SDR / decoder / display constants
├── decoder.py         # Dual-protocol preamble detection + packet decode
├── spectrogram.py     # Spectrogram computation and image rendering
├── sdr.py             # PlutoSDR RX thread (pyadi-iio)
├── processor.py       # Processing loop (spec + decode + render)
├── app.py             # Flask app, routes, thread orchestration
├── templates/
│   └── index.html     # Dashboard HTML
└── static/
    └── style.css      # Dashboard CSS
```

## Setup

### Install Docker

Official instructions: <https://docs.docker.com/engine/install/>

Quick install with the convenience script:

```shell
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh ./get-docker.sh
```

### Add user to docker group

```shell
sudo groupadd docker
sudo usermod -aG docker $USER
```

Log out and back in, then verify:

```shell
groups | grep docker
```

### Install git-lfs

The TX source files are stored with git-lfs:

```shell
# Debian / Ubuntu
sudo apt install git-lfs

# macOS
brew install git-lfs
```

### Clone and build

```shell
git clone <repo-url>
cd pluto-sdr-docker/
git lfs pull
docker build -t pluto_container .
```

> **Non-x86 architectures:** download the correct libiio `.deb` from
> [libiio releases](https://github.com/analogdevicesinc/libiio/releases/tag/v0.26)
> and update the `wget` line in the [Dockerfile](./Dockerfile).

## Run

### Background

```shell
docker run -d -p 8050:8050 pluto_container
```

### Interactive

```shell
docker run -it -p 8050:8050 pluto_container
```

Then open <http://localhost:8050> in a browser.

### With Docker Compose (development)

```shell
docker build -t pluto_container .
docker compose up
```

The compose file mounts the local repo into the container so code changes take
effect on restart (`docker compose restart`).

## Stop

```shell
docker ps
docker kill <container_id>
```

## Configuration

All tuneable parameters live in [`src/stream_web/config.py`](src/stream_web/config.py).
Key settings:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `PLUTO_URI` | `ip:192.168.2.1` | PlutoSDR network address |
| `PLUTO_FREQ_HZ` | 2.482754875 GHz | Centre frequency |
| `SAMPLE_RATE` | 781 250 Hz | ADC sample rate |
| `RX_INITIAL_GAIN_DB` | 40 | Initial RX gain (adjustable from the UI) |
| `FLASK_PORT` | 8050 | Web server port |
| `SPEC_DURATION_S` | 10.0 | Rolling spectrogram window |
| `DECODE_INTERVAL_S` | 0.5 | Decode cycle interval |

## Web dashboard

The dashboard auto-refreshes every 500 ms and provides:

- **Live spectrogram** — 10 s rolling window with coloured detection boxes
  (orange = PHY v-1, red = PHY v1).
- **Decodes tab** — per-device summary: PHY version, device ID, chipset, RSSI,
  last 10 sequence numbers, last-seen timestamp.
- **Statistics tab** — per-chipset decode success rates.
- **Gain control** — adjust RX gain from the browser.
- **Time-domain viewer** — enter a device ID to see a per-symbol magnitude plot.

## Source files

The `source_files/` directory contains pre-generated TX waveforms (`.out` files
produced by Matlab). These are used by the legacy TX API and are not required
for the streaming decoder.
