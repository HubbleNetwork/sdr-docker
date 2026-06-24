"""Symbol timing analysis — edge detection and rise/fall time measurement."""

import numpy as np
from scipy.signal import filtfilt


def build_crossings(iq_seg: np.ndarray, smoothing_window: int = 8):
    """Find the rising and falling edges of symbols in the given IQ segment by smoothing
    the magnitude and applying a percentile-based threshold.
    """
    b = np.ones(smoothing_window) / smoothing_window
    smoothed = filtfilt(b, [1], np.abs(iq_seg))
    thresh = float(np.percentile(smoothed, 90)) * 0.5
    above = (smoothed >= thresh).astype(np.int8)
    d = np.diff(above)
    rising = np.where(d > 0)[0]
    falling = np.where(d < 0)[0]
    return rising, falling


def correct_symbol_edges(
    iq_seg: np.ndarray,
    start_sample: int,
    view_start: int,
    n_sym: int,
    sym_offset: int,
    slot: int,
    sym_len: int,
) -> list[tuple[int, int]]:
    """Return corrected (start, end) sample pairs for each symbol, relative to iq_seg.

    Uses global crossings from build_crossings and chaining (prev_end + gap_samples)
    to track real inter-symbol timing rather than relying on purely nominal positions.
    """
    n = len(iq_seg)
    gap_samples = slot - sym_len
    search_margin = max(gap_samples, sym_len // 8)
    fall_margin = gap_samples // 4

    # Find every point where the signal rises above or falls below threshold (50% of max dBFS)
    rising_xings, falling_xings = build_crossings(iq_seg)

    def _nearest_xing(arr, target):
        if not len(arr):
            return int(target)
        return int(arr[np.argmin(np.abs(arr - target))])

    edges = []
    prev_end = None
    for k in range(n_sym):
        # Find nominal symbol start time based on protocol
        abs_start = start_sample + (sym_offset + k) * slot
        nom_s = abs_start - view_start
        nom_e = nom_s + sym_len

        if nom_s < 0 or nom_e > n:
            continue

        # Create a search window to find the rise, based on the previous symbol's
        # end + gap or the nominal position if no previous symbol
        search_center = (prev_end + gap_samples) if prev_end is not None else nom_s
        rs_lo = max(search_center - search_margin, (prev_end + 1) if prev_end is not None else 0)
        rs_hi = search_center + search_margin
        cands = rising_xings[(rising_xings >= rs_lo) & (rising_xings <= rs_hi)]
        if not len(cands):
            rs_lo = max(nom_s - search_margin, (prev_end + 1) if prev_end is not None else 0)
            rs_hi = nom_s + search_margin
            cands = rising_xings[(rising_xings >= rs_lo) & (rising_xings <= rs_hi)]
        s = _nearest_xing(cands, search_center)
        if prev_end is not None:
            s = max(s, prev_end + 1)

        # Once we correct for start, we start searching for the end based on that
        nom_e_corr = s + sym_len
        fe_lo = max(0, nom_e_corr - fall_margin)
        fe_hi = min(n, nom_e_corr + fall_margin)
        cands = falling_xings[(falling_xings >= fe_lo) & (falling_xings <= fe_hi)]
        e = _nearest_xing(cands, nom_e_corr)
        e = max(e, s + 1)

        s = max(0, min(s, n - 1))
        e = max(s + 1, min(e, n))
        prev_end = e

        edges.append((s, e))

    return edges


def measure_transition_us(
    iq_seg: np.ndarray,
    begin: int,
    end: int,
    sr: int,
    rise: bool,
    smoothing_window: int = 32,
) -> float | None:
    """10%-90% rise or fall time in microseconds. Returns None if not measurable."""
    begin = max(0, begin)
    end = min(len(iq_seg), end)
    if end - begin < smoothing_window * 2:
        return None

    magnitude = np.abs(iq_seg[begin:end])
    b = np.ones(smoothing_window) / smoothing_window
    smoothed = filtfilt(b, [1], magnitude)
    lo = float(smoothed.min())
    hi = float(smoothed.max())

    if hi - lo < 1e-9:
        return None

    thresh_10 = lo + 0.10 * (hi - lo)
    thresh_90 = lo + 0.90 * (hi - lo)

    if rise:
        hits_10 = np.where(smoothed > thresh_10)[0]
        if not len(hits_10):
            return None
        i10 = int(hits_10[0])
        hits_90 = np.where(smoothed[i10:] > thresh_90)[0]
        if not len(hits_90):
            return None
        i90 = i10 + int(hits_90[0])
    else:
        hits_90 = np.where(smoothed < thresh_90)[0]
        if not len(hits_90):
            return None
        i90 = int(hits_90[0])
        hits_10 = np.where(smoothed[i90:] < thresh_10)[0]
        if not len(hits_10):
            return None
        i10 = i90 + int(hits_10[0])
    return abs(i10 - i90) / sr * 1e6


def edges_to_timing_stats(edges: list[tuple[int, int]], sr: int) -> dict:
    """Convert corrected (start, end) sample pairs into timing stats.

    Returns dict with sym_count, sym_mean_ms, sym_std_ms,
    gap_count, gap_mean_ms, gap_std_ms.
    """
    if not edges:
        return {
            "sym_count": 0, "sym_mean_ms": None, "sym_std_ms": None,
            "gap_count": 0, "gap_mean_ms": None, "gap_std_ms": None,
        }
    s_arr = np.array([s for s, _ in edges])
    e_arr = np.array([e for _, e in edges])
    sym_ms = (e_arr - s_arr) / sr * 1e3
    gap_ms = (s_arr[1:] - e_arr[:-1]) / sr * 1e3 if len(s_arr) > 1 else np.array([])
    return {
        "sym_count": int(len(sym_ms)),
        "sym_mean_ms": float(round(np.mean(sym_ms), 4)),
        "sym_std_ms": float(round(np.std(sym_ms), 4)),
        "gap_count": int(len(gap_ms)),
        "gap_mean_ms": float(round(np.mean(gap_ms), 4)) if len(gap_ms) else None,
        "gap_std_ms": float(round(np.std(gap_ms), 4)) if len(gap_ms) else None,
    }
