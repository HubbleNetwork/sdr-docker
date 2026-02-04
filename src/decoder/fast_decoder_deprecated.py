import errno
import numpy as np
from scipy import signal
import statistics
import reedsolo

# TODO: move these to a constants file later
DATA_LEN_ARRAY = list(range(11, 26, 2))
ERROR_SYMBOLS_LEN_ARRAY = [10] * 2 + [12] * 2 + [14] * 2 + [16] * 2
PREAMBLE_CODE = [1, 0, 1, 0, 1, 0, 1, 1]  # ON OFF keying sequence for preamble
SYMBOL_TIME = 8e-3  # symbol duration
FS = 2e6
FFT_SIZE = 2**14  # FFT samples over one symbol period (with zero padding)
RS_C_EXP = 6  # Galois field exponent for RS(2^6)
RS_PRIMITIVE_POLY = [1, 1, 0, 0, 0, 0, 1]
RS_PRIMITIVE_POLY_VALUE = 0x43

PACKET_HEADER_SYMBOLS = 10 # 34 bits (devID) + 10 bits (seq number) + 16 bits (auth tag)

class FastDecoderDeprecated:

    def __init__(self, data, frequency_step=373):
        self.samples_per_symbol = int(SYMBOL_TIME * FS)
        self.zero_time_samples = int(0.2 * SYMBOL_TIME * FS)
        self.effective_symbol_samples = int(1.2 * SYMBOL_TIME * FS)
        expanded_preamble = []
        for symbol in PREAMBLE_CODE:
            expanded_preamble.extend([symbol] * self.samples_per_symbol)
            expanded_preamble.extend([0] * self.zero_time_samples)
        self.expanded_preamble = np.array(expanded_preamble)
        self.set_data(data)
        self.synth_res = frequency_step

    def set_data(self, data):
        self.data = data / np.max(np.abs(data))

    def _correct_symbol(self, symbol):
        """Given a symbol index, find the point in the region around the symbol
        where the correlation is maximized.

        Note: not a very reslient method, and only works with high SNR

        Args:
            symbol (int): The symbol index to correct

        Returns:
            int: The corrected symbol index
        """
        search_start = max(0, symbol - self.effective_symbol_samples)
        search_end = min(len(self.data), symbol + self.effective_symbol_samples)
        search_region = np.abs(self.data[search_start:search_end])
        template = np.ones(self.samples_per_symbol)

        # if search region is smaller than template
        # then we cannot do correlation
        if len(search_region) < len(template):
            return symbol
        
        correlation = np.correlate(search_region, template, mode="valid")
        return search_start + np.argmax(correlation)

    def _detect_preamble(self, data=None, threshold=0.8):
        """Detect a single preamble in the data

        Args:
            data (ndarray): The data to search for the preamble. If None, uses the data set in the object.
            threshold (float, optional): Correlation threshold to detect the preamble. Defaults to 0.8.

        Returns:
            bool: True if preamble is detected
            list: List of indices in the data where the preamble syhmbols are detected
        """

        if data is None:
            data = self.data

        # Cross-correlation to detect the preamble
        try:
            correlation = signal.fftconvolve(
                np.abs(data), self.expanded_preamble[::-1], mode="valid"
            )
            correlation /= np.max(correlation)
        except ValueError:
            return False, []

        # Detect the first peak above the threshold & correct it
        peaks = np.where(correlation > threshold)[0]
        if len(peaks) == 0:
            return False, []

        preamble_start = self._correct_symbol(peaks[0])

        preamble_symbols = [
            preamble_start + i * self.effective_symbol_samples
            for i in range(len(PREAMBLE_CODE))
        ]

        # Demodulate the preamble symbols with simple thresholding for OOK
        demodulated_preamble = []
        for start_idx in preamble_symbols:
            end_idx = start_idx + self.samples_per_symbol
            symbol_samples = data[start_idx:end_idx]
            avg_magnitude = np.mean(np.abs(symbol_samples))
            demodulated_preamble.append(1 if avg_magnitude > 0.5 else 0)

        if demodulated_preamble != PREAMBLE_CODE:
            return False, preamble_symbols

        return True, preamble_symbols

    def find_all_preambles(self, limit: int = 10, threshold: float = 0.8):
        # find the first preamble using _detect_preamble
        # then skip to the end of the preamble + 24 symbols and search again
        # repeat until limit is reached, or reaching the end of the data
        preambles = []
        start_idx = 0
        while len(preambles) < limit:
            found, preamble_symbols = self._detect_preamble(
                self.data[start_idx:], threshold
            )
            if preamble_symbols == []:
                break
            if found:
                preambles.append([idx + start_idx for idx in preamble_symbols])

            start_idx = (
                start_idx + preamble_symbols[-1] + 24 * self.effective_symbol_samples
            )

            # check if we have enough data left for another packet
            remaining_symbols = (
                len(self.data) - start_idx
            ) // self.effective_symbol_samples
            if remaining_symbols < 32:
                break

        return preambles

    def _dominant_symbol_frequency(self, symbol_data):
        symbol = np.pad(symbol_data, (0, FFT_SIZE - len(symbol_data)))
        fft = np.fft.fft(symbol)
        freqs = np.fft.fftfreq(FFT_SIZE, 1 / FS)
        return freqs[np.argmax(np.abs(fft))]

    def _demodulate_data_symbol(self, transmitter_freq, symbol_data):
        """Given the data corresponding to a symbol, demodulate the symbol

        Args:
            transmitter_freq (float): The freuqnecy of the last preamble symbol
            symbol_data (ndarray): The data corresponding to the symbol

        Returns:
            int: The demodulated symbol
        """
        freq = self._dominant_symbol_frequency(symbol_data)
        return round((freq - transmitter_freq) / self.synth_res)

    def _demodulate_symbols_from_indices(self, transmitter_freq, indices):
        symbols = [self.data[i : i + self.samples_per_symbol] for i in indices]
        return [
            self._demodulate_data_symbol(transmitter_freq, symbol) for symbol in symbols
        ]

    @staticmethod
    def total_symbols(num_data_symbols):
        # TODO is there a way to compute this without hardcoding?
        # based on https://hubblenetwork.atlassian.net/wiki/x/AYA_Aw
        mac_length_symbols = 3
        if num_data_symbols <= 13:
            error_control_symbols = 10
        elif num_data_symbols <= 17:
            error_control_symbols = 12
        elif num_data_symbols <= 21:
            error_control_symbols = 14
        else:
            error_control_symbols = 16
        return num_data_symbols + mac_length_symbols + error_control_symbols

    def _analyze_packet_timing(self, data_start, total_symbols):
        """
        Analyze symbol timing for a single packet (per preamble).

        Args:
            data_start (int): sample index of first data symbol
            total_symbols (int): total number of symbols in the packet

        Returns:
            dict or None:
                {
                    "total_data_symbols": int,    # symbols analyzed (excluding preamble)
                    "offsets_us": list[float],    # offsets in microseconds
                    "offsets_samples": list[int], # offsets in samples
                    "offset_stats": {
                        "mean_offset": float,     # mean offset in us
                        "std_offset": float,      # standard deviation of offsets in us
                        "max_abs_offset": float   # maximum absolute offset in us
                    },
                    "gaps_us": list[float]        # gaps between symbols in us
                }
                or None if something failed
        """

        if total_symbols <= 0:
            return None
        
        corrected_starts = []
        offsets = []

        for i in range(total_symbols):
            nominal = data_start + i * self.effective_symbol_samples

            # check if we have enough data left
            if nominal + self.samples_per_symbol >= len(self.data):
                break

            corrected = self._correct_symbol(nominal)

            corrected_starts.append(corrected)
            offsets.append(corrected - nominal)
        
        if not offsets:
            return None

        # convert to microseconds
        offsets_us = (np.array(offsets, dtype=float) * 1e6) / FS

        # get the mean, standard deviation, and max offset
        offset_stats = {
            "mean_offset": round(float(np.mean(offsets_us)), 2),
            "std_offset": round(float(np.std(offsets_us)), 2),
            "max_abs_offset": round(float(np.max(np.abs(offsets_us))), 2)
        }

        # make sure these return back to Python type
        offsets = [int(o) for o in offsets]
        offsets_us = [float(o) for o in offsets_us]

        # return len of offset because that's the number of
        # data symbols analyzed
        timing_analysis = {
            "total_data_symbols": len(offsets),
            "offsets_us": offsets_us,
            "offsets_samples": offsets,
            "offset_stats": offset_stats
        }

        # compute the gap between each symbol
        gaps_us = None
        if len(corrected_starts) >= 2:
            gaps_samples = [
                corrected_starts[i+1] - corrected_starts[i]
                for i in range(len(corrected_starts) - 1)
            ]

            gaps_us = [((float(g) * 1e6) / FS) for g in gaps_samples]

            gap_stats = {
                "mean_gap": round(float(np.mean(gaps_us)), 2),
                "std_gap": round(float(np.std(gaps_us)), 2),
                "max_gap": round(float(np.max(gaps_us)), 2)
            }

            timing_analysis["gaps_us"] = gaps_us
            timing_analysis["gap_stats"] = gap_stats

        return timing_analysis

    def demodulate_symbols(self, preamble_symbols, symbol_timing_debug: bool = False):
        """Given the indices of the preamble symbols, demodulate the data symbols

        Length of the packet is determined based on the header symbols

        Args:
            preamble_symbols (list): The indices of the preamble symbols
            symbol_timing_debug (bool, optional): If True, also return symbol timing analysis

        Returns:
            list: The demodulated data symbols
            dict or None: Symbol timing analysis if requested, else None
        """

        # compute the reference frequency from the last preamble symbol
        last_preamble = self.data[
            preamble_symbols[-1] : preamble_symbols[-1] + self.samples_per_symbol
        ]
        transmitter_freq = self._dominant_symbol_frequency(last_preamble)

        # find the number of symbols by first demodulating the header symbols
        # number of data symbols = 11 + bottom 5 bits of header symbol
        # the header is repeated at symbols 0, 9, 18
        data_start = preamble_symbols[-1] + self.effective_symbol_samples
        header_indices = [
            data_start + i * self.effective_symbol_samples for i in (0, 9, 18)
        ]
        header_symbols = self._demodulate_symbols_from_indices(
            transmitter_freq, header_indices
        )

        # compute total number of symbols from the mode of the headers
        num_data_symbols = statistics.mode(
            [(symbol & 0b11111) for symbol in header_symbols]
        )

        if not (0 <= num_data_symbols < len(DATA_LEN_ARRAY)):
            return None, None
        
        num_data_symbols = DATA_LEN_ARRAY[num_data_symbols]

        total_symbols = self.total_symbols(num_data_symbols)

        # finally, demodulate the correct number of data symbols
        data_indices = [
            data_start + i * self.effective_symbol_samples for i in range(total_symbols)
        ]
        data_symbols_demodulated = self._demodulate_symbols_from_indices(
            transmitter_freq, data_indices
        )

        # if symbol timing debug is requested, perform the analysis
        if symbol_timing_debug:
            timing_info = self._analyze_packet_timing(data_start, total_symbols)
            return data_symbols_demodulated, timing_info

        return data_symbols_demodulated, None

    @staticmethod
    def extract_device_id_and_payload(symbols):
        """Extract the device ID and payload from the demodulated symbols

        Args:
            symbols (list): The demodulated symbols

        Returns:
            int: The device ID
            ndarray: The payload bytes
            int: The number of corrected errors, or negative error code
        """
        symbols = np.array(symbols, dtype=np.uint8)

        # recompute the number of data symbols from the headers
        header_indices = [0, 9, 18]
        headers = [symbols[i] for i in header_indices]
        num_data_symbols = statistics.mode([(symbol & 0b11111) for symbol in headers])

        if not (0 <= num_data_symbols < len(DATA_LEN_ARRAY)):
            return None, None, -errno.EINVAL

        # this works when the DATA_LEN_ARRAY has the same len as ERROR_SYMBOLS_LEN_ARRAY
        num_err_control_symbols = ERROR_SYMBOLS_LEN_ARRAY[num_data_symbols]
        num_data_symbols = DATA_LEN_ARRAY[num_data_symbols]

        # drop the headers
        mask = np.ones(len(symbols), dtype=bool)
        mask[header_indices] = False
        symbols = symbols[mask]

        # correct the symbols using Reed-Solomon
        # prim_poly = [1, 1, 0, 0, 0, 0, 1] -> 0x43;
        corrected_symbols = symbols
        rsc = reedsolo.RSCodec(num_err_control_symbols, c_exp=RS_C_EXP, prim=RS_PRIMITIVE_POLY_VALUE, fcr=1)

        try:
            corrected_symbols, full, errata_pos = rsc.decode(bytes(symbols))
            corrected_symbols = np.frombuffer(corrected_symbols, dtype=np.uint8)
            errata_pos_len = len(errata_pos)
        except reedsolo.ReedSolomonError:
            errata_pos_len = -errno.EILSEQ
        except Exception:
            errata_pos_len = -errno.EIO

        # convert what's left to a binary string
        binary_string = "".join(f"{symbol:06b}" for symbol in corrected_symbols)

        # device ID: first 34 bits
        device_id = int(binary_string[:34], 2)

        # payload: 6 * num_data_symbols bits
        payload = binary_string[PACKET_HEADER_SYMBOLS * 6 : 6 * num_data_symbols]

        # strip padding bits. The payload contains 1 followed by 0s until the nearest
        # packet length.
        payload = payload.rstrip("0")[:-1]

        payload_bytes = [int(payload[i : i + 8], 2) for i in range(0, len(payload), 8)]
        payload_bytes = np.array(payload_bytes, dtype=np.uint8)

        return device_id, payload_bytes, errata_pos_len

    def get_frequency_channel(self, preamble_symbols):
        """
        Returns the frequency channel as an integer

        Args: 
            preamble_symbols (list): The indices of the preamble symbols

        Returns:
            int: The frequency channel
        """

        # Compute the reference frequency from the last preamble symbol
        last_preamble = self.data[
            preamble_symbols[-1] : preamble_symbols[-1] + self.samples_per_symbol
        ]
        transmitter_freq = self._dominant_symbol_frequency(last_preamble)
        freq_channel = np.floor((transmitter_freq / (64 * self.synth_res)))
        
        return int(freq_channel)
        