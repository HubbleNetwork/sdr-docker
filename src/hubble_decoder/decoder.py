import numpy as np
from scipy.signal import fftconvolve
import timeit

# TODO: this is taken from sim-decode, should be in a common location

class FastDecoder:
    PREAMBLE = [1, 0, 1, 0, 1, 0, 1, 1]
    SYMBOL_TIME = 8e-3
    SAMPLING_RATE = 781250
    FFT_SIZE = 16384
    SYNTH_RES = 373

    def __init__(self, data):
        self.data = data
        self.samples_per_symbol = int(self.SYMBOL_TIME * self.SAMPLING_RATE)
        self.zero_time_samples = int(0.2 * self.SYMBOL_TIME * self.SAMPLING_RATE)
        self.effective_symbol_samples = int(1.2 * self.SYMBOL_TIME * self.SAMPLING_RATE)
        expanded_preamble = []
        for symbol in self.PREAMBLE:
            expanded_preamble.extend([symbol] * self.samples_per_symbol)
            expanded_preamble.extend([0] * self.zero_time_samples)
        self.expanded_preamble = np.array(expanded_preamble)

    def _correct_symbol(self, symbol):
        search_window = int(0.1 * self.samples_per_symbol)
        search_start = max(0, symbol - search_window)
        search_end = min(len(self.data), symbol + search_window)
        search_region = np.abs(self.data[search_start:search_end])
        zero_time_samples = int(0.2 * self.SYMBOL_TIME * self.SAMPLING_RATE)
        zero_start = np.argmin(
            np.convolve(search_region, np.ones(zero_time_samples), mode="valid")
        )
        return search_start + zero_start + zero_time_samples

    def detect_and_validate_preamble(self, num_data_symbols=26, threshold=0.8):
        def expand_preamble_with_zeros(preamble, sampling_rate, symbol_time):
            samples_per_symbol = int(symbol_time * sampling_rate)
            zero_samples = int(0.2 * symbol_time * sampling_rate)

            expanded_preamble = []
            for symbol in preamble:
                expanded_preamble.extend([symbol] * samples_per_symbol)
                expanded_preamble.extend([0] * zero_samples)
            return np.array(expanded_preamble)

        # Expand the preamble with zero gaps
        expanded_preamble = expand_preamble_with_zeros(
            self.PREAMBLE, self.SAMPLING_RATE, self.SYMBOL_TIME
        )

        # Cross-correlation to detect the preamble
        correlation = fftconvolve(
            np.abs(self.data), expanded_preamble[::-1], mode="valid"
        )
        correlation /= np.max(correlation)

        # Detect the first peak above the threshold
        peaks = np.where(correlation > threshold)[0]
        if len(peaks) == 0:
            print("No preamble detected.")
            return False, [], []

        preamble_start = peaks[0]
        preamble_start = self._correct_symbol(preamble_start)

        # Demodulate the preamble symbols
        samples_per_symbol = int(self.SYMBOL_TIME * self.SAMPLING_RATE)
        demodulated_preamble = []
        for i in range(len(self.PREAMBLE)):
            start_idx = preamble_start + i * self.effective_symbol_samples
            end_idx = start_idx + samples_per_symbol
            symbol_samples = self.data[start_idx:end_idx]
            avg_magnitude = np.mean(np.abs(symbol_samples))
            demodulated_preamble.append(
                1 if avg_magnitude > 0.5 else 0
            )  # Thresholding for OOK

        # Validate the demodulated preamble
        if demodulated_preamble != self.PREAMBLE:
            return False, [], []

        # Calculate the start indices for preamble and data symbols
        preamble_symbols = [
            preamble_start + i * self.effective_symbol_samples
            for i in range(len(self.PREAMBLE))
        ]
        data_start = preamble_start + len(self.PREAMBLE) * self.effective_symbol_samples
        data_symbols = [
            data_start + i * self.effective_symbol_samples
            for i in range(num_data_symbols)
        ]

        data_symbols = [self._correct_symbol(symbol) for symbol in data_symbols]

        return True, preamble_symbols, data_symbols

    def _demodulate_data_symbol(self, transmitter_freq, symbol_index):
        symbol = self.data[symbol_index : symbol_index + self.samples_per_symbol]
        symbol = np.pad(symbol, (0, self.FFT_SIZE - len(symbol)))
        fft = np.fft.fft(symbol)
        freqs = np.fft.fftfreq(self.FFT_SIZE, 1 / self.SAMPLING_RATE)
        data_freq = freqs[np.argmax(np.abs(fft))]
        return round((data_freq - transmitter_freq) / self.SYNTH_RES)

    def demodulate_symbols(self, preamble_symbols, data_symbols):
        last_preamble_idx = preamble_symbols[-1]
        last_preamble_symbol = self.data[
            last_preamble_idx : last_preamble_idx + self.samples_per_symbol
        ]
        last_preamble_symbol = np.pad(
            last_preamble_symbol, (0, self.FFT_SIZE - len(last_preamble_symbol))
        )
        fft = np.fft.fft(last_preamble_symbol)
        freqs = np.fft.fftfreq(self.FFT_SIZE, 1 / self.SAMPLING_RATE)
        transmitter_freq = freqs[np.argmax(np.abs(fft))]
        decoded_symbols = [
            self._demodulate_data_symbol(transmitter_freq, symbol)
            for symbol in data_symbols
        ]
        return decoded_symbols

    def symbols_to_byte_fields(self, symbols):
        symbols = np.array(symbols, dtype=np.uint8)
        # number of data symbols = 11 + bottom 5 bits of header symbol
        # the header is repeated at symbols 0, 9, 18
        header_symbol = symbols[0]
        num_data_symbols = 11 + (header_symbol & 0b11111)

        # drop the headers
        header_indices = [0, 9, 18]
        mask = np.ones(len(symbols), dtype=bool)
        mask[header_indices] = False
        symbols = symbols[mask]

        binary_string = "".join(f"{symbol:06b}" for symbol in symbols)

        # device ID: first 34 bits
        device_id = int(binary_string[:34], 2)

        # payload: 6 * num_data_symbols bits
        payload = binary_string[: 6 * num_data_symbols]
        payload = payload.ljust(160, "0")
        payload_bytes = [int(payload[i : i + 8], 2) for i in range(0, len(payload), 8)]
        payload_bytes = np.array(payload_bytes, dtype=np.uint8)

        return device_id, payload_bytes
