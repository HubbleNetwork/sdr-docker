"""Processor — runs in a **separate OS process** to avoid GIL contention
with the real-time SDR RX thread.

Communicates with the main process via:
  * POSIX shared memory  — IQ circular buffer (read-only here)
  * multiprocessing.Value — buf_write_idx and control scalars
  * multiprocessing.Queue — results back to main, drop positions in
"""

import queue
import time
from collections import deque
from multiprocessing import shared_memory

import numpy as np

from . import config
from .decoder import decode_signal
from .spectrogram import compute_spec_chunk, render_spec_image, render_td_plot


def processor_main(shm_name, buf_write_idx_val, rx_peak_frac_val,
                   rx_overflows_val, rx_gain_dB_val, td_running_val,
                   td_ntw_id_val, td_has_ntw_val, td_chipset_arr,
                   running_event, drop_queue, result_queue):
    """Entry point for the processor process."""

    shm = shared_memory.SharedMemory(name=shm_name, create=False)
    iq_buffer = np.ndarray(config.IQ_BUFFER_SIZE, dtype=np.complex64,
                           buffer=shm.buf)

    spec_chunks: deque = deque(maxlen=config.MAX_SPEC_CHUNKS)
    detection_history: list[dict] = []

    buf_len = config.IQ_BUFFER_SIZE

    print("[PROC] Processor process started (separate GIL).", flush=True)

    while running_event.is_set():
        t0 = time.perf_counter()

        widx = buf_write_idx_val.value

        # Drain drop positions from queue
        drop_positions = []
        while True:
            try:
                drop_positions.append(drop_queue.get_nowait())
            except queue.Empty:
                break

        decode_start = (widx - config.DECODE_SAMPLES) % buf_len
        drop_sample_offsets = []
        for dp in drop_positions:
            if decode_start < widx:
                if decode_start <= dp < widx:
                    drop_sample_offsets.append(dp - decode_start)
            else:
                if dp >= decode_start:
                    drop_sample_offsets.append(dp - decode_start)
                elif dp < widx:
                    drop_sample_offsets.append(buf_len - decode_start + dp)

        def _extract_last(n):
            start = (widx - n) % buf_len
            if start < widx:
                return iq_buffer[start:widx].copy()
            return np.concatenate([iq_buffer[start:], iq_buffer[:widx]])

        # 1) Spectrogram
        t_spec0 = time.perf_counter()
        spec_chunk_iq = _extract_last(config.SPEC_CHUNK_SAMPLES)
        try:
            sxx_chunk = compute_spec_chunk(spec_chunk_iq)
            spec_chunks.append(sxx_chunk)
        except Exception as e:
            print(f"[PROC] Spec error: {e}")
        t_spec_ms = (time.perf_counter() - t_spec0) * 1000

        # 2) Decode
        t_dec0 = time.perf_counter()
        decode_chunk = _extract_last(config.DECODE_SAMPLES)
        try:
            packets, detections, attempts = decode_signal(decode_chunk)
        except Exception as e:
            print(f"[PROC] Decode error: {e}")
            packets, detections, attempts = [], [], []
        t_dec_ms = (time.perf_counter() - t_dec0) * 1000

        # Annotate sample-drop failures
        if drop_sample_offsets and attempts:
            for att in attempts:
                if att.get("decoded"):
                    continue
                pkt_start = att.get("start_sample",
                                    int(att.get("time_s", 0) * config.SAMPLE_RATE))
                dur_s = att.get("signal_duration_s", 0.05)
                pkt_end = pkt_start + int(dur_s * config.SAMPLE_RATE)
                for doff in drop_sample_offsets:
                    if pkt_start <= doff <= pkt_end:
                        att["sample_drop"] = True
                        if att.get("reason") and att["reason"] != "ok":
                            att["reason"] = f"{att['reason']}+sample_drop"
                        else:
                            att["reason"] = "sample_drop"
                        break

        # 3) Detection history (process-local)
        for d in detection_history:
            d["offset_from_right"] += config.SPEC_CHUNK_S
        detection_history = [
            d for d in detection_history
            if d["offset_from_right"] <= config.SPEC_DURATION_S
        ]
        for det in detections:
            new_offset = config.DECODE_WINDOW_S - det["time_s"]
            is_dup = False
            for existing in detection_history:
                if (abs(existing["offset_from_right"] - new_offset) < 0.15
                        and abs(existing["freq_hz"] - det.get("F0_hz", det["freq_hz"])) < 5000):
                    is_dup = True
                    break
            if not is_dup:
                detection_history.append({
                    "offset_from_right": new_offset,
                    "freq_hz": det.get("F0_hz", det["freq_hz"]),
                    "phy_ver": det["phy_ver"],
                    "signal_duration_s": det.get(
                        "signal_duration_s",
                        det.get("preamble_duration_s", 0.05),
                    ),
                    "chipset": det.get("chipset", "v-1"),
                })

        # 4) Render spectrogram
        t_render0 = time.perf_counter()
        img_bytes = b""
        try:
            img_bytes = render_spec_image(list(spec_chunks), detection_history)
        except Exception as e:
            print(f"[PROC] Render error: {e}")
        t_render_ms = (time.perf_counter() - t_render0) * 1000

        dt_ms = (time.perf_counter() - t0) * 1000

        ts = time.strftime("%H:%M:%S")
        decode_entries = []
        for pkt in packets:
            ver = pkt["phy_ver"]
            ntw_hex = f"0x{pkt['ntw_id']:09X}" if ver == -1 else f"0x{pkt['ntw_id']:08X}"
            decode_entries.append({
                "timestamp": ts,
                "phy_ver": ver,
                "ntw_id": pkt["ntw_id"],
                "ntw_id_hex": ntw_hex,
                "seq_num": pkt["seq_num"],
                "energy_dB": round(pkt["total_energy_dB"], 1),
                "chipset": pkt.get("chipset", ""),
                "freq_delta_hz": pkt.get("freq_delta_hz"),
            })

        stats = {
            "process_time_ms": round(dt_ms, 1),
            "n_detections": len(packets),
            "timestamp": ts,
            "t_spec_ms": round(t_spec_ms, 1),
            "t_render_ms": round(t_render_ms, 1),
            "t_decode_ms": round(t_dec_ms, 1),
            "rx_gain_dB": round(rx_gain_dB_val.value, 1),
            "rx_peak_pct": round(rx_peak_frac_val.value * 100, 1),
            "rx_overflows": rx_overflows_val.value,
        }

        # 5) Time-domain plot
        td_img = None
        td_status_str = None
        td_decode_info_out = None
        td_iq_seg_out = None

        td_on = bool(td_running_val.value)
        td_chipset_raw = td_chipset_arr.value
        td_chipset = td_chipset_raw.decode() if td_chipset_raw else None
        td_ntw_id = td_ntw_id_val.value if td_has_ntw_val.value else None

        if td_on:
            td_hit = None

            if td_chipset:
                ok_times = [p["time_s"] for p in packets
                            if p.get("chipset") == td_chipset]
                matches = [a for a in attempts
                           if a.get("chipset") == td_chipset
                           and not a.get("decoded")
                           and not any(abs(a.get("time_s", 0) - t) < 0.5
                                       for t in ok_times)]
                if matches:
                    td_hit = matches[0]
            elif td_ntw_id is not None:
                matches = [p for p in packets
                           if p["ntw_id"] == td_ntw_id]
                if matches:
                    td_hit = matches[0]
                    td_hit.setdefault("decoded", True)
                    td_hit.setdefault("reason", "ok")
                    td_hit.setdefault(
                        "start_sample",
                        int(round(matches[0]["time_s"] * config.SAMPLE_RATE)),
                    )

            if td_hit is not None:
                td_samples = int(config.TD_WINDOW_S * config.SAMPLE_RATE)
                td_center = td_hit.get(
                    "start_sample",
                    int(round(td_hit["time_s"] * config.SAMPLE_RATE)),
                )
                td_start = max(0, td_center - td_samples // 10)
                td_end = td_start + td_samples
                if td_end > len(decode_chunk):
                    td_hit = None
                if td_hit is not None:
                    td_seg = decode_chunk[td_start:td_end]
                    decode_info = dict(td_hit)
                    decode_info.setdefault("decoded", False)
                    decode_info.setdefault("reason", "unknown")
                    if not decode_info.get("energy_dB"):
                        decode_info["energy_dB"] = td_hit.get("total_energy_dB")
                    try:
                        td_img = render_td_plot(td_seg, decode_info=decode_info)
                        status = f"t={td_hit['time_s']:.3f}s"
                        if decode_info["decoded"]:
                            seq = decode_info.get("seq_num")
                            status += f" | DECODED seq={seq}"
                        else:
                            status += f" | FAILED: {decode_info['reason']}"
                        td_status_str = status
                        td_decode_info_out = {
                            k: v for k, v in decode_info.items()
                            if isinstance(v, (str, int, float, bool, list, type(None)))
                        }
                        td_iq_seg_out = td_seg.copy()
                    except Exception as e:
                        print(f"[TD] Plot error: {e}")
                        td_status_str = f"Render error: {e}"
            else:
                if td_chipset:
                    td_status_str = (
                        f"Waiting for {td_chipset} failure... "
                        f"({len(packets)} decoded this cycle)"
                    )
                elif td_ntw_id is not None:
                    pkt_ids = [p["ntw_id"] for p in packets]
                    td_status_str = (
                        f"Searching... ({len(packets)} pkts, "
                        f"IDs: {pkt_ids[:5]})"
                    )

        # Send results to main process
        result = {
            "img": img_bytes,
            "detections": detections,
            "decode_entries": decode_entries,
            "stats": stats,
            "td_img": td_img,
            "td_status": td_status_str,
            "td_decode_info": td_decode_info_out,
            "td_iq_segment": td_iq_seg_out,
        }
        try:
            result_queue.put_nowait(result)
        except Exception:
            pass

        if config.VERBOSE:
            print(
                f"[PROC] total={dt_ms:6.1f} ms | spec={t_spec_ms:5.1f} | "
                f"render={t_render_ms:5.1f} | decode={t_dec_ms:5.1f} | det={len(packets)}"
            )

        elapsed = time.perf_counter() - t0
        sleep_s = max(0, config.DECODE_INTERVAL_S - elapsed)
        if sleep_s > 0:
            time.sleep(sleep_s)

    shm.close()
    print("[PROC] Processor process exiting.", flush=True)
