"""PlutoSDR RX thread: reads IQ samples into a circular buffer."""

import time

import adi
import numpy as np

from . import config


def rx_loop(state):
    """Connect to PlutoSDR and continuously fill the shared IQ circular buffer.

    Retries connection every SDR_RETRY_INTERVAL_S until successful or stopped.
    """
    sdr = None
    while state.running.is_set() and sdr is None:
        try:
            sdr = adi.Pluto(uri=config.PLUTO_URI)
        except Exception as e:
            print(f"[RX] PlutoSDR not found ({e}), retrying in {config.SDR_RETRY_INTERVAL_S}s...")
            for _ in range(config.SDR_RETRY_INTERVAL_S * 10):
                if not state.running.is_set():
                    print("[RX] Stopped (never connected).")
                    return
                time.sleep(0.1)

    if sdr is None:
        return

    sdr.rx_lo = config.PLUTO_FREQ_HZ
    sdr.sample_rate = config.SAMPLE_RATE
    sdr.rx_rf_bandwidth = config.RF_BANDWIDTH
    sdr.rx_buffer_size = config.RX_BUFFER_SIZE
    sdr.gain_control_mode_chan0 = config.RX_GAIN_MODE
    if config.RX_GAIN_MODE == "manual":
        sdr.rx_hardwaregain_chan0 = int(config.RX_INITIAL_GAIN_DB)
    state.rx_connected.set()
    print(
        f"[RX] Connected -- LO={sdr.rx_lo / 1e9:.5f} GHz, "
        f"fs={sdr.sample_rate:,.0f} Hz, "
        f"gain_mode={config.RX_GAIN_MODE}, gain={config.RX_INITIAL_GAIN_DB} dB"
    )

    cur_gain = config.RX_INITIAL_GAIN_DB
    state.rx_gain_dB = cur_gain

    while state.running.is_set():
        try:
            samples = sdr.rx()
        except Exception as e:
            print(f"[RX] Error: {e}")
            time.sleep(0.1)
            continue

        peak = max(np.max(np.abs(samples.real)), np.max(np.abs(samples.imag)))
        state.rx_peak_frac = peak / config.ADC_FULL_SCALE

        # Apply gain change requested by UI
        if config.RX_GAIN_MODE == "manual" and state.rx_gain_dB != cur_gain:
            new_gain = int(max(config.RX_GAIN_MIN_DB, min(config.RX_GAIN_MAX_DB, state.rx_gain_dB)))
            try:
                sdr.gain_control_mode_chan0 = "manual"
                sdr.rx_hardwaregain_chan0 = new_gain
                cur_gain = new_gain
                state.rx_gain_dB = cur_gain
                print(f"[GAIN] Set to {cur_gain} dB")
            except OSError as e:
                print(f"[GAIN] Failed to set {new_gain} dB: {e}")
                state.rx_gain_dB = cur_gain

        n = len(samples)
        with state.lock:
            space = config.IQ_BUFFER_SIZE - state.buf_write_idx
            if n <= space:
                state.iq_buffer[state.buf_write_idx: state.buf_write_idx + n] = samples
                state.buf_write_idx += n
            else:
                state.iq_buffer[state.buf_write_idx:] = samples[:space]
                remainder = n - space
                state.iq_buffer[:remainder] = samples[space:]
                state.buf_write_idx = remainder

    sdr.rx_destroy_buffer()
    if config.VERBOSE:
        print("[RX] Stopped.")
