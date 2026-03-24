"""Dual-protocol preamble detection and packet decoder (PHY v-1 and v1).

Pipeline:
  1. Dual-template preamble detection via OpenCV matchTemplate + NMS
  2. Protocol-specific demodulation (OOK for v-1, FSK with hopping for v1)
  3. Reed-Solomon decode and MAC parsing
"""

from collections import Counter

import cv2
import numpy as np
import reedsolo as rs
from scipy.signal import spectrogram as scipy_spectrogram

from . import config

# -- Per-chipset decode statistics ------------------------------------------
_chipset_stats: dict[str, dict[str, int]] = {}


def cs_inc(chipset: str, field: str):
    """Increment a per-chipset decode counter."""
    if chipset not in _chipset_stats:
        _chipset_stats[chipset] = {
            "detected": 0, "snr_fail": 0, "header_fail": 0,
            "pdu_fail": 0, "ok": 0,
        }
    _chipset_stats[chipset][field] += 1
    _last_attempt["chipset"] = chipset
    if field != "detected":
        _last_attempt["reason"] = field


def get_chipset_stats() -> dict:
    return dict(_chipset_stats)


def reset_chipset_stats():
    _chipset_stats.clear()


# -- Diagnostic counter for v1 verbose output -------------------------------
_v1_diag_counter = 0

_last_attempt: dict = {}


# ===========================================================================
# Preamble detection
# ===========================================================================

def detect_preambles(spec_img, t_det, f_det):
    """Dual-template preamble detection via OpenCV + NMS.

    Returns (det_time_s, det_freq_hz, det_scores, det_phy_ver).
    """
    all_raw_y, all_raw_x, all_raw_scores, all_raw_ver = [], [], [], []

    for phy_ver, tmpl in sorted(config.templates.items()):
        match_result = cv2.matchTemplate(
            spec_img, tmpl["uint8"], cv2.TM_CCOEFF_NORMED
        )
        ry, rx = np.where(match_result > config.DETECTION_THRESHOLD)
        if len(ry) > 0:
            r_scores = match_result[ry, rx]
            all_raw_y.append(ry)
            all_raw_x.append(rx)
            all_raw_scores.append(r_scores)
            all_raw_ver.append(np.full(len(ry), phy_ver, dtype=int))
        del match_result

    if not all_raw_y:
        return np.array([]), np.array([]), np.array([]), np.array([], dtype=int)

    raw_y = np.concatenate(all_raw_y)
    raw_x = np.concatenate(all_raw_x)
    raw_scores = np.concatenate(all_raw_scores)
    raw_phy_ver = np.concatenate(all_raw_ver)

    if len(raw_scores) > config.MAX_RAW:
        top_k = np.argpartition(-raw_scores, config.MAX_RAW)[:config.MAX_RAW]
        raw_y, raw_x, raw_scores = raw_y[top_k], raw_x[top_k], raw_scores[top_k]
        raw_phy_ver = raw_phy_ver[top_k]

    order = np.argsort(-raw_scores)
    raw_y, raw_x, raw_scores = raw_y[order], raw_x[order], raw_scores[order]
    raw_phy_ver = raw_phy_ver[order]

    keep: list[int] = []
    for i in range(len(raw_scores)):
        suppress = False
        for j in keep:
            if (abs(int(raw_x[i]) - int(raw_x[j])) < config.NMS_TIME_BINS and
                    abs(int(raw_y[i]) - int(raw_y[j])) < config.NMS_FREQ_BINS):
                suppress = True
                break
        if not suppress:
            keep.append(i)

    half_h = config.TEMPLATE_FREQ_BINS // 2
    det_x = raw_x[keep]
    det_y = raw_y[keep]
    det_scores = raw_scores[keep]
    det_phy_ver = raw_phy_ver[keep]
    det_time_s = t_det[np.clip(det_x, 0, len(t_det) - 1)]
    det_freq_hz = f_det[np.clip(det_y + half_h, 0, len(f_det) - 1)]
    return det_time_s, det_freq_hz, det_scores, det_phy_ver


# ===========================================================================
# Shared helpers
# ===========================================================================

def _build_chan_mask(F0, synth_res_val):
    """Boolean mask for 64-FSK bandwidth around F0."""
    a = F0 - 1 * synth_res_val
    b = F0 + (config.NUM_FSK_BINS + 1) * synth_res_val
    return (config.fft_freqs >= min(a, b)) & (config.fft_freqs <= max(a, b))


def _lfsr7_symbols(seed: int, nsym: int) -> list[int]:
    """Generate LFSR-7 whitening symbols (6-bit) for de-scrambling."""
    state = (3 << 5) | (0b1000000 | seed)
    bits = []
    for _ in range(nsym * 6):
        bits.append((state & 0x40) >> 6)
        fb = ((state >> 6) ^ (state >> 3)) & 1
        state = ((state << 1) & 0x7F) | fb
    return [int("".join(map(str, bits[i * 6:(i + 1) * 6])), 2) for i in range(nsym)]


def _data_de_scrambling(pdu_symbols: np.ndarray, channel_num: int) -> np.ndarray:
    """XOR PDU symbols with the LFSR-7 whitening sequence for this channel."""
    whitening = np.array(_lfsr7_symbols(channel_num, len(pdu_symbols)))
    return pdu_symbols ^ whitening


def _rs_decode(symbols, rs_n_array: list[int], rs_k_array: list[int]):
    """Reed-Solomon decode over GF(64). Returns (decoded, n_corrections)."""
    sym_list = symbols.tolist() if isinstance(symbols, np.ndarray) else list(symbols)
    try:
        idx = rs_n_array.index(len(sym_list))
    except ValueError:
        return np.full(0, -1, dtype=int), -1
    rs_k = rs_k_array[idx]
    num_ecc = rs_n_array[idx] - rs_k
    try:
        rx_syms, _, errata_pos = rs.rs_correct_msg(sym_list, num_ecc, fcr=1)
        return np.array(rx_syms), len(errata_pos)
    except Exception:
        return np.full(rs_k, -1, dtype=int), -1


def _demod_one_symbol(sig_segment, F0, synth_res_val, chan_mask):
    """Return (fsk_bin, peak_freq, peak_power) for one symbol slot."""
    spectrum = np.fft.fft(sig_segment)
    psd = np.abs(spectrum) ** 2
    psd_masked = psd.copy()
    psd_masked[~chan_mask] = 0.0
    peak_bin = np.argmax(psd_masked)
    peak_freq = _interp_peak(psd_masked, peak_bin, config.fft_freqs)
    fsk_bin = int(round((peak_freq - F0) / synth_res_val))
    fsk_bin = max(0, min(config.NUM_FSK_BINS - 1, fsk_bin))
    return fsk_bin, peak_freq, psd_masked[peak_bin]


_TIMING_SEARCH = int(0.1e-3 * config.SAMPLE_RATE)  # ±78 samples
_TIMING_STEP = max(1, (2 * _TIMING_SEARCH) // 9)   # 10 positions
_TIMING_STEPS = np.arange(-_TIMING_SEARCH, _TIMING_SEARCH + 1, _TIMING_STEP)


def _demod_best(signal, s0, F0, synth_res_val, chan_mask):
    """Demod with ±0.1 ms timing search. Returns (fsk_bin, peak_freq, best_offset)."""
    sym_len = config.samples_per_symbol
    best_bin, best_freq, best_power, best_off = 0, 0.0, -1.0, 0
    for off in _TIMING_STEPS:
        s = s0 + off
        if s < 0 or s + sym_len > len(signal):
            continue
        fsk_bin, peak_freq, peak_power = _demod_one_symbol(
            signal[s: s + sym_len], F0, synth_res_val, chan_mask,
        )
        if peak_power > best_power:
            best_bin, best_freq, best_power, best_off = fsk_bin, peak_freq, peak_power, int(off)
    return best_bin, best_freq, best_off


def _interp_peak(psd, bin_idx, freqs):
    """Parabolic interpolation around an FFT peak for sub-bin accuracy."""
    n = len(psd)
    b = bin_idx
    if b <= 0 or b >= n - 1:
        return freqs[b]
    alpha = psd[b - 1]
    beta = psd[b]
    gamma = psd[b + 1]
    denom = alpha - 2 * beta + gamma
    if abs(denom) < 1e-30:
        return freqs[b]
    delta = 0.5 * (alpha - gamma) / denom
    bin_spacing = freqs[1] - freqs[0] if n > 1 else 1.0
    return freqs[b] + delta * bin_spacing


def _identify_chipset(measured_synth_res):
    """Return (chipset_name, synth_res_value) for closest matching chipset."""
    best_name, best_val, best_err = None, None, float("inf")
    for name, val in config.SYNTH_RES.items():
        err = abs(measured_synth_res - val)
        if err < best_err:
            best_name, best_val, best_err = name, val, err
    return best_name, best_val


# ===========================================================================
# PHY v-1 decode
# ===========================================================================

def _decode_vneg1(signal, start_sample, sps):
    """Decode a PHY v-1 packet: OOK preamble, no header, no hopping.

    Returns (pkt_info, result) or (None, None) on failure.
    """
    end_sample = start_sample + config.SYMBOLS_PER_PACKET_VNEG1 * sps["slot"]
    if end_sample > len(signal):
        return None, None

    # F0 estimation via ON-OFF spectral differencing
    psd_on = np.zeros(config.samples_per_symbol)
    for sym in config.preamble_on_idx:
        s0 = start_sample + sym * sps["slot"]
        psd_on += np.abs(np.fft.fft(signal[s0: s0 + config.samples_per_symbol])) ** 2
    psd_on /= len(config.preamble_on_idx)

    psd_off = np.zeros(config.samples_per_symbol)
    for sym in config.preamble_off_idx:
        s0 = start_sample + sym * sps["slot"]
        psd_off += np.abs(np.fft.fft(signal[s0: s0 + config.samples_per_symbol])) ** 2
    psd_off /= len(config.preamble_off_idx)

    psd_diff = psd_on - psd_off
    F0_bin = np.argmax(psd_diff)
    F0 = config.fft_freqs[F0_bin]
    F0_snr = psd_diff[F0_bin] / (np.median(np.abs(psd_diff)) + 1e-30)

    if F0_snr < config.PREAMBLE_F0_SNR_MIN:
        _last_attempt["reason"] = "snr_fail"
        return None, None

    total_energy_dBFS = 10.0 * np.log10(
        psd_on[F0_bin] / (config.samples_per_symbol * config.ADC_FULL_SCALE) ** 2 + 1e-30
    )
    _last_attempt.update(F0_hz=F0, energy_dB=total_energy_dBFS)

    # Channel mask and per-symbol peak frequency
    chan_mask = _build_chan_mask(F0, config.FREQ_STEP_VNEG1)
    sym_peak_freqs = np.zeros(config.SYMBOLS_PER_PACKET_VNEG1)
    for sym in range(config.SYMBOLS_PER_PACKET_VNEG1):
        s0 = start_sample + sym * sps["slot"]
        spectrum = np.fft.fft(signal[s0: s0 + config.samples_per_symbol])
        psd = np.abs(spectrum) ** 2
        psd_m = psd.copy()
        psd_m[~chan_mask] = 0.0
        pk = np.argmax(psd_m)
        sym_peak_freqs[sym] = config.fft_freqs[pk]

    # Decode data symbols (slots 8..31)
    data_bins = []
    for sym_idx in range(config.PREAMBLE_LEN, config.SYMBOLS_PER_PACKET_VNEG1):
        freq_offset = sym_peak_freqs[sym_idx] - F0
        fsk_bin = int(round(freq_offset / config.FREQ_STEP_VNEG1))
        fsk_bin = max(0, min(config.NUM_FSK_BINS - 1, fsk_bin))
        data_bins.append(fsk_bin)

    # RS decode v-1: length symbols at positions 0, 9, 18
    len_sym_0 = data_bins[0]
    len_sym_9 = data_bins[9]
    len_sym_18 = data_bins[18]
    len_idx = Counter([len_sym_0, len_sym_9, len_sym_18]).most_common(1)[0][0]

    if len_idx < 0 or len_idx >= len(config.RS_K_VNEG1):
        return None, None

    rs_k = config.RS_K_VNEG1[len_idx]
    rs_n = config.RS_N_VNEG1[len_idx]
    num_ecc = rs_n - rs_k

    codeword = data_bins[1:9] + data_bins[10:18] + data_bins[19:]
    if len(codeword) < rs_n:
        return None, None
    codeword_rs = codeword[:rs_n]

    syndromes = rs.rs_calc_syndromes(codeword_rs, num_ecc, fcr=1)
    has_errors = not all(s == 0 for s in syndromes[1:])

    if has_errors:
        try:
            corrected = rs.rs_correct_msg(codeword_rs, num_ecc, fcr=1)
            mac_syms = list(corrected[0])
            n_corr = len(corrected[2])
        except rs.ReedSolomonError:
            return None, None
    else:
        mac_syms = list(codeword_rs[:rs_k])
        n_corr = 0

    # Parse v-1 MAC
    bits = "".join(f"{s:06b}" for s in mac_syms)
    if len(bits) < 44:
        return None, None

    ntw_id = int(bits[0:34], 2)
    seq_num = int(bits[34:44], 2)
    auth_tag = int(bits[44:60], 2) if len(bits) >= 60 else 0

    return (
        {"F0_hz": F0, "total_energy_dB": total_energy_dBFS},
        {
            "phy_ver": -1, "ntw_id": ntw_id, "seq_num": seq_num,
            "auth_tag": auth_tag, "rs_errors": n_corr,
        },
    )


# ===========================================================================
# PHY v1 decode
# ===========================================================================

def _decode_v1(signal, start_sample, sps):
    """Decode a PHY v1 packet: FSK preamble, header, frequency hopping.

    Returns (pkt_info, result) or (None, None) on failure.
    """
    global _v1_diag_counter
    _v1_diag_counter += 1
    _diag = config.VERBOSE or (_v1_diag_counter % 50 == 1)

    # F63 / F0 estimation
    psd_63 = np.zeros(config.samples_per_symbol)
    for sym in config.on_indices_v1:
        s0 = start_sample + sym * sps["slot"]
        if s0 + config.samples_per_symbol > len(signal):
            return None, None
        psd_63 += np.abs(np.fft.fft(signal[s0: s0 + config.samples_per_symbol])) ** 2
    psd_63 /= len(config.on_indices_v1)

    psd_0 = np.zeros(config.samples_per_symbol)
    for sym in config.off_indices_v1:
        s0 = start_sample + sym * sps["slot"]
        if s0 + config.samples_per_symbol > len(signal):
            return None, None
        psd_0 += np.abs(np.fft.fft(signal[s0: s0 + config.samples_per_symbol])) ** 2
    psd_0 /= len(config.off_indices_v1)

    psd_diff_63 = psd_63 - psd_0
    F63_bin = np.argmax(psd_diff_63)
    F63_snr = psd_diff_63[F63_bin] / (np.median(np.abs(psd_diff_63)) + 1e-30)

    psd_diff_0 = psd_0 - psd_63
    F0_bin = np.argmax(psd_diff_0)

    if F63_snr < config.PREAMBLE_F0_SNR_MIN:
        _last_attempt["reason"] = "snr_fail"
        if _diag:
            print(f"[v1-DIAG] FAIL snr: F63_snr={F63_snr:.1f} < {config.PREAMBLE_F0_SNR_MIN}")
        return None, None

    F0 = _interp_peak(psd_diff_0, F0_bin, config.fft_freqs)
    F63 = _interp_peak(psd_diff_63, F63_bin, config.fft_freqs)

    synth_res_signed = (F63 - F0) / 63.0
    measured_synth_res = abs(synth_res_signed)
    chipset_name, table_synth_res = _identify_chipset(measured_synth_res)
    synth_res_val = table_synth_res if synth_res_signed >= 0 else -table_synth_res
    device_channel_spacing = config.DEVICE_CHANNEL_SPACING[chipset_name]
    cs_inc(chipset_name, "detected")

    if _diag:
        sign_char = "+" if synth_res_signed >= 0 else "-"
        print(f"[v1-DIAG] F0={F0:.1f} F63={F63:.1f} Hz, "
              f"meas_sr={sign_char}{measured_synth_res:.2f} -> {chipset_name}(val={synth_res_val:.1f}), "
              f"snr={F63_snr:.1f}")

    total_energy_dBFS = 10.0 * np.log10(
        max(psd_0[F0_bin], psd_63[F63_bin]) / (config.samples_per_symbol * config.ADC_FULL_SCALE) ** 2 + 1e-30
    )
    _last_attempt.update(
        F0_hz=F0, energy_dB=total_energy_dBFS,
        measured_synth_res=round(measured_synth_res, 2),
        synth_res_val=round(synth_res_val, 1),
        F63_snr=round(F63_snr, 1),
    )

    # Demodulate header (6 symbols, same channel) with timing tolerance
    chan_mask = _build_chan_mask(F0, synth_res_val)
    header_syms = []
    drift = 0
    for h in range(config.NUM_HEADER_SYMS):
        sym_abs_idx = config.PREAMBLE_LEN + h
        s0 = start_sample + sym_abs_idx * sps["slot"] + drift
        if s0 + config.samples_per_symbol > len(signal):
            return None, None
        fsk_bin, _, off = _demod_best(signal, s0, F0, synth_res_val, chan_mask)
        drift += off
        header_syms.append(fsk_bin)

    _last_attempt["header_syms"] = list(header_syms)

    # RS decode header
    header_decoded, header_n_corr = _rs_decode(
        np.array(header_syms, dtype=int), config.RS_N_V1, config.RS_K_V1
    )
    if header_n_corr < 0:
        cs_inc(chipset_name, "header_fail")
        if _diag:
            print(f"[v1-DIAG] FAIL header RS: syms={header_syms}, chipset={chipset_name}")
        return None, None

    # Parse header
    hdr_bits = f"{int(header_decoded[0]):06b}{int(header_decoded[1]):06b}"
    phy_ver = int(hdr_bits[0:4], 2)
    pkt_len_idx = int(hdr_bits[4:6], 2)
    hop_seq_idx = int(hdr_bits[6:8], 2)
    channel_num = int(hdr_bits[8:12], 2)

    # 4 bits encode 0-15; channels 16-18 alias to 0-2.
    # Disambiguate by comparing measured center to both nominal frequencies.
    candidate_b = channel_num + 16
    if candidate_b < config.NUM_CHANNELS:
        measured_center = F0 + 31.5 * synth_res_val
        nominal_a = (channel_num - config.LO_CHANNEL) * config.CHANNEL_SPACING
        nominal_b = (candidate_b - config.LO_CHANNEL) * config.CHANNEL_SPACING
        if abs(measured_center - nominal_b) < abs(measured_center - nominal_a):
            channel_num = candidate_b

    num_pdu_symbols = config.RS_N_V1[pkt_len_idx + 1]
    hopping_seq = config.HOPPING_SEQS[hop_seq_idx]
    _last_attempt.update(
        header_n_corr=int(header_n_corr),
        channel_num=channel_num, hop_seq_idx=hop_seq_idx,
        pkt_len_idx=pkt_len_idx, num_pdu_symbols=num_pdu_symbols,
    )

    try:
        hop_index = hopping_seq.index(channel_num)
    except ValueError:
        _last_attempt["reason"] = "hop_fail"
        return None, None

    # Demodulate PDU with frequency hopping and timing tolerance
    current_channel = channel_num
    F0_current = F0
    pdu_syms = []

    for p_idx in range(num_pdu_symbols):
        sym_abs_idx = config.PREAMBLE_LEN + config.NUM_HEADER_SYMS + p_idx
        s0 = start_sample + sym_abs_idx * sps["slot"] + drift
        if s0 + config.samples_per_symbol > len(signal):
            break

        next_channel = hopping_seq[
            (hop_index + sym_abs_idx // config.NUM_SYM_PER_HOP) % config.NUM_CHANNELS
        ]
        if next_channel != current_channel:
            F0_current += (next_channel - current_channel) * device_channel_spacing
            chan_mask = _build_chan_mask(F0_current, synth_res_val)
            current_channel = next_channel

        fsk_bin, _, off = _demod_best(signal, s0, F0_current, synth_res_val, chan_mask)
        drift += off
        pdu_syms.append(fsk_bin)

    if len(pdu_syms) != num_pdu_symbols:
        _last_attempt["reason"] = "pdu_incomplete"
        return None, None

    # De-scramble + RS decode PDU
    pdu_raw = np.array(pdu_syms, dtype=int)
    de_scrambled = _data_de_scrambling(pdu_raw, channel_num)
    pdu_decoded, pdu_n_corr = _rs_decode(de_scrambled, config.RS_N_V1, config.RS_K_V1)
    if pdu_n_corr < 0:
        _last_attempt["pdu_syms_head"] = pdu_syms[:10]
        cs_inc(chipset_name, "pdu_fail")
        if _diag:
            print(f"[v1-DIAG] FAIL pdu RS: chipset={chipset_name}, ch={channel_num}, "
                  f"hop={hop_seq_idx}, hdr_corr={header_n_corr}, "
                  f"pdu_raw={pdu_syms[:8]}..., sr_val={synth_res_val:.1f}")
        return None, None

    # Parse v1 MAC
    mac_syms = pdu_decoded.tolist()
    bits = "".join(f"{int(s):06b}" for s in mac_syms)

    payload_proto_ver = int(bits[0:2], 2)
    seq_num = int(bits[2:12], 2)
    ntw_id = int(bits[12:44], 2)
    auth_tag = int(bits[44:76], 2)
    remaining_bits = bits[76:]

    payload_bytes = config.PAYLOAD_LEN_BYTES_V1[pkt_len_idx]
    payload_bits_len = payload_bytes * 8
    if payload_bits_len > 0 and len(remaining_bits) >= payload_bits_len:
        payload_val = int(remaining_bits[:payload_bits_len], 2)
    elif payload_bits_len == 0:
        payload_val = 0
    else:
        payload_val = int(remaining_bits, 2) if remaining_bits else 0

    nominal_center_hz = (channel_num - config.LO_CHANNEL) * config.CHANNEL_SPACING
    measured_center_hz = F0 + 31.5 * table_synth_res
    freq_delta_hz = measured_center_hz - nominal_center_hz

    cs_inc(chipset_name, "ok")
    if _diag:
        print(f"[v1-DIAG] OK: chipset={chipset_name}, meas_sr={measured_synth_res:.2f}, "
              f"ntw=0x{ntw_id:08X}, seq={seq_num}, ch={channel_num}, "
              f"hdr_corr={header_n_corr}, pdu_corr={pdu_n_corr}, "
              f"freq_delta={freq_delta_hz:.0f}")

    return (
        {"F0_hz": F0, "total_energy_dB": total_energy_dBFS},
        {
            "phy_ver": 1, "ntw_id": ntw_id, "seq_num": seq_num,
            "auth_tag": auth_tag, "payload_proto_ver": payload_proto_ver,
            "payload_val": payload_val, "chipset": chipset_name,
            "channel_num": channel_num, "hop_seq_idx": hop_seq_idx,
            "header_n_corr": header_n_corr, "pdu_n_corr": pdu_n_corr,
            "measured_synth_res": round(measured_synth_res, 2),
            "num_pdu_symbols": num_pdu_symbols,
            "freq_delta_hz": round(freq_delta_hz, 1),
        },
    )


# ===========================================================================
# Full decode pipeline
# ===========================================================================

def decode_signal(signal):
    """Full dual-protocol decode pipeline on a 1-second IQ chunk.

    Returns (decoded_packets, detection_list, all_attempts).
    - decoded_packets: successfully decoded packets with MAC fields.
    - detection_list:  all preamble detections (for box overlay on spectrogram).
    - all_attempts:    per-detection decode outcomes (chipset, decoded, reason).
    """
    sig = signal.copy()
    sig -= sig.mean()

    # Detection spectrogram
    f_det, t_det, Sxx_det = scipy_spectrogram(
        sig, fs=config.SAMPLE_RATE, nperseg=config.NFFT_DET,
        noverlap=config.NOVERLAP_DET, return_onesided=False,
    )
    f_det = np.fft.fftshift(f_det)
    Sxx_det = np.fft.fftshift(Sxx_det, axes=0)
    dc_idx = len(f_det) // 2
    Sxx_det[dc_idx, :] = 0.0

    Sxx_dB = (10.0 * np.log10(Sxx_det + 1e-12)).astype(np.float32)
    plow, phigh = np.percentile(Sxx_dB, [2, 99.5])
    if phigh <= plow:
        return [], [], []
    spec_img = np.clip((Sxx_dB - plow) / (phigh - plow) * 255, 0, 255).astype(np.uint8)

    # Dual-template detection
    det_time_s, det_freq_hz, det_scores, det_phy_ver = detect_preambles(
        spec_img, t_det, f_det
    )
    if len(det_time_s) == 0:
        return [], [], []

    # Build detection info list (for box overlay)
    detection_list = []
    for i in range(len(det_time_s)):
        ver = int(det_phy_ver[i])
        detection_list.append({
            "time_s": float(det_time_s[i]),
            "freq_hz": float(det_freq_hz[i]),
            "phy_ver": ver,
            "score": float(det_scores[i]),
            "preamble_duration_s": config.templates[ver]["duration_s"],
        })

    # Decode each detection (dispatch by protocol version)
    decoded_packets = []
    all_attempts = []
    for det in detection_list:
        start_sample = int(round(det["time_s"] * config.SAMPLE_RATE))
        ver = det["phy_ver"]
        sps = config.slot_samples[ver]

        _last_attempt.clear()
        if ver == -1:
            pkt_info, result = _decode_vneg1(sig, start_sample, sps)
        else:
            pkt_info, result = _decode_v1(sig, start_sample, sps)

        attempt = {
            "time_s": det["time_s"],
            "freq_hz": det["freq_hz"],
            "phy_ver": ver,
            "score": det["score"],
            "start_sample": start_sample,
            "decoded": False,
            "reason": "unknown",
        }
        attempt.update(_last_attempt)

        if pkt_info is not None and pkt_info["total_energy_dB"] >= config.MIN_ENERGY_DBFS:
            result["time_s"] = det["time_s"]
            result["freq_hz"] = det["freq_hz"]
            result["F0_hz"] = pkt_info["F0_hz"]
            result["total_energy_dB"] = pkt_info["total_energy_dB"]
            result["score"] = det["score"]
            result["preamble_duration_s"] = det["preamble_duration_s"]
            ver = result["phy_ver"]
            if ver == -1:
                total_syms = config.SYMBOLS_PER_PACKET_VNEG1
            else:
                total_syms = config.PREAMBLE_LEN + config.NUM_HEADER_SYMS + result.get("num_pdu_symbols", 0)
            result["signal_duration_s"] = total_syms * config.slot_samples[ver]["slot"] / config.SAMPLE_RATE
            decoded_packets.append(result)
            attempt["decoded"] = True
            attempt["reason"] = "ok"
            attempt["ntw_id"] = result.get("ntw_id")
            attempt["seq_num"] = result.get("seq_num")
        elif pkt_info is not None:
            attempt["reason"] = "energy_too_low"

        all_attempts.append(attempt)

    # De-duplicate
    unique: list[dict] = []
    for pkt in decoded_packets:
        is_dup = False
        for upkt in unique:
            if (abs(pkt["F0_hz"] - upkt["F0_hz"]) < config.F0_TOL and
                    abs(pkt["time_s"] - upkt["time_s"]) < config.TIME_TOL):
                if pkt["total_energy_dB"] > upkt["total_energy_dB"]:
                    upkt.update(pkt)
                is_dup = True
                break
        if not is_dup:
            unique.append(pkt)

    return unique, unique, all_attempts
