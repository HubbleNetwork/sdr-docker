"""Offline diagnostics for a recorded IQ segment (the /api/record_analyze endpoint)

  1. Packet detection   -- decode_packets(): run the decoder over the recording, keep the
                           decoded packets, anchor symbol edges on the preamble (start_sample).
  2. Per-symbol analyses -- one function per question, each run on a single packet:
       analyze_timing()    duration / gap / clock drift   (cf. validate_symbol_gaps)
       analyze_amplitude() per-symbol RMS dBFS + SNR
       analyze_channels()  dominant freq vs channel window (cf. validate_channel_v2)
       analyze_chipset()   measured synth-res vs known chipsets
       expected_hops()     the expected channel-hop schedule
  3. Orchestration      -- analyze_recording(): decode, pick a representative packet, run
                           the analyses on it, and summarise the capture.
  4. Report formatting  -- build_report(): render the plain-text file the customer returns.

Note: Currently, this only analyzes packets that successfully decode
"""
import numpy as np
from hubble_satnet_decoder import decode_signal

from . import config
from .timing import correct_symbol_edges, dominant_symbol_freq, symbol_amplitudes_dbfs

# Ideal symbol grid, used only as the reference the drift measurement is taken against --
_SYM_GRID_MS, _GAP_GRID_MS = 8.0, 0.8

# ===========================================================================
# 1. Packet detection
# ===========================================================================

def _window_offsets(n: int, win: int) -> list[int]:
    """Decoder-window start offsets covering the recording, overlapping by more than the
    longest packet (~0.53 s) so every packet lands fully inside at least one window."""
    if n <= win:
        return [0]
    step = max(1, win - int(0.6 * config.SAMPLE_RATE))
    offsets = list(range(0, n - win + 1, step))
    if offsets[-1] != n - win:
        offsets.append(n - win)
    return offsets


def decode_packets(iq: np.ndarray) -> list[dict]:
    """Run the decoder over the recording and return each decoded packet once, in time order.

    The decoder works on a fixed-size window, so we slide overlapping windows across the
    recording and keep the fully-decoded packets, deduped by (network id, sequence number)
    -- a packet caught in two overlapping windows is reported once. The decoded *attempt*
    supplies start_sample (the preamble location); the *packet* supplies the decode-only
    fields (freq offset, PDU RS corrections). 
    """
    sr, win = config.SAMPLE_RATE, config.DECODE_SAMPLES
    seen: dict = {}
    for off in _window_offsets(len(iq), win):
        try:
            packets, _detections, attempts = decode_signal(iq[off:off + win])
        except Exception:              # one bad window shouldn't abort the whole capture
            continue
        starts = {(a.get("ntw_id"), a.get("seq_num")): a.get("start_sample")
                  for a in attempts
                  if a.get("decoded") and a.get("start_sample") is not None}
        for p in packets:
            key = (p.get("ntw_id"), p.get("seq_num"))
            if key in seen or starts.get(key) is None:
                continue
            d = dict(p)
            d["abs_start"] = off + starts[key]
            d["abs_time_s"] = d["abs_start"] / sr
            n_sym, slot, sym_len = config.packet_symbol_grid(d)
            d["edges"] = correct_symbol_edges(iq, d["abs_start"], 0, n_sym, 0, slot, sym_len)
            seen[key] = d
    return sorted(seen.values(), key=lambda d: d["abs_start"])


# ===========================================================================
# 2. Per-symbol analyses (each answers one question about one packet)
# ===========================================================================

def analyze_timing(pkt: dict) -> dict:
    """Per-symbol duration, inter-symbol gap, and clock drift.

    Drift is the symbol's midpoint minus where an ideal 8ms-symbol/0.8ms-gap grid says it
    should be; the symbol-0 offset is subtracted as a baseline so only accumulating clock
    skew remains. Rate is the drift increment from the previous symbol. Mirrors
    validate_symbol_gaps() in pluto_one_second_trace.py.
    """
    sr, edges = config.SAMPLE_RATE, pkt["edges"]
    if len(edges) < 2:
        return {"per_symbol": [], "sym_mean_ms": None, "gap_mean_ms": None,
                "drift_us_per_sym": None, "total_drift_us": None}

    # Create nominal template (8ms symbol + 0.8ms gap) and compare real packet to it
    _n, slot, sym_len = config.packet_symbol_grid(pkt)
    tmpl_mid = [edges[0][0] + i * slot + sym_len // 2 for i in range(len(edges))]
    drift = [((s + e) // 2 - t) / sr * 1e6 for (s, e), t in zip(edges, tmpl_mid)]
    drift = [d - drift[0] for d in drift]          

    rows, durs, gaps = [], [], []
    for i, (s, e) in enumerate(edges):
        dur = (e - s) / sr * 1e6
        gap = (edges[i + 1][0] - e) / sr * 1e6 if i < len(edges) - 1 else None
        durs.append(dur)
        if gap is not None:
            gaps.append(gap)
        rows.append({"idx": i, "duration_us": round(dur, 2),
                     "gap_us": round(gap, 2) if gap is not None else None,
                     "drift_us": round(drift[i], 2),
                     "rate_us_per_sym": round(drift[i] - drift[i - 1], 2) if i else 0.0})

    return {"per_symbol": rows,
            "sym_mean_ms": round(float(np.mean(durs)) / 1e3, 4),
            "gap_mean_ms": round(float(np.mean(gaps)) / 1e3, 4) if gaps else None,
            "total_drift_us": round(drift[-1], 2),
            "drift_us_per_sym": round(drift[-1] / (len(edges) - 1), 2)}


def analyze_amplitude(iq: np.ndarray, pkt: dict) -> dict:
    """Per-symbol RMS amplitude (dBFS) and SNR above the inter-symbol noise floor."""
    amps, gaps = symbol_amplitudes_dbfs(iq, pkt["edges"], config.ADC_FULL_SCALE)
    if not amps:
        return {"per_symbol": [], "mean_dbfs": None, "dropoff_db": None,
                "noise_floor_dbfs": None, "snr_db": None}

    floor = float(np.median(gaps)) if gaps else None
    rows = [{"idx": i, "amp_dbfs": round(float(a), 2),
             "snr_db": round(float(a) - floor, 2) if floor is not None else None}
            for i, a in enumerate(amps)]
    return {"per_symbol": rows,
            "mean_dbfs": round(float(np.mean(amps)), 2),
            "dropoff_db": round(float(max(amps) - min(amps)), 2),
            "noise_floor_dbfs": round(floor, 2) if floor is not None else None,
            "snr_db": round(float(np.mean(amps)) - floor, 2) if floor is not None else None}


def _calibrate_spacing(slots, channel_num, base_freq, step):
    """Inter-channel spacing (Hz), estimated as the median over PDU hops with enough symbols;
    falls back to the configured CHANNEL_SPACING when the packet is too short to calibrate.
    Returns (spacing, calibrated_from_pdu)."""
    est = []
    for slot_num, ch, fs in slots:
        if slot_num == 1 or ch == channel_num or len(fs) < 8:
            continue
        midrange = 0.5 * (min(fs) + max(fs))
        e = (midrange - 31.5 * step - base_freq) / (ch - channel_num)
        if 0.7 * config.CHANNEL_SPACING < abs(e) < 1.3 * config.CHANNEL_SPACING:
            est.append(e)
    if est:
        return float(np.median(est)), True
    return float(config.CHANNEL_SPACING), False


def analyze_channels(iq: np.ndarray, pkt: dict) -> dict | None:
    """Per-symbol dominant frequency vs the expected channel window.

    Calibrates the intra-channel FSK step from the two extreme preamble tones (symbol 0 =
    value-63, symbol 1 = value-0), calibrates inter-channel spacing from the PDU hops, then
    window-checks every symbol against its channel. Mirrors validate_channel_v2() in
    pluto_one_second_trace.py. Returns None if hop parameters are unavailable.
    """
    edges = pkt["edges"]
    rotated = config.rotated_hop_sequence(pkt.get("channel_num"), pkt.get("hop_seq_idx"))
    if rotated is None or len(edges) < 2:
        return None

    sr, sps, hop = config.SAMPLE_RATE, config.samples_per_symbol, config.NUM_SYM_PER_HOP
    ch = rotated[0]                                        # == channel_num, by construction
    freqs = [dominant_symbol_freq(iq[s:s + sps], sr) for s, _ in edges]

    f63, f0 = freqs[0], freqs[1]
    step = abs(f63 - f0) / 63.0
    width = step * 64.0
    base = min(f63, f0)

    # Group symbols into hops of NUM_SYM_PER_HOP; hop h sits on channel rotated[h].
    slots = [(h + 1, rotated[h % len(rotated)], freqs[b:b + hop])
             for h, b in enumerate(range(0, len(freqs), hop))]
    spacing, from_pdu = _calibrate_spacing(slots, ch, base, step)
    expected = {c: base + (c - ch) * spacing for c in rotated}

    rows, gi = [], 0
    for _slot_num, c, fs in slots:
        lo, hi = expected[c], expected[c] + width
        for fr in fs:
            ok = lo <= fr < hi
            rows.append({"idx": gi, "channel": int(c), "freq_hz": round(fr, 1),
                         "in_window": ok,
                         "off_by_hz": 0.0 if ok else round(min(abs(fr - lo), abs(fr - hi)), 1)})
            gi += 1

    return {"calibration": {"preamble_f63_hz": round(f63, 1), "preamble_f0_hz": round(f0, 1),
                            "step_hz": round(step, 1), "channel_width_hz": round(width, 1),
                            "spacing_hz": round(spacing, 1), "spacing_from_pdu": from_pdu,
                            "rotated_channels": [int(c) for c in rotated]},
            "per_symbol": rows,
            "n_in_window": sum(r["in_window"] for r in rows),
            "all_valid": all(r["in_window"] for r in rows)}


def analyze_chipset(pkt: dict) -> dict:
    """Chipset and synthesizer resolution as reported by the decoder: the measured value and
    the chipset's nominal table value, printed as-is (no matching / threshold)."""
    meas = pkt.get("measured_synth_res")
    name = pkt.get("chipset")
    return {"chipset": name,
            "measured_synth_res": round(float(meas), 2) if meas is not None else None,
            "nominal_synth_res": config.SYNTH_RES.get(name)}


def expected_hops(pkt: dict) -> dict | None:
    """Expected channel-hop schedule (start channel + rotated sequence). None if unavailable."""
    rotated = config.rotated_hop_sequence(pkt.get("channel_num"), pkt.get("hop_seq_idx"))
    if rotated is None:
        return None
    n_sym, _, _ = config.packet_symbol_grid(pkt)
    n_hops = -(-n_sym // config.NUM_SYM_PER_HOP)                    # ceil division
    return {"hop_seq_idx": int(pkt["hop_seq_idx"]), "start_channel": int(rotated[0]),
            "expected_channels": [int(rotated[h % len(rotated)]) for h in range(n_hops)]}


# ===========================================================================
# 3. Capture-level summary
# ===========================================================================

def _rs_corrections(packets: list[dict]) -> dict:
    """Reed-Solomon correction effort across packets (printed as-is; higher = less margin)."""
    def col(key):
        return [float(p[key]) for p in packets
                if isinstance(p.get(key), (int, float)) and not isinstance(p.get(key), bool)]
    hdr, pdu = col("header_n_corr"), col("pdu_n_corr")
    return {"header_mean": round(float(np.mean(hdr)), 2) if hdr else None,
            "pdu_mean": round(float(np.mean(pdu)), 2) if pdu else None,
            "pdu_max": int(max(pdu)) if pdu else None}


# ===========================================================================
# 4. Orchestration
# ===========================================================================

def _packet_info(pkt: dict) -> dict:
    return {"abs_time_s": round(float(pkt["abs_time_s"]), 4),
            "channel_num": pkt.get("channel_num"), "chipset": pkt.get("chipset")}


def _packet_line(iq: np.ndarray, pkt: dict) -> dict:
    """One-line-per-packet summary for the all-packets table."""
    t, a = analyze_timing(pkt), analyze_amplitude(iq, pkt)
    return {**_packet_info(pkt), "sym_mean_ms": t["sym_mean_ms"], "gap_mean_ms": t["gap_mean_ms"],
            "total_drift_us": t["total_drift_us"], "amp_mean_dbfs": a["mean_dbfs"],
            "snr_db": a["snr_db"]}


def analyze_recording(iq: np.ndarray) -> dict:
    """Analyze a recorded IQ segment: decode the packets, then run the per-symbol timing,
    amplitude, frequency/channel, and chipset analyses on the representative packet. Returns
    the report dict consumed by build_report (the endpoint adds the 'capture' block)."""
    packets = decode_packets(iq)
    if not packets:
        return {"summary": {"packets": 0}, "representative": None, "packets": []}

    rep = max(packets, key=lambda p: p.get("num_pdu_symbols") or 0)   # fullest = richest tables
    return {
        "summary": {"packets": len(packets), "rs_corrections": _rs_corrections(packets),
                    "chipset": analyze_chipset(rep),
                    "freq_delta_hz": round(float(rep["freq_delta_hz"]), 1)
                    if rep.get("freq_delta_hz") is not None else None},
        "representative": {"info": _packet_info(rep), "timing": analyze_timing(rep),
                           "amplitude": analyze_amplitude(iq, rep),
                           "channels": analyze_channels(iq, rep), "hops": expected_hops(rep)},
        "packets": [_packet_line(iq, p) for p in packets],
    }


# ===========================================================================
# 5. Report formatting
# ===========================================================================

def build_report(report: dict) -> str:
    """Render the analysis dict into the plain-text diagnostic file the customer returns."""
    s = report["summary"]
    lines = ["=== Hubble sat-record diagnostic ==="]
    cap = report.get("capture")
    if cap:
        lines.append(f"capture: {cap['seconds']} s @ {cap['sample_rate_hz']} Hz, "
                     f"center {cap['center_freq_hz'] / 1e6:.4f} MHz, {cap['n_samples']} samples")
    lines.append(f"decoded packets: {s['packets']}")

    rep = report.get("representative")
    if not rep:
        lines.append("No packets decoded -- check the device is transmitting on the "
                     "expected channel.")
        return "\n".join(lines) + "\n"

    info = rep["info"]
    lines += ["", "=== representative packet ===",
              f"  t={info['abs_time_s']}s  channel {info['channel_num']}  "
              f"chipset {info['chipset']}"]
    _timing_section(lines, rep["timing"])
    _channel_section(lines, rep["channels"], rep["hops"])
    _amplitude_section(lines, rep["amplitude"])
    _frequency_section(lines, s)

    rs = s["rs_corrections"]
    lines += ["", f"RS corrections: header_mean={rs['header_mean']} "
              f"pdu_mean={rs['pdu_mean']} pdu_max={rs['pdu_max']}"]
    _packet_table(lines, report["packets"])
    return "\n".join(lines) + "\n"


def _timing_section(lines: list, t: dict) -> None:
    lines += ["", f"PER-SYMBOL TIMING (duration, gap; drift measured vs an ideal "
              f"{_SYM_GRID_MS * 1e3:.0f} us symbol / {_GAP_GRID_MS * 1e3:.0f} us gap grid)"]
    if not t["per_symbol"]:
        lines.append("  n/a (not enough symbols)")
        return
    for r in t["per_symbol"]:
        gap = f", gap={r['gap_us']:.2f} us" if r["gap_us"] is not None else ""
        lines.append(f"  Symbol {r['idx']:>2}: duration={r['duration_us']:.2f} us{gap}, "
                     f"drift={r['drift_us']:+.2f} us, rate={r['rate_us_per_sym']:+.2f} us/sym")
    lines.append(f"  Overall drift: {t['total_drift_us']:+.2f} us "
                 f"({t['drift_us_per_sym']:+.2f} us/symbol over {len(t['per_symbol'])} symbols)")


def _channel_section(lines: list, ch: dict | None, hops: dict | None) -> None:
    lines += ["", "PER-SYMBOL FREQUENCY / CHANNEL HOPPING"]
    if hops:
        lines.append(f"  hop sequence idx {hops['hop_seq_idx']}, start channel "
                     f"{hops['start_channel']}, expected channels {hops['expected_channels']}")
    if not ch:
        lines.append("  n/a (channel/hop info unavailable for this packet)")
        return
    c = ch["calibration"]
    lines += [f"  calibration: preamble sym63={c['preamble_f63_hz']} Hz "
              f"sym0={c['preamble_f0_hz']} Hz, FSK step={c['step_hz']} Hz, "
              f"channel width={c['channel_width_hz']} Hz",
              f"  channel spacing: {c['spacing_hz']} Hz "
              f"({'calibrated from PDU hops' if c['spacing_from_pdu'] else 'default'})"]
    for r in ch["per_symbol"]:
        mark = "ok" if r["in_window"] else f"OUT by {r['off_by_hz']} Hz"
        lines.append(f"  Symbol {r['idx']:>2}: ch {r['channel']:>2}, "
                     f"freq={r['freq_hz']:>10.1f} Hz  [{mark}]")
    lines.append(f"  channel validation: {'PASS' if ch['all_valid'] else 'FAIL'} "
                 f"({ch['n_in_window']}/{len(ch['per_symbol'])} symbols in window)")


def _amplitude_section(lines: list, a: dict) -> None:
    lines += ["", "PER-SYMBOL AMPLITUDE (RMS dBFS; SNR above the inter-symbol noise floor)"]
    if not a["per_symbol"]:
        lines.append("  n/a (not enough symbols)")
        return
    for r in a["per_symbol"]:
        snr = f", snr={r['snr_db']:.2f} dB" if r["snr_db"] is not None else ""
        lines.append(f"  Symbol {r['idx']:>2}: amp={r['amp_dbfs']:.2f} dBFS{snr}")
    lines.append(f"  summary: mean {a['mean_dbfs']} dBFS, dropoff (p-p) {a['dropoff_db']} dB, "
                 f"noise floor {a['noise_floor_dbfs']} dBFS, SNR {a['snr_db']} dB")


def _frequency_section(lines: list, s: dict) -> None:
    ch = s["chipset"]
    lines += ["", "FREQUENCY / CHIPSET",
              f"  offset from center: {s['freq_delta_hz']} Hz",
              f"  chipset (decoder): {ch['chipset']}",
              f"  synth-res: measured {ch['measured_synth_res']} Hz "
              f"(chipset nominal {ch['nominal_synth_res']} Hz)"]


def _packet_table(lines: list, packets: list[dict]) -> None:
    lines += ["", "PER-PACKET (all packets)"]
    for p in packets:
        lines.append(f"  t={p['abs_time_s']}s ch{p['channel_num']} {p['chipset']}: "
                     f"sym={p['sym_mean_ms']}ms gap={p['gap_mean_ms']}ms "
                     f"drift={p['total_drift_us']}us amp={p['amp_mean_dbfs']}dBFS "
                     f"snr={p['snr_db']}dB")
