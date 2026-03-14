"""Spectrogram computation and image rendering."""

import io

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.signal import spectrogram as scipy_spectrogram

from . import config

# -- Pre-computed LUT and font ----------------------------------------------

_VIRIDIS_LUT = (matplotlib.colormaps["viridis"](np.arange(256))[:, :3] * 255).astype(np.uint8)

try:
    _SPEC_FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size=14)
except Exception:
    try:
        _SPEC_FONT = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size=14)
    except Exception:
        _SPEC_FONT = ImageFont.load_default()


# ===========================================================================
# Spectrogram chunk computation
# ===========================================================================

def compute_spec_chunk(iq_chunk: np.ndarray) -> np.ndarray:
    """Compute the vis spectrogram for a 0.5 s IQ chunk. Returns Sxx_dB (freq x time)."""
    chunk = iq_chunk - iq_chunk.mean()
    f, t, Sxx = scipy_spectrogram(
        chunk, fs=config.SAMPLE_RATE, nperseg=config.NFFT_VIS,
        noverlap=config.NOVERLAP_VIS, return_onesided=False,
    )
    f = np.fft.fftshift(f)
    Sxx = np.fft.fftshift(Sxx, axes=0)
    dc_idx = len(f) // 2
    Sxx[dc_idx, :] = 0.0
    return (10.0 * np.log10(Sxx + 1e-12)).astype(np.float32)


# ===========================================================================
# Spectrogram image rendering
# ===========================================================================

def render_spec_image(chunks: list[np.ndarray], detections: list[dict] | None = None) -> bytes:
    """Concat Sxx_dB chunks, apply viridis LUT, draw detection boxes, return JPEG bytes."""
    if not chunks:
        return b""
    Sxx_all = np.concatenate(chunks, axis=1)
    full_cols = int(config.MAX_SPEC_CHUNKS * chunks[0].shape[1])
    if Sxx_all.shape[1] < full_cols:
        pad = np.full((Sxx_all.shape[0], full_cols - Sxx_all.shape[1]),
                      np.min(Sxx_all), dtype=Sxx_all.dtype)
        Sxx_dB = np.concatenate([pad, Sxx_all], axis=1)
    else:
        Sxx_dB = Sxx_all

    plow, phigh = np.percentile(Sxx_dB, [2, 99.5])
    if phigh <= plow:
        phigh = plow + 1.0
    idx = np.clip((Sxx_dB - plow) / (phigh - plow) * 255, 0, 255).astype(np.uint8)

    rgb = _VIRIDIS_LUT[idx]
    rgb = rgb[::-1, :, :]
    img = Image.fromarray(rgb, mode="RGB")
    img = img.resize((config.SPEC_IMG_WIDTH, config.SPEC_IMG_HEIGHT), Image.BILINEAR)

    if detections:
        draw = ImageDraw.Draw(img)
        total_time_s = config.SPEC_DURATION_S
        box_h = 20

        for det in detections:
            abs_t = total_time_s - det["offset_from_right"]
            dur = det.get("signal_duration_s", 0.05)
            x0 = int(abs_t / total_time_s * config.SPEC_IMG_WIDTH)
            box_w = max(4, int(dur / total_time_s * config.SPEC_IMG_WIDTH))
            x1 = min(config.SPEC_IMG_WIDTH - 1, x0 + box_w)
            if x1 < 0 or x0 >= config.SPEC_IMG_WIDTH:
                continue

            y_center = int((0.5 - det["freq_hz"] / config.SAMPLE_RATE) * config.SPEC_IMG_HEIGHT)
            y0 = max(0, y_center - box_h // 2)
            y1 = min(config.SPEC_IMG_HEIGHT - 1, y_center + box_h // 2)

            color = (255, 200, 0) if det["phy_ver"] == -1 else (255, 50, 50)
            draw.rectangle([x0, y0, x1, y1], outline=color, width=2)
            label = det.get("chipset", "")
            if label:
                tx = max(0, x0 + 3)
                ty = max(0, y0 - 18)
                bbox = draw.textbbox((tx, ty), label, font=_SPEC_FONT)
                draw.rectangle(
                    [bbox[0] - 2, bbox[1] - 1, bbox[2] + 2, bbox[3] + 1],
                    fill=(10, 10, 20),
                )
                draw.text((tx, ty), label, fill=color, font=_SPEC_FONT)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf.read()


# ===========================================================================
# Time-domain plot rendering
# ===========================================================================

def render_td_plot(iq_segment: np.ndarray, decode_info: dict | None = None) -> bytes:
    """Render a time-domain magnitude plot + spectrogram with annotations."""
    n = len(iq_segment)
    t_ms = np.arange(n) / config.SAMPLE_RATE * 1e3
    mag = np.abs(iq_segment)

    mag_dbfs = 20.0 * np.log10(np.clip(mag, 1e-12, None) / config.ADC_FULL_SCALE)
    DBFS_FLOOR = -80.0

    # Envelope-based symbol edge detection
    win_samples = max(1, int(0.3e-3 * config.SAMPLE_RATE))
    envelope = np.convolve(mag, np.ones(win_samples) / win_samples, mode="same")

    noise_floor = np.percentile(envelope, 10)
    signal_peak = np.percentile(envelope, 95)
    thresh = noise_floor + 0.4 * (signal_peak - noise_floor)
    above = envelope > thresh

    padded = np.concatenate([[False], above, [False]])
    edges = np.diff(padded.astype(np.int8))
    starts = np.where(edges == 1)[0]
    ends = np.where(edges == -1)[0]

    min_sym = int(2e-3 * config.SAMPLE_RATE)
    mask = (ends - starts) >= min_sym
    starts, ends = starts[mask], ends[mask]

    min_gap = int(0.1e-3 * config.SAMPLE_RATE)
    m_starts, m_ends = [], []
    for s, e in zip(starts, ends):
        if m_ends and (s - m_ends[-1]) < min_gap:
            m_ends[-1] = e
        else:
            m_starts.append(s)
            m_ends.append(e)
    starts, ends = np.array(m_starts), np.array(m_ends)

    sym_dur_ms = (ends - starts) / config.SAMPLE_RATE * 1e3
    gap_starts = ends[:-1]
    gap_ends = starts[1:]
    gap_dur_ms = (gap_ends - gap_starts) / config.SAMPLE_RATE * 1e3

    # -- Plotting (2 subplots: time domain + spectrogram) ------------------
    fig = Figure(figsize=(12, 7), dpi=100, facecolor="#0f0f23")
    canvas = FigureCanvasAgg(fig)
    ax_td = fig.add_subplot(2, 1, 1)
    ax_sg = fig.add_subplot(2, 1, 2, sharex=ax_td)

    # --- Top: time-domain magnitude ---
    ax_td.set_facecolor("#1a1a2e")
    ax_td.plot(t_ms, mag_dbfs, color="#7fdbca", linewidth=0.4, alpha=0.7)

    sig_peak_dbfs = np.max(mag_dbfs) if len(mag_dbfs) else -10.0
    y_floor = max(DBFS_FLOOR, sig_peak_dbfs - 60)
    ax_td.set_ylim(y_floor, 0)

    sym_freqs = []
    for s, e in zip(starts, ends):
        sym_iq = iq_segment[s:e]
        spec = np.fft.fft(sym_iq)
        psd = np.abs(spec) ** 2
        freqs = np.fft.fftfreq(len(sym_iq), d=1.0 / config.SAMPLE_RATE)
        dc_zone = int(len(psd) * 0.02)
        if dc_zone > 0:
            psd[:dc_zone] = 0
            psd[-dc_zone:] = 0
        pk = np.argmax(psd)
        sym_freqs.append(freqs[pk])

    if decode_info and decode_info.get("F0_hz") is not None:
        f0 = decode_info["F0_hz"]
    else:
        f0 = sym_freqs[1] if len(sym_freqs) > 1 else (sym_freqs[0] if sym_freqs else 0.0)

    for i, (s, e) in enumerate(zip(starts, ends)):
        t0 = s / config.SAMPLE_RATE * 1e3
        t1 = e / config.SAMPLE_RATE * 1e3
        ax_td.axvspan(t0, t1, alpha=0.08, color="#22d3ee")
        ax_td.axvline(t0, color="#22d3ee", linewidth=0.5, alpha=0.4)
        ax_td.axvline(t1, color="#22d3ee", linewidth=0.5, alpha=0.4)
        df = sym_freqs[i] - f0
        if i == 0:
            lbl = f"F31={sym_freqs[i]:.0f}"
        elif i == 1:
            lbl = f"F0={f0:.0f}"
        else:
            lbl = f"{df:+.0f}"
        y_mid = (0 + y_floor) / 2
        ax_td.text(
            (t0 + t1) / 2, y_mid, lbl,
            ha="center", va="center", fontsize=8, color="#e2e8f0",
            fontfamily="monospace", fontweight="bold", rotation=90,
        )

    for i in range(len(gap_dur_ms)):
        t0 = gap_starts[i] / config.SAMPLE_RATE * 1e3
        t1 = gap_ends[i] / config.SAMPLE_RATE * 1e3
        ax_td.axvspan(t0, t1, alpha=0.12, color="#f87171")

    EXPECTED_PERIOD_MS = 8.8
    lines = []
    if len(sym_dur_ms):
        lines.append(
            f"Symbols   {len(sym_dur_ms):>3d}   "
            f"mean {np.mean(sym_dur_ms):5.2f} ms   "
            f"std {np.std(sym_dur_ms):5.3f} ms"
        )
    if len(gap_dur_ms):
        lines.append(
            f"Gaps      {len(gap_dur_ms):>3d}   "
            f"mean {np.mean(gap_dur_ms):5.2f} ms   "
            f"std {np.std(gap_dur_ms):5.3f} ms"
        )
    if len(starts) >= 2:
        sym_starts_ms = starts / config.SAMPLE_RATE * 1e3
        n_periods = len(sym_starts_ms) - 1
        actual_span = sym_starts_ms[-1] - sym_starts_ms[0]
        expected_span = n_periods * EXPECTED_PERIOD_MS
        drift_ms = actual_span - expected_span
        lines.append(
            f"Drift     {drift_ms:+.3f} ms over {n_periods} periods "
            f"({drift_ms / n_periods * 1e3:+.1f} \u00b5s/period)"
        )
    if lines:
        ax_td.text(
            0.99, 0.97, "\n".join(lines), transform=ax_td.transAxes,
            fontsize=8, color="#7fdbca", fontfamily="monospace",
            va="top", ha="right",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#1a1a2e",
                      edgecolor="#333", alpha=0.92),
        )

    ax_td.set_ylabel("ABS (dBFS)", color="#ccc")
    ax_td.tick_params(colors="#888", labelbottom=False)
    for spine in ax_td.spines.values():
        spine.set_color("#333")
    ax_td.grid(True, color="#333", linewidth=0.3, alpha=0.5)
    ax_td.set_xlim(t_ms[0], t_ms[-1])

    if decode_info:
        di_lines = []
        if decode_info.get("decoded"):
            di_lines.append("DECODED OK")
            if decode_info.get("seq_num") is not None:
                di_lines.append(f"seq={decode_info['seq_num']}")
            if decode_info.get("ntw_id") is not None:
                di_lines.append(f"id=0x{decode_info['ntw_id']:08X}")
        else:
            di_lines.append(f"DECODE FAILED: {decode_info.get('reason', '?')}")

        if decode_info.get("chipset"):
            di_lines.append(f"chipset: {decode_info['chipset']}")
        if decode_info.get("energy_dB") is not None:
            di_lines.append(f"energy: {decode_info['energy_dB']:.1f} dBFS")

        if decode_info.get("F31_snr") is not None:
            di_lines.append(
                f"SNR={decode_info['F31_snr']:.1f}  "
                f"synth_res={decode_info.get('measured_synth_res', '?')} Hz"
            )
        if decode_info.get("F0_hz") is not None:
            di_lines.append(f"F0={decode_info['F0_hz']:.0f} Hz")
        if decode_info.get("header_syms") is not None:
            di_lines.append(f"hdr_syms={decode_info['header_syms']}")
        if decode_info.get("header_n_corr") is not None:
            di_lines.append(
                f"hdr_corr={decode_info['header_n_corr']}  "
                f"ch={decode_info.get('channel_num', '?')}  "
                f"hop={decode_info.get('hop_seq_idx', '?')}  "
                f"pdu_len={decode_info.get('num_pdu_symbols', '?')}"
            )
        reason = decode_info.get("reason", "")
        if reason == "pdu_fail" and decode_info.get("pdu_syms_head") is not None:
            di_lines.append(f"pdu[0:10]={decode_info['pdu_syms_head']}")

        di_color = "#22d3ee" if decode_info.get("decoded") else "#f87171"
        ax_td.text(
            0.01, 0.97, "\n".join(di_lines), transform=ax_td.transAxes,
            fontsize=8, color=di_color, fontfamily="monospace",
            va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#1a1a2e",
                      edgecolor=di_color, alpha=0.92),
        )

    # --- Bottom: spectrogram ---
    ax_sg.set_facecolor("#1a1a2e")
    nperseg_td = min(256, n // 4) if n > 256 else max(16, n // 2)
    noverlap_td = nperseg_td * 3 // 4
    f_sg, t_sg, Sxx = scipy_spectrogram(
        iq_segment, fs=config.SAMPLE_RATE,
        nperseg=nperseg_td, noverlap=noverlap_td, return_onesided=False,
    )
    f_sg = np.fft.fftshift(f_sg)
    Sxx = np.fft.fftshift(Sxx, axes=0)
    Sxx_dB = 10.0 * np.log10(Sxx + 1e-12)
    t_sg_ms = t_sg * 1e3
    f_sg_khz = f_sg / 1e3

    plow, phigh = np.percentile(Sxx_dB, [2, 99.5])
    if phigh <= plow:
        phigh = plow + 1.0

    ax_sg.pcolormesh(
        t_sg_ms, f_sg_khz, Sxx_dB,
        vmin=plow, vmax=phigh, cmap="viridis", shading="auto",
    )
    ax_sg.set_xlabel("Time (ms)", color="#ccc")
    ax_sg.set_ylabel("Freq (kHz)", color="#ccc")
    ax_sg.tick_params(colors="#888")
    for spine in ax_sg.spines.values():
        spine.set_color("#333")
    ax_sg.set_xlim(t_ms[0], t_ms[-1])

    fig.tight_layout(pad=0.5)

    buf = io.BytesIO()
    canvas.print_png(buf)
    buf.seek(0)
    return buf.read()
