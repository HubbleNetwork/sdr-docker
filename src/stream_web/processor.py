"""Processing thread: spectrogram computation, decode, and image rendering.

Runs every DECODE_INTERVAL_S (0.5 s):
  1. Compute a 0.5 s spectrogram chunk from the latest IQ data.
  2. Run the dual-protocol decoder on the latest 1 s of IQ data.
  3. Maintain persistent detection history for box overlay.
  4. Render the rolling spectrogram image with detection boxes.
  5. Optionally render a time-domain plot for a tracked device.
"""

import time

import numpy as np

from . import config
from .decoder import decode_signal
from .spectrogram import compute_spec_chunk, render_spec_image, render_td_plot


def process_loop(state):
    """Main processing loop — runs as a daemon thread."""
    while state.running.is_set():
        t0 = time.perf_counter()

        with state.lock:
            full_buf = state.iq_buffer.copy()
            widx = state.buf_write_idx

        ordered = np.concatenate([full_buf[widx:], full_buf[:widx]])

        # 1) Spectrogram: compute latest 0.5 s chunk
        t_spec0 = time.perf_counter()
        spec_chunk_iq = ordered[-config.SPEC_CHUNK_SAMPLES:]
        try:
            sxx_chunk = compute_spec_chunk(spec_chunk_iq)
            state.spec_chunks.append(sxx_chunk)
        except Exception as e:
            print(f"[PROC] Spec error: {e}")
        t_spec_ms = (time.perf_counter() - t_spec0) * 1000

        # 2) Decode last 1 second
        t_dec0 = time.perf_counter()
        decode_chunk = ordered[-config.DECODE_SAMPLES:]
        try:
            packets, detections = decode_signal(decode_chunk)
        except Exception as e:
            print(f"[PROC] Decode error: {e}")
            packets, detections = [], []
        t_dec_ms = (time.perf_counter() - t_dec0) * 1000

        # 3) Update persistent detection history
        for d in state.detection_history:
            d["offset_from_right"] += config.SPEC_CHUNK_S
        state.detection_history = [
            d for d in state.detection_history
            if d["offset_from_right"] <= config.SPEC_DURATION_S
        ]
        for det in detections:
            new_offset = config.DECODE_WINDOW_S - det["time_s"]
            is_dup = False
            for existing in state.detection_history:
                if (abs(existing["offset_from_right"] - new_offset) < 0.15
                        and abs(existing["freq_hz"] - det.get("F0_hz", det["freq_hz"])) < 5000):
                    is_dup = True
                    break
            if not is_dup:
                state.detection_history.append({
                    "offset_from_right": new_offset,
                    "freq_hz": det.get("F0_hz", det["freq_hz"]),
                    "phy_ver": det["phy_ver"],
                    "signal_duration_s": det.get(
                        "signal_duration_s",
                        det.get("preamble_duration_s", 0.05),
                    ),
                    "chipset": det.get("chipset", "v-1"),
                })

        # 4) Render spectrogram image
        t_render0 = time.perf_counter()
        try:
            img_bytes = render_spec_image(list(state.spec_chunks), state.detection_history)
            with state.lock:
                state.latest_img = img_bytes
                state.latest_detections = detections
        except Exception as e:
            print(f"[PROC] Render error: {e}")
        t_render_ms = (time.perf_counter() - t_render0) * 1000

        dt_ms = (time.perf_counter() - t0) * 1000

        with state.lock:
            ts = time.strftime("%H:%M:%S")
            for pkt in packets:
                ver = pkt["phy_ver"]
                ntw_hex = f"0x{pkt['ntw_id']:09X}" if ver == -1 else f"0x{pkt['ntw_id']:08X}"
                state.decode_results.append({
                    "timestamp": ts,
                    "phy_ver": ver,
                    "ntw_id": pkt["ntw_id"],
                    "ntw_id_hex": ntw_hex,
                    "seq_num": pkt["seq_num"],
                    "energy_dB": round(pkt["total_energy_dB"], 1),
                    "chipset": pkt.get("chipset", ""),
                })
            state.decode_results[:] = state.decode_results[-config.MAX_DECODE_HISTORY:]
            state.decode_stats = {
                "process_time_ms": round(dt_ms, 1),
                "n_detections": len(packets),
                "timestamp": ts,
                "t_spec_ms": round(t_spec_ms, 1),
                "t_render_ms": round(t_render_ms, 1),
                "t_decode_ms": round(t_dec_ms, 1),
                "rx_gain_dB": round(state.rx_gain_dB, 1),
                "rx_peak_pct": round(state.rx_peak_frac * 100, 1),
            }

        # 5) Time-domain plot for tracked device
        if state.td_running and state.td_target_ntw_id is not None:
            pkt_ids = [p["ntw_id"] for p in packets]
            td_match = [p for p in packets if p["ntw_id"] == state.td_target_ntw_id]
            if td_match:
                pkt = td_match[0]
                td_samples = int(config.TD_WINDOW_S * config.SAMPLE_RATE)
                td_center = int(round(pkt["time_s"] * config.SAMPLE_RATE))
                td_start = max(0, td_center - td_samples // 4)
                td_end = min(td_start + td_samples, len(decode_chunk))
                if td_start < td_end:
                    td_seg = decode_chunk[td_start:td_end]
                    try:
                        td_img = render_td_plot(td_seg)
                        with state.lock:
                            state.td_latest_img = td_img
                            state.td_status = (
                                f"Updated -- t={pkt['time_s']:.3f}s, {len(td_seg):,} samples"
                            )
                    except Exception as e:
                        print(f"[TD] Plot error: {e}")
                        with state.lock:
                            state.td_status = f"Render error: {e}"
            else:
                with state.lock:
                    state.td_status = (
                        f"Searching... ({len(packets)} pkts this cycle, IDs: {pkt_ids[:5]})"
                    )

        if config.VERBOSE:
            print(
                f"[PROC] total={dt_ms:6.1f} ms | spec={t_spec_ms:5.1f} | "
                f"render={t_render_ms:5.1f} | decode={t_dec_ms:5.1f} | det={len(packets)}"
            )

        elapsed = time.perf_counter() - t0
        sleep_s = max(0, config.DECODE_INTERVAL_S - elapsed)
        if sleep_s > 0:
            time.sleep(sleep_s)
