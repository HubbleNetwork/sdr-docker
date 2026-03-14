"""Configuration constants and derived parameters for the SDR stream decoder."""

import os

import numpy as np
import reedsolo as rs

# -- SDR selection (override with environment variables) --------------------
SDR_TYPE = os.environ.get("SDR_TYPE", "pluto").lower()  # "pluto" | "bladerf"

# -- PlutoSDR connection (ignored when SDR_TYPE != "pluto") -----------------
PLUTO_URI = os.environ.get("PLUTO_URI", "ip:192.168.2.1")

# -- Radio parameters (shared across SDR backends) -------------------------
CENTER_FREQ_HZ = int(2.482754875e9)
SAMPLE_RATE = 781_250  # 6.25 MHz / 8
RX_BUFFER_SIZE = 2 ** 16  # ~84 ms per read
RF_BANDWIDTH = int(SAMPLE_RATE)
RX_GAIN_MODE = "manual"
RX_INITIAL_GAIN_DB = 40
RX_GAIN_MIN_DB = 0
RX_GAIN_STEP_DB = 2

# -- Per-SDR hardware parameters -------------------------------------------
# gr-soapy always outputs CF32 normalised to ±1.0 regardless of backend
ADC_FULL_SCALE = 1.0

if SDR_TYPE == "bladerf":
    RX_GAIN_MAX_DB = 60    # bladeRF 2.0 Micro A4 overall-gain range
else:  # pluto (default)
    RX_GAIN_MAX_DB = 71    # AD9361 max at >1.3 GHz

# -- Spectrogram (visualisation) -------------------------------------------
NFFT_VIS = 2 ** 12  # 4096
NOVERLAP_VIS = NFFT_VIS // 4  # 1024
SPEC_DURATION_S = 10.0  # 10 s rolling window
SPEC_CHUNK_S = 0.5  # compute spec on 0.5 s chunks
SPEC_CHUNK_SAMPLES = int(SPEC_CHUNK_S * SAMPLE_RATE)
MAX_SPEC_CHUNKS = int(SPEC_DURATION_S / SPEC_CHUNK_S)  # 20 chunks -> 10 s

# IQ circular buffer: ~2 s for decode + headroom
IQ_BUFFER_SIZE = int(2.0 * SAMPLE_RATE)

# Target image size for web display
SPEC_IMG_WIDTH = 1200
SPEC_IMG_HEIGHT = 200

# -- Decoder ---------------------------------------------------------------
DECODE_WINDOW_S = 1.0  # decode 1 s at a time
DECODE_INTERVAL_S = 0.5  # run decoder every 0.5 s
DECODE_SAMPLES = int(DECODE_WINDOW_S * SAMPLE_RATE)

# Detection spectrogram
NFFT_DET = 625
NOVERLAP_DET = 0

# Preamble (shared)
SYMBOL_DURATION_S = 8e-3
PREAMBLE_LEN = 8
PREAMBLE_BITS = [1, 0, 1, 0, 1, 0, 1, 1]

# Dual-protocol gap durations
GAP_DURATIONS = {
    -1: 1.6e-3,  # v-1: 1.6 ms inter-symbol gap (OOK)
     1: 0.8e-3,  # v1:  0.8 ms inter-symbol gap (FSK)
}

# FSK
NUM_FSK_BINS = 64

# v-1 specific
SYMBOLS_PER_PACKET_VNEG1 = 32
DATA_LEN_VNEG1 = SYMBOLS_PER_PACKET_VNEG1 - PREAMBLE_LEN  # 24
FREQ_STEP_VNEG1 = 488.28125

# Template matching
DETECTION_THRESHOLD = 0.5
TEMPLATE_FREQ_BINS = 3
MAX_RAW = 10_000

# Filtering
PREAMBLE_F0_SNR_MIN = 5.0
MIN_ENERGY_DBFS = -80.0


# -- Web server & app behaviour --------------------------------------------
FLASK_PORT = 8050
VERBOSE = False
MAX_DECODE_HISTORY = 200
SDR_RETRY_INTERVAL_S = 3  # seconds between SDR connect retries

# -- Time-domain viewer ----------------------------------------------------
TD_WINDOW_S = 0.5

# ===========================================================================
# Derived parameters (do not modify below this line)
# ===========================================================================

# -- Protocol parameters ---------------------------------------------------
SYNTH_RES = {
    "ti": 338.0,
    "nordic": 488.28125,
    "silabs": 370,
    "esp": 400,
    "atmosic": 500,
}

# v1 Reed-Solomon block sizes: index 0 = header, 1-4 = PDU lengths
RS_K_V1 = [2, 13, 18, 25, 30]   # data symbols
RS_N_V1 = [6, 23, 30, 39, 46]   # total (data + parity)

# v-1 Reed-Solomon block sizes
RS_K_VNEG1 = [11, 13, 15, 17, 19, 21, 23, 25]
RS_N_VNEG1 = [21, 23, 27, 29, 33, 35, 39, 41]

NUM_HEADER_SYMS = RS_N_V1[0]  # 6
NUM_CHANNELS = 19
LO_CHANNEL = 9  # channel 9 center = CENTER_FREQ_HZ (0 Hz baseband)
CHANNEL_SPACING = 25_500.0  # Hz
NUM_SYM_PER_HOP = 16
HOPPING_SEQS = [
    [3, 14, 5, 6, 9, 2, 12, 8, 15, 4, 11, 13, 17, 10, 1, 7, 0, 18, 16],
    [10, 3, 15, 5, 0, 17, 13, 6, 11, 4, 8, 18, 9, 14, 1, 12, 7, 16, 2],
    [14, 5, 11, 3, 8, 2, 18, 4, 10, 13, 9, 1, 16, 17, 0, 6, 15, 12, 7],
    [7, 0, 11, 18, 4, 2, 13, 5, 10, 17, 3, 9, 16, 14, 8, 12, 1, 6, 15],
]
PAYLOAD_LEN_BYTES_V1 = [0, 4, 9, 13]

# Preamble ON/OFF indices
preamble_on_idx = [i for i, b in enumerate(PREAMBLE_BITS) if b == 1]
preamble_off_idx = [i for i, b in enumerate(PREAMBLE_BITS) if b == 0]

# v1 preamble code indices (for F31/F0 estimation)
PREAMBLE_CODE_V1 = [31, 0, 31, 0, 31, 0, 31, 31]
on_indices_v1 = [i for i, b in enumerate(PREAMBLE_CODE_V1) if b == 31]
off_indices_v1 = [i for i, b in enumerate(PREAMBLE_CODE_V1) if b == 0]

# -- Derived timing --------------------------------------------------------
samples_per_symbol = int(SYMBOL_DURATION_S * SAMPLE_RATE)
fft_freqs = np.fft.fftfreq(samples_per_symbol, d=1.0 / SAMPLE_RATE)

slot_samples = {}
for _ver, _gap_s in GAP_DURATIONS.items():
    _gap = int(_gap_s * SAMPLE_RATE)
    slot_samples[_ver] = {"gap": _gap, "slot": samples_per_symbol + _gap}

time_step_s = NFFT_DET / SAMPLE_RATE
bins_on = int(round(SYMBOL_DURATION_S / time_step_s))

NMS_TIME_BINS = max(
    bins_on + int(round(g / time_step_s)) for g in GAP_DURATIONS.values()
) * 4
NMS_FREQ_BINS = 15

F0_TOL = 2 * max(SYNTH_RES.values())
TIME_TOL = 0.5

# -- Reed-Solomon init -----------------------------------------------------
rs_prim = rs.find_prime_polys(c_exp=6, fast_primes=True, single=False)[0]
rs.init_tables(c_exp=6, prim=rs_prim)

# -- Build preamble templates (both protocols) ------------------------------
templates = {}
for _phy_ver, _gap_s in GAP_DURATIONS.items():
    _bins_gap = int(round(_gap_s / time_step_s))
    _bins_per_slot = bins_on + _bins_gap
    _pattern_1d = []
    for bit in PREAMBLE_BITS:
        if bit == 1:
            _pattern_1d.extend([1.0] * bins_on + [0.0] * _bins_gap)
        else:
            _pattern_1d.extend([0.0] * _bins_per_slot)
    _pattern_1d = np.array(_pattern_1d, dtype=np.float32)
    _template_2d = np.tile(_pattern_1d, (TEMPLATE_FREQ_BINS, 1))
    _template_uint8 = (_template_2d * 255).astype(np.uint8)
    templates[_phy_ver] = {
        "uint8": _template_uint8,
        "gap_s": _gap_s,
        "bins_gap": _bins_gap,
        "bins_per_slot": _bins_per_slot,
        "width": len(_pattern_1d),
        "duration_s": len(_pattern_1d) * time_step_s,
    }
