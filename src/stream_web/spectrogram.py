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
    Sxx_dB = np.concatenate(chunks, axis=1)

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
        total_time_s = len(chunks) * config.SPEC_CHUNK_S
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

def render_td_plot(iq_segment: np.ndarray) -> bytes:
    """Render a time-domain magnitude plot with symbol/gap duration annotations."""
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

    # Merge ON intervals separated by gaps shorter than 0.1 ms
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

    # -- Plotting ----------------------------------------------------------
    fig = Figure(figsize=(12, 4.5), dpi=100, facecolor="#0f0f23")
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_facecolor("#1a1a2e")
    ax.plot(t_ms, mag_dbfs, color="#7fdbca", linewidth=0.4, alpha=0.7)

    sig_peak_dbfs = np.max(mag_dbfs) if len(mag_dbfs) else -10.0
    y_floor = max(DBFS_FLOOR, sig_peak_dbfs - 60)
    ax.set_ylim(y_floor, 0)

    # Peak frequency per symbol via FFT
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
    f0 = sym_freqs[0] if sym_freqs else 0.0

    # Symbol shading + delta-f labels
    for i, (s, e) in enumerate(zip(starts, ends)):
        t0 = s / config.SAMPLE_RATE * 1e3
        t1 = e / config.SAMPLE_RATE * 1e3
        ax.axvspan(t0, t1, alpha=0.08, color="#22d3ee")
        ax.axvline(t0, color="#22d3ee", linewidth=0.5, alpha=0.4)
        ax.axvline(t1, color="#22d3ee", linewidth=0.5, alpha=0.4)
        df = sym_freqs[i] - f0
        lbl = f"F0={sym_freqs[i]:.0f}" if i == 0 else f"{df:+.0f}"
        y_mid = (0 + y_floor) / 2
        ax.text(
            (t0 + t1) / 2, y_mid, lbl,
            ha="center", va="center", fontsize=8, color="#e2e8f0",
            fontfamily="monospace", fontweight="bold", rotation=90,
        )

    # Gap shading
    for i in range(len(gap_dur_ms)):
        t0 = gap_starts[i] / config.SAMPLE_RATE * 1e3
        t1 = gap_ends[i] / config.SAMPLE_RATE * 1e3
        ax.axvspan(t0, t1, alpha=0.12, color="#f87171")

    # Summary statistics box
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
        ax.text(
            0.99, 0.97, "\n".join(lines), transform=ax.transAxes,
            fontsize=8, color="#7fdbca", fontfamily="monospace",
            va="top", ha="right",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#1a1a2e",
                      edgecolor="#333", alpha=0.92),
        )

    ax.set_xlabel("Time (ms)", color="#ccc")
    ax.set_ylabel("ABS (dBFS)", color="#ccc")
    ax.tick_params(colors="#888")
    for spine in ax.spines.values():
        spine.set_color("#333")
    ax.grid(True, color="#333", linewidth=0.3, alpha=0.5)
    ax.set_xlim(t_ms[0], t_ms[-1])
    fig.tight_layout(pad=0.5)

    buf = io.BytesIO()
    canvas.print_png(buf)
    buf.seek(0)
    return buf.read()
