"""IQ capture endpoint — records the next N seconds of live IQ samples.

Usage:
    GET /api/iq_capture?seconds=<int>

Blocks until the requested seconds of fresh IQ data have been written to
the circular buffer, then streams the samples back as a .npy file.
Metadata is returned in response headers:
    X-Sample-Rate-Hz, X-Center-Freq-Hz, X-Duration-S, X-N-Samples
"""

from __future__ import annotations

import io
import time

import numpy as np
from flask import Blueprint, Response, jsonify, send_file
from flask import request as flask_request

bp = Blueprint("iq_capture", __name__)

# Hard cap: no single request may ask for more than this many seconds.
_MAX_SECONDS = 60


def _samples_advanced(start: int, end: int, buf_size: int) -> int:
    """Number of samples written from *start* to *end* in a circular buffer.

    Handles exactly one wrap-around (safe as long as the requested duration
    is shorter than the buffer length, which is enforced above).
    """
    if end >= start:
        return end - start
    return buf_size - start + end


@bp.route("/api/iq_capture", methods=["GET"])
def api_iq_capture():
    # Lazy import to avoid circular dependency (state lives in app.py).
    from . import config
    from .app import state

    # --- parse & validate ---
    raw = flask_request.args.get("seconds", "")
    try:
        seconds = int(raw)
    except (ValueError, TypeError):
        return jsonify(error="'seconds' must be an integer"), 400

    if seconds < 1:
        return jsonify(error="'seconds' must be >= 1"), 400

    if seconds > _MAX_SECONDS:
        return jsonify(error=f"'seconds' must be <= {_MAX_SECONDS}"), 400

    n_samples = seconds * config.SAMPLE_RATE
    buf_size = config.IQ_BUFFER_SIZE

    # Read in chunks of at most half the buffer so we always read before
    # new samples overwrite what we haven't consumed yet.
    chunk_n = buf_size // 2
    chunks = []
    remaining = n_samples
    chunk_start = int(state.buf_write_idx)

    while remaining > 0:
        this_n = min(chunk_n, remaining)
        deadline = time.monotonic() + (this_n / config.SAMPLE_RATE) + 5.0

        while True:
            current_wi = int(state.buf_write_idx)
            if _samples_advanced(chunk_start, current_wi, buf_size) >= this_n:
                break
            if time.monotonic() > deadline:
                return jsonify(error="Timed out waiting for IQ samples from SDR"), 504
            time.sleep(0.05)

        s = chunk_start
        e = (chunk_start + this_n) % buf_size
        if s < e:
            chunks.append(np.array(state.iq_buffer[s:e]))
        else:
            chunks.append(np.concatenate([
                np.array(state.iq_buffer[s:]),
                np.array(state.iq_buffer[:e]),
            ]))

        chunk_start = (chunk_start + this_n) % buf_size
        remaining -= this_n

    segment = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]

    # --- serialise and stream back ---
    buf = io.BytesIO()
    np.save(buf, segment)
    buf.seek(0)

    fname = f"iq_{int(time.time())}_{seconds}s.npy"
    resp = send_file(
        buf,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=fname,
    )
    resp.headers["X-Sample-Rate-Hz"] = str(config.SAMPLE_RATE)
    resp.headers["X-Center-Freq-Hz"] = str(int(state.lo_freq_hz))
    resp.headers["X-Duration-S"] = str(seconds)
    resp.headers["X-N-Samples"] = str(int(len(segment)))
    return resp
