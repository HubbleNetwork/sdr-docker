"""Flask web application and process orchestration.

Serves the live spectrogram dashboard on port 8050 and coordinates the
SDR RX thread, processor *process*, and Flask server.

The processor runs in a **separate OS process** so its Python GIL is
independent of the RX thread.  This prevents spectrogram/decode
computation from stalling the real-time sample stream and causing
sample drops.
"""

import base64
import json
import logging
import multiprocessing as mp
import threading
from collections import deque
from multiprocessing import shared_memory

import numpy as np
import io

from flask import Flask, Response, jsonify, render_template, request as flask_request, send_file

from . import config
from .decoder import get_chipset_stats, reset_chipset_stats
from .processor import processor_main
from .sdr import rx_loop


# ===========================================================================
# Shared application state
# ===========================================================================

_IQ_SHM_NAME = "pluto_iq_buf"
_IQ_NBYTES = config.IQ_BUFFER_SIZE * np.dtype(np.complex64).itemsize


class SharedState:
    """State shared between RX thread, processor process, and Flask.

    The IQ circular buffer lives in POSIX shared memory so the processor
    process can read it without any copy or GIL contention.  Simple
    scalars use ``multiprocessing.Value`` (atomic on CPython).  Everything
    else stays in normal Python objects protected by a threading lock
    (only accessed within the main process).
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.running = mp.Event()
        self.rx_connected = threading.Event()

        # --- shared memory IQ buffer (RX writes, processor reads) ---
        try:
            shared_memory.SharedMemory(name=_IQ_SHM_NAME).unlink()
        except FileNotFoundError:
            pass
        self._shm = shared_memory.SharedMemory(
            name=_IQ_SHM_NAME, create=True, size=_IQ_NBYTES,
        )
        self.iq_buffer = np.ndarray(
            config.IQ_BUFFER_SIZE, dtype=np.complex64, buffer=self._shm.buf,
        )
        self.iq_buffer[:] = 0

        # --- multiprocessing-safe scalars (accessed by RX + processor) ---
        self._buf_write_idx = mp.Value("q", 0)       # unsigned‐long‐long
        self._rx_peak_frac = mp.Value("d", 0.0)
        self._rx_overflows = mp.Value("i", 0)
        self._rx_gain_dB = mp.Value("d", config.RX_INITIAL_GAIN_DB)

        # --- control values read by the processor (mp-safe) ---
        self._td_running = mp.Value("b", 0)
        self._td_target_ntw_id = mp.Value("q", 0)    # 0 = None
        self._td_has_ntw_id = mp.Value("b", 0)       # flag
        self._lo_freq_hz = mp.Value("q", config.CENTER_FREQ_HZ)

        # td_target_chipset needs a string; use a fixed-size mp.Array
        self._td_chipset_arr = mp.Array("c", 32)

        # --- drop positions (RX→processor, lock-free via mp.Queue) ---
        self.drop_queue: mp.Queue = mp.Queue()

        # --- results coming back from processor (via mp.Queue) ---
        self.result_queue: mp.Queue = mp.Queue()

        # --- main-process-only state (Flask / result drainer) ---
        self.spec_chunks: deque = deque(maxlen=config.MAX_SPEC_CHUNKS)
        self.latest_img: bytes = b""
        self.latest_detections: list[dict] = []
        self.decode_results: list[dict] = []
        self.packet_feed: list[dict] = []
        self.detection_history: list[dict] = []
        self.decode_stats: dict = {
            "process_time_ms": 0, "n_detections": 0, "timestamp": "",
            "t_spec_ms": 0, "t_render_ms": 0, "t_decode_ms": 0,
        }

        self.td_latest_img: bytes = b""
        self.td_status: str = ""
        self.td_decode_info: dict | None = None
        self.td_iq_segment: np.ndarray | None = None

    def cleanup_shm(self):
        try:
            self._shm.close()
            self._shm.unlink()
        except Exception:
            pass

    # --- properties that wrap mp.Value for transparent access ---

    @property
    def buf_write_idx(self):
        return self._buf_write_idx.value

    @buf_write_idx.setter
    def buf_write_idx(self, v):
        self._buf_write_idx.value = v

    @property
    def rx_peak_frac(self):
        return self._rx_peak_frac.value

    @rx_peak_frac.setter
    def rx_peak_frac(self, v):
        self._rx_peak_frac.value = v

    @property
    def rx_overflows(self):
        return self._rx_overflows.value

    @rx_overflows.setter
    def rx_overflows(self, v):
        self._rx_overflows.value = v

    @property
    def rx_gain_dB(self):
        return self._rx_gain_dB.value

    @rx_gain_dB.setter
    def rx_gain_dB(self, v):
        self._rx_gain_dB.value = v

    @property
    def lo_freq_hz(self):
        return self._lo_freq_hz.value

    @lo_freq_hz.setter
    def lo_freq_hz(self, v):
        self._lo_freq_hz.value = v

    @property
    def td_running(self):
        return bool(self._td_running.value)

    @td_running.setter
    def td_running(self, v):
        self._td_running.value = int(bool(v))

    @property
    def td_target_ntw_id(self):
        if not self._td_has_ntw_id.value:
            return None
        return self._td_target_ntw_id.value

    @td_target_ntw_id.setter
    def td_target_ntw_id(self, v):
        if v is None:
            self._td_has_ntw_id.value = 0
        else:
            self._td_target_ntw_id.value = int(v)
            self._td_has_ntw_id.value = 1

    @property
    def td_target_chipset(self):
        raw = self._td_chipset_arr.value
        return raw.decode() if raw else None

    @td_target_chipset.setter
    def td_target_chipset(self, v):
        self._td_chipset_arr.value = (v or "").encode()[:31]

    # legacy compat — RX pushes drop positions via queue now
    @property
    def rx_drop_positions(self):
        return []

    @rx_drop_positions.setter
    def rx_drop_positions(self, v):
        pass


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
            if r.get("freq_delta_hz") is not None:
                d["freq_delta_hz"] = r["freq_delta_hz"]

        for d in devices.values():
            seen = set()
            unique_seqs = []
            for s in d["seq_nums"]:
                if s not in seen:
                    seen.add(s)
                    unique_seqs.append(s)
            d["seq_nums"] = unique_seqs[-10:]
            d.setdefault("freq_delta_hz", None)

        dev_list = sorted(devices.values(), key=lambda x: x["ntw_id"])
        stats = dict(state.decode_stats)
        stats["n_unique_devices"] = len(dev_list)

        td_b64 = ""
        if state.td_latest_img:
            td_b64 = base64.b64encode(state.td_latest_img).decode("ascii")

        cs_stats = get_chipset_stats()

        return jsonify(
            devices=dev_list, stats=stats,
            td_img=td_b64, td_running=state.td_running,
            td_device_id=state.td_target_ntw_id,
            td_chipset=state.td_target_chipset,
            td_status=state.td_status,
            td_decode_info=state.td_decode_info,
            chipset_stats=cs_stats,
            known_chipsets=sorted(config.SYNTH_RES.keys()),
            lo_freq_hz=state.lo_freq_hz,
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


@app.route("/api/lo", methods=["POST"])
def api_lo():
    data = flask_request.get_json(silent=True) or {}
    delta = data.get("delta_khz", 0)
    new_freq = state.lo_freq_hz + int(delta) * 1000
    state.lo_freq_hz = new_freq
    return jsonify(lo_freq_hz=new_freq)


@app.route("/api/timedomain", methods=["GET", "POST"])
def api_timedomain():
    if flask_request.method == "POST":
        data = flask_request.get_json(silent=True) or {}
        action = data.get("action")
        if action == "start":
            chipset = data.get("chipset")
            dev_id = data.get("device_id")
            if chipset:
                state.td_target_chipset = chipset
                state.td_target_ntw_id = None
                state.td_running = True
            elif dev_id is not None:
                try:
                    state.td_target_ntw_id = int(dev_id)
                    state.td_target_chipset = None
                    state.td_running = True
                except (ValueError, TypeError):
                    return jsonify(error="Invalid device_id"), 400
        elif action == "stop":
            state.td_running = False
    with state.lock:
        status = state.td_status
    return jsonify(
        running=state.td_running, device_id=state.td_target_ntw_id,
        chipset=state.td_target_chipset, status=status,
    )


@app.route("/api/td_iq", methods=["GET"])
def api_td_iq():
    """Download the current TD IQ capture as a .npy file."""
    with state.lock:
        seg = state.td_iq_segment
    if seg is None:
        return jsonify(error="No IQ capture available"), 404
    buf = io.BytesIO()
    np.save(buf, seg)
    buf.seek(0)
    return send_file(
        buf, mimetype="application/octet-stream",
        as_attachment=True, download_name="td_capture.npy",
    )


@app.route("/api/td_info", methods=["GET"])
def api_td_info():
    """Return just the decode_info for the current TD capture."""
    with state.lock:
        info = state.td_decode_info
    if info is None:
        return jsonify(error="No capture available"), 404
    return jsonify(info)


@app.route("/api/packets", methods=["GET"])
def api_packets():
    """Poll-and-drain: return all decodes since last call as JSONL, then clear.

    Each line is a JSON object with: device_id, seq_num, device_type,
    timestamp, rssi_dB, channel_num, freq_offset_hz.
    """
    with state.lock:
        entries = list(state.packet_feed)
        state.packet_feed.clear()

    lines = []
    for e in entries:
        lines.append(json.dumps({
            "device_id": e["ntw_id_hex"],
            "seq_num": e["seq_num"],
            "device_type": e.get("chipset", ""),
            "timestamp": e.get("unix_ts", 0),
            "rssi_dB": e.get("energy_dB"),
            "channel_num": e.get("channel_num"),
            "freq_offset_hz": e.get("freq_delta_hz"),
        }))
    payload = "\n".join(lines) + ("\n" if lines else "")
    return Response(payload, mimetype="application/x-ndjson")


# ===========================================================================
# Result drainer — receives processor output via mp.Queue
# ===========================================================================

def _drain_results(state):
    """Background thread: pull results from the processor process."""
    import queue as _queue
    while state.running.is_set():
        try:
            r = state.result_queue.get(timeout=0.1)
        except _queue.Empty:
            continue
        with state.lock:
            if r.get("img"):
                state.latest_img = r["img"]
            if r.get("detections") is not None:
                state.latest_detections = r["detections"]
            if r.get("decode_entries"):
                state.decode_results.extend(r["decode_entries"])
                state.decode_results[:] = state.decode_results[-config.MAX_DECODE_HISTORY:]
                state.packet_feed.extend(r["decode_entries"])
                state.packet_feed[:] = state.packet_feed[-1000:]
            if r.get("stats"):
                state.decode_stats = r["stats"]
            if r.get("td_img") is not None:
                state.td_latest_img = r["td_img"]
            if r.get("td_status") is not None:
                state.td_status = r["td_status"]
            if r.get("td_decode_info") is not None:
                state.td_decode_info = r["td_decode_info"]
            if r.get("td_iq_segment") is not None:
                state.td_iq_segment = r["td_iq_segment"]


# ===========================================================================
# Entry point
# ===========================================================================

def main():
    """Start RX thread, processor process, result drainer, and Flask."""
    state.running.set()

    # Fork the processor BEFORE starting any threads (safe on macOS)
    proc = mp.Process(
        target=processor_main,
        args=(
            _IQ_SHM_NAME,
            state._buf_write_idx,
            state._rx_peak_frac,
            state._rx_overflows,
            state._rx_gain_dB,
            state._td_running,
            state._td_target_ntw_id,
            state._td_has_ntw_id,
            state._td_chipset_arr,
            state.running,
            state.drop_queue,
            state.result_queue,
        ),
        daemon=True,
    )
    proc.start()

    rx_thread = threading.Thread(target=rx_loop, args=(state,), daemon=True)
    rx_thread.start()

    drain_thread = threading.Thread(target=_drain_results, args=(state,),
                                    daemon=True)
    drain_thread.start()

    print("[main] RX thread + processor process started.")
    print(f"[main] Open http://localhost:{config.FLASK_PORT} in a browser.")

    try:
        app.run(
            host="0.0.0.0",
            port=config.FLASK_PORT,
            threaded=True,
            use_reloader=False,
        )
    finally:
        state.cleanup_shm()
