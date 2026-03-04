"""Flask web application and thread orchestration.

Serves the live spectrogram dashboard on port 8050 and coordinates the
SDR RX, processor, and Flask server threads.
"""

import base64
import logging
import threading
from collections import deque

import numpy as np
from flask import Flask, Response, jsonify, render_template, request as flask_request

from . import config
from .decoder import get_chipset_stats, reset_chipset_stats
from .processor import process_loop
from .sdr import rx_loop


# ===========================================================================
# Shared application state
# ===========================================================================

class SharedState:
    """Thread-safe mutable state shared between RX, processor, and Flask."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = threading.Event()
        self.rx_connected = threading.Event()

        # IQ circular buffer
        self.iq_buffer = np.zeros(config.IQ_BUFFER_SIZE, dtype=np.complex64)
        self.buf_write_idx = 0

        # Rolling spectrogram chunks
        self.spec_chunks: deque = deque(maxlen=config.MAX_SPEC_CHUNKS)

        # Data served to the web page
        self.latest_img: bytes = b""
        self.latest_detections: list[dict] = []
        self.decode_results: list[dict] = []
        self.detection_history: list[dict] = []
        self.decode_stats: dict = {
            "process_time_ms": 0, "n_detections": 0, "timestamp": "",
            "t_spec_ms": 0, "t_render_ms": 0, "t_decode_ms": 0,
        }

        # Time-domain plot state
        self.td_target_ntw_id: int | None = None
        self.td_running = False
        self.td_latest_img: bytes = b""
        self.td_status: str = ""

        # AGC state
        self.rx_gain_dB: float = config.RX_INITIAL_GAIN_DB
        self.rx_peak_frac: float = 0.0


state = SharedState()


# ===========================================================================
# Flask app
# ===========================================================================

app = Flask(__name__)

if not config.VERBOSE:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/spectrogram.jpg")
def spectrogram_jpg():
    with state.lock:
        data = state.latest_img
    if not data:
        return Response(status=204)
    return Response(data, mimetype="image/jpeg")


@app.route("/api/status")
def api_status():
    with state.lock:
        devices: dict[int, dict] = {}
        for r in state.decode_results:
            nid = r["ntw_id"]
            if nid not in devices:
                devices[nid] = {
                    "ntw_id": nid,
                    "ntw_id_hex": r["ntw_id_hex"],
                    "phy_ver": r.get("phy_ver", -1),
                    "chipset": r.get("chipset", ""),
                    "max_energy_dB": r["energy_dB"],
                    "seq_nums": [],
                    "last_seen": r["timestamp"],
                }
            d = devices[nid]
            d["max_energy_dB"] = r["energy_dB"]
            d["seq_nums"].append(r["seq_num"])
            d["last_seen"] = r["timestamp"]

        for d in devices.values():
            seen = set()
            unique_seqs = []
            for s in d["seq_nums"]:
                if s not in seen:
                    seen.add(s)
                    unique_seqs.append(s)
            d["seq_nums"] = unique_seqs[-10:]

        dev_list = sorted(devices.values(), key=lambda x: x["ntw_id"])
        stats = dict(state.decode_stats)
        stats["n_unique_devices"] = len(dev_list)

        td_b64 = ""
        if state.td_running and state.td_latest_img:
            td_b64 = base64.b64encode(state.td_latest_img).decode("ascii")

        cs_stats = get_chipset_stats()

        return jsonify(
            devices=dev_list, stats=stats,
            td_img=td_b64, td_running=state.td_running,
            td_device_id=state.td_target_ntw_id, td_status=state.td_status,
            chipset_stats=cs_stats,
        )


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with state.lock:
        state.decode_results.clear()
        state.decode_stats.update(
            process_time_ms=0, n_detections=0, timestamp="",
            t_spec_ms=0, t_render_ms=0, t_decode_ms=0,
        )
        reset_chipset_stats()
        state.detection_history = []
    return jsonify(ok=True)


@app.route("/api/gain", methods=["POST"])
def api_gain():
    data = flask_request.get_json(silent=True) or {}
    direction = data.get("direction", 0)
    new_gain = state.rx_gain_dB + direction * config.RX_GAIN_STEP_DB
    new_gain = int(max(config.RX_GAIN_MIN_DB, min(config.RX_GAIN_MAX_DB, new_gain)))
    state.rx_gain_dB = new_gain
    return jsonify(gain=new_gain)


@app.route("/api/timedomain", methods=["GET", "POST"])
def api_timedomain():
    if flask_request.method == "POST":
        data = flask_request.get_json(silent=True) or {}
        action = data.get("action")
        if action == "start":
            dev_id = data.get("device_id")
            if dev_id is not None:
                try:
                    state.td_target_ntw_id = int(dev_id)
                    state.td_running = True
                except (ValueError, TypeError):
                    return jsonify(error="Invalid device_id"), 400
        elif action == "stop":
            state.td_running = False
    with state.lock:
        status = state.td_status
    return jsonify(running=state.td_running, device_id=state.td_target_ntw_id, status=status)


# ===========================================================================
# Entry point
# ===========================================================================

def main():
    """Start all threads and run the Flask server (blocking)."""
    state.running.set()

    rx_thread = threading.Thread(target=rx_loop, args=(state,), daemon=True)
    rx_thread.start()

    proc_thread = threading.Thread(target=process_loop, args=(state,), daemon=True)
    proc_thread.start()

    print(f"[main] All threads started.")
    print(f"[main] Open http://localhost:{config.FLASK_PORT} in a browser.")

    app.run(
        host="0.0.0.0",
        port=config.FLASK_PORT,
        threaded=True,
        use_reloader=False,
    )
