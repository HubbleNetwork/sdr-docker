import errno
import math
import numpy as np
from scipy import signal
import statistics
import reedsolo

# Constants
# TODO: move this to a separate constants file later
PLUTO_SAMPLING_RATE = 2e6
HEADER_LEN = 2 # 2 symbols for header
PREAMBLE_CODE = [1, 0, 1, 0, 1, 0, 1, 1]  # 2 FSK sequence for preamble
NUM_HEADER_SYMBOLS = 6 # Number of header symbols err correction symbols

DATA_LEN_ARRAY = [13, 18, 25, 30]
ERROR_SYMBOLS_LEN_ARRAY = [10, 12, 14, 16]

SYMBOL_SIZE = 6  # bits per symbol
SYMBOL_TIME = 8e-3  # symbol duration

ZERO_TIME = 0.1 * SYMBOL_TIME  # Time between symbols
EFFECTIVE_SYMBOL_TIME = (SYMBOL_TIME + ZERO_TIME)
FFT_SIZE = 2**14  # FFT samples over one symbol period (with zero padding)

RS_C_EXP = 6  # Galois field exponent for RS(2^6)
RS_PRIMITIVE_POLY = [1, 1, 0, 0, 0, 0, 1]
RS_PRIMITIVE_POLY_VALUE = 0x43

# Header:
# 4 bits (PHY protocol version) + 2 bits (packet len) + 6 bits (channel num)
# Payload metadata:
# 2 bits (Payload Protocol) + 10 bits (seq num) + 32 bits (dynamic netID) + 32 bits (auth tag) + 2 bits RFU
PACKET_METADATA_SYMBOLS = 13

FRAME_SIZE_MAX = 16
NUM_CHANNELS = 19

HOPPING_SEQUENCE = [
    # Sequence 1 (Seed = 1)
    [3, 14, 5, 6, 9, 2, 12, 8, 15, 4, 11, 13, 17, 10, 1, 7, 0, 18, 16], 

    # Sequence 2 (Seed = 7)
    [10, 3, 15, 5, 0, 17, 13, 6, 11, 4, 8, 18, 9, 14, 1, 12, 7, 16, 2],

    # Sequence 3 (Seed = 42)
    [14, 5, 11, 3, 8, 2, 18, 4, 10, 13, 9, 1, 16, 17, 0, 6, 15, 12, 7],

    # Sequence 4 (Seed = 99)
    [7, 0, 11, 18, 4, 2, 13, 5, 10, 17, 3, 9, 16, 14, 8, 12, 1, 6, 15]
]

class FastDecoderV1:

    def __init__(self, data, frequency_step=373):
        self.samples_per_symbol = int(SYMBOL_TIME * PLUTO_SAMPLING_RATE)
        self.zero_time_samples = int(ZERO_TIME * PLUTO_SAMPLING_RATE)
        self.effective_symbol_samples = int(EFFECTIVE_SYMBOL_TIME * PLUTO_SAMPLING_RATE)
        expanded_preamble = []
        for _ in PREAMBLE_CODE:
            expanded_preamble.extend([1] * self.samples_per_symbol)
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
    
    def _compute_all_preamble_correlations(self, data=None):
        """
        Compute the correlation of the entire data with the preamble

        Args:
            data (ndarray): The data to compute the correlation on.
                            If None, uses the data set in the object.

        Returns:
            ndarray: The correlation values
        """
        if data is None:
            data = self.data

        try:
            correlation = signal.fftconvolve(
                np.abs(data), self.expanded_preamble[::-1], mode="valid"
            )

            if np.max(correlation) == 0:
                return None
            
            correlation /= np.max(correlation)
        
        except ValueError:
            return None

        return correlation
        
    def _detect_preamble(self, preamble_indices, threshold=0.8, step=31):
        """
        Detect a single preamble in the data

        Args:
            preamble_indices (list): List of indices in the data where the preamble syhmbols are expected.
            step (int, optional): Frequency step size for 2 FSK. Defaults to 31.

        Returns:
            bool: True if preamble is detected
        """

        # get a low frequency reference from the 2nd symbol
        second_symbol_data = self.data[
            preamble_indices[1] : preamble_indices[1] + self.samples_per_symbol
        ]
        low_freq = self._dominant_symbol_frequency(second_symbol_data)
        
        # demodulate the preamble symbols relative to low freq
        demodulated_preamble = self._demodulate_symbols_from_indices(
            low_freq, preamble_indices
        )

        # TODO: remove the debug print
        print("Demodulated preamble:", demodulated_preamble)

        expected_preamble = [(0 if symbol == 0 else step) for symbol in PREAMBLE_CODE]

        # TODO: this might be too tight. Might be off by certain margin
        # e.g. +/- 2 steps
        if demodulated_preamble != expected_preamble:
            return False

        return True

    def find_all_preambles(self, limit: int = 10, threshold: float = 0.8, min_symbol_spacing: int = 2):
        """
        Find all valid preambles in a data window without skipping ahead by a whole packet.
        This allows detecting of overlaping packets.

        Args:
            limit (int, optional):
                Maximum number of preambles to find. Defaults to 50.
            threshold (float, optional):
                Correlation threshold to detect the preamble. Defaults to 0.8.
            min_symbol_spacing (int, optional):
                Minimum spacing between preambles in symbols. Defaults to 2.
        
        Returns:
            list[list[int]]: list of preambles; each preamble is a list of symbol
                             start indices in the data window.
        """

        if min_symbol_spacing < 1:
            min_symbol_spacing = 2

        correlations = self._compute_all_preamble_correlations()
        if correlations is None:
            return []

        min_sample_spacing = min_symbol_spacing * self.effective_symbol_samples

        # find peaks in correlation
        peaks, _ = signal.find_peaks(correlations, height=threshold, distance=min_sample_spacing)

        preambles = []
        accepted_starts = []

        for peak in peaks:
            if len(preambles) >= limit:
                break

            # index into corr, which aligns with data
            preamble_start = self._correct_symbol(int(peak))

            # check if we have enough data left for another packet
            # assume 0 bytes payload, 8 preamble + 6 header + 23 payload = 37 symbols
            remaining_symbols = (
                len(self.data) - preamble_start
            ) // self.effective_symbol_samples
            
            if remaining_symbols < 37:
                break

            # avoid duplicates
            if preamble_start in accepted_starts:
                continue
            
            preamble_indices = [
                preamble_start + i * self.effective_symbol_samples
                for i in range(len(PREAMBLE_CODE))
            ]
            
            if self._detect_preamble(preamble_indices, threshold):
                preambles.append(preamble_indices)
                accepted_starts.append(preamble_start)

        return preambles

    def _dominant_symbol_frequency(self, symbol_data):
        symbol = np.pad(symbol_data, (0, FFT_SIZE - len(symbol_data)))
        fft = np.fft.fft(symbol)
        freqs = np.fft.fftfreq(FFT_SIZE, 1 / PLUTO_SAMPLING_RATE)
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
        """
        Given the number of data symbols, compute the total number of symbols
        Args:
            num_data_symbols (int): The number of data symbols
        Returns:
            int: The total number of symbols (data + error control)
                 if invalid num_data_symbols, returns negative error code
        """
        # TODO is there a way to compute this without hardcoding?
        # based on https://hubblenetwork.atlassian.net/wiki/x/4IFcTQ
        
        try:
            index = DATA_LEN_ARRAY.index(num_data_symbols)
        except ValueError:
            return -errno.EINVAL
        
        # only works when DATA_LEN_ARRAY and ERROR_SYMBOLS_LEN_ARRAY
        # have the same length
        return num_data_symbols + ERROR_SYMBOLS_LEN_ARRAY[index]

    def _get_channel_spacing(self):
        """
        Get the channel spacing in Hz

        Returns:
            int: The channel spacing in Hz
        """
        # silabs case is special bc step = 74 (synth res) * 5
        if self.synth_res == 370:
            return 25500

        return min(64, math.floor(25500 / self.synth_res)) * self.synth_res

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
        offsets_us = (np.array(offsets, dtype=float) * 1e6) / PLUTO_SAMPLING_RATE

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

            gaps_us = [((float(g) * 1e6) / PLUTO_SAMPLING_RATE) for g in gaps_samples]

            gap_stats = {
                "mean_gap": round(float(np.mean(gaps_us)), 2),
                "std_gap": round(float(np.std(gaps_us)), 2),
                "max_gap": round(float(np.max(gaps_us)), 2)
            }

            timing_analysis["gaps_us"] = gaps_us
            timing_analysis["gap_stats"] = gap_stats

        return timing_analysis

    def _demodulate_header_symbols(self, preamble_symbols):
        """
        Given the indices of the preamble symbols, demodulate the header symbols

        Args:
            preamble_symbols (list): The indices of the preamble symbols

        Returns:
            list: The demodulated header symbols or None if failed
            float: The transmitter frequency of the packet's header or None if failed
        """
        # check if there is enough data after the preamble for header
        last_preamble_end = preamble_symbols[-1] + self.samples_per_symbol
        if (last_preamble_end + (self.effective_symbol_samples * NUM_HEADER_SYMBOLS)) > len(self.data):
            return None, None
        
        # compute the reference frequency from the 2nd preamble symbol (channel base)
        second_preamble = self.data[
            preamble_symbols[1] : preamble_symbols[1] + self.samples_per_symbol
        ]
        transmitter_freq = self._dominant_symbol_frequency(second_preamble)

        # find the number of symbols by first demodulating the header symbols
        header_start = preamble_symbols[-1] + self.effective_symbol_samples
        header_indices = [
            header_start + i * self.effective_symbol_samples for i in range(NUM_HEADER_SYMBOLS)
        ]
        header_symbols = self._demodulate_symbols_from_indices(
            transmitter_freq, header_indices
        )

        return header_symbols, transmitter_freq
    
    def _extract_header_info(self, header_symbols):
        """
        Extract information from the demodulated header symbols

        Args:
            header_symbols (list): The demodulated header symbols
        Returns:
            dict: The extracted header information including:
                {
                    "phy_protocol_version": int,
                    "payload_length": int,
                    "hopping_sequence_index": int,
                    "channel_index": int
                }

        """
        # rs err correction
        corrected_symbols, _ = self._reed_solomon_correct_symbols(header_symbols, (NUM_HEADER_SYMBOLS - HEADER_LEN))

        # convert what's left to a binary string
        binary_string = "".join(f"{symbol:06b}" for symbol in corrected_symbols)

        # phy protocol version: first 4 bits
        # payload length: next 2 bits
        # channel number: next 6 bits
        #   hopping sequence: 2 bits
        #   4 LSB of channel number: 4 bits
        phy_protocol_version = int(binary_string[0:4], 2)
        payload_length = int(binary_string[4:6], 2)
        hopping_sequence = int(binary_string[6:8], 2)
        channel_index = int(binary_string[8:12], 2)

        payload_length = DATA_LEN_ARRAY[payload_length]

        return {
            "phy_protocol_version": phy_protocol_version,
            "payload_length": payload_length,
            "hopping_sequence_index": hopping_sequence,
            "channel_index": channel_index
        }

    def _dewhitening_symbols(self, seed, symbols):
        """
        Dewhiten the given symbols using LFSR7

        Args:
            seed (int): The seed for the LFSR
            symbols (list): The symbols to dewhiten

        Returns:
            list: The dewhitened symbols or None if failed
        """

        # the seed can't be bigger than the transmit channels
        if seed < 0 or seed >= NUM_CHANNELS:
            return None

        # force to 6 bits (more like a safeguard)
        # TODO: maybe return error if symbols val > 6 bits value?
        out = [(s & 0b111111) for s in symbols]

        seed = 0b1000000 | seed
        state = (3 << 5) | seed

        symbol_state = 0
        sym_idx = 0

        # Let's do the dewhitening in place instead of generating
        # all masks and apply later
        for i in range(len(symbols) * 6):
            # pack 6 masking bits into 1 symbol (MSB first)
            symbol_state |= (((state & 0x40) >> 6) << (5 - (i % 6)))

            if (i % 6) == 5:
                out[sym_idx] ^= symbol_state

                # just a safe guard to keep 6 bits
                out[sym_idx] &= 0b111111
                sym_idx += 1
                symbol_state = 0

            # taps at 7 and 4
            fb = ((state >> 6) ^ (state >> 3)) & 1 
            state = ((state << 1) & 0x7F) | fb

        return out

    
    def _reed_solomon_correct_symbols(self, symbols, num_error_symbols):
        """
        Correct the given symbols using Reed-Solomon error correction

        Args:
            symbols (list): The demodulated symbols
            num_error_symbols (int): The number of error correction symbols

        Returns:
            list: The corrected symbols
            int: The number of corrected errors, or negative error code
        """
        # correct the symbols using Reed-Solomon
        # prim_poly = [1, 1, 0, 0, 0, 0, 1] -> 0x43;
        corrected_symbols = symbols
        rsc = reedsolo.RSCodec(num_error_symbols, c_exp=RS_C_EXP, prim=RS_PRIMITIVE_POLY_VALUE, fcr=1)

        try:
            corrected_symbols, full, errata_pos = rsc.decode(bytes(symbols))
            corrected_symbols = np.frombuffer(corrected_symbols, dtype=np.uint8)
            errata_pos_len = len(errata_pos)
        except reedsolo.ReedSolomonError:
            errata_pos_len = -errno.EILSEQ
        except Exception:
            errata_pos_len = -errno.EIO
        
        return corrected_symbols, errata_pos_len

    def _lfsr5_next(self, s):
        """
        Compute the next state of a 5-bit LFSR
        Args:
            s (int): Current state of the LFSR (5 bits)
        Returns:
            int: Next state of the LFSR (5 bits)
        """
        # 5-bit LFSR with taps at x^5 + x^2 + 1 (primitive polynomial)
        fb = ((s >> 4) ^ (s >> 1)) & 0x1
        return ((s << 1) & 0x1F) | fb  # Keep it 5 bits

    # TODO: check the seed and if this is the right implementation
    def _build_hops(self, seed=5, num_channels=NUM_CHANNELS):
        """
        Build the frequency hopping sequence using a 5-bit LFSR
        Args:
            seed (int, optional): Seed for the LFSR. Defaults to 5.
            num_channels (int, optional): Number of channels to hop through. Defaults to 19.
        Returns:
            list: The frequency hopping sequence
        """
        s = seed & 0x1F
        if s == 0:
            s = 1
        hops = []
        k = 0

        # Build a permutation hopping sequence
        for i in range(60):
            if k >= num_channels:
                break
            if 1 <= s <= num_channels:
                hops.append(s - 1)  # Channels are 0-indexed
                k += 1
            s = self._lfsr5_next(s)
        return hops

    def _get_hopping_sequence(self, channel_number, length, sequence_index):
        """
        Get the frequency hopping sequence for the given channel number

        Args:
            channel_number (int): The channel number
            length (int): The length of the hopping sequence (include input channel)
            sequence_index (int): The index of the hopping sequence to use
        Returns:
            list: The frequency hopping sequence (include the input channel)
                  or None if the channel number is not in the hopping sequence
        """
        if sequence_index < 0 or sequence_index >= len(HOPPING_SEQUENCE):
            return None
        
        hopping_sequence = HOPPING_SEQUENCE[sequence_index]
        channel_len = len(hopping_sequence)
        try:
            start_index = hopping_sequence.index(channel_number)
        except ValueError:
            return None

        sequence = [channel_number]
        for i in range(1, length):
            sequence.append(hopping_sequence[(start_index + i) % channel_len])
        return sequence

    def demodulate_symbols(self, preamble_symbols, symbol_timing_debug: bool = False):
        """Given the indices of the preamble symbols, demodulate the data symbols

        Length of the packet is determined based on the header symbols

        Args:
            preamble_symbols (list): The indices of the preamble symbols
            symbol_timing_debug (bool, optional): If True, also return symbol timing analysis

        Returns:
            list: The demodulated data symbols (not include header), or None if failed
            list: The hopping channel sequence, or None if failed
            int: Payload length in symbols, or None if failed
            dict or None: Symbol timing analysis if requested, else None
        """
        header_symbols, transmitter_freq = self._demodulate_header_symbols(preamble_symbols)
        if header_symbols is None or transmitter_freq is None:
            return None, None, None, None

        header_info = self._extract_header_info(header_symbols)
        sequence_index = header_info["hopping_sequence_index"]

        payload_length = header_info["payload_length"]
        total_symbols = self.total_symbols(payload_length)

        if total_symbols < 0:
            return None, None, None, None

        # get the correct number of data symbols
        data_start = preamble_symbols[-1] + self.effective_symbol_samples + self.effective_symbol_samples * 6

        data_indices = [
            data_start + i * self.effective_symbol_samples for i in range(total_symbols)
        ]

        # for the entire packet, each slot = 16 symbols
        # except for the last one which may be shorter
        # however, the 1st slot contains preamble + header
        # --> data sym = 16 - 8 (preamble) - 6 (header) = 2
        slots = []
        slots.append(data_indices[0:2]) # 1st slot
        for i in range(2, total_symbols, FRAME_SIZE_MAX):
            slots.append(data_indices[i:i+FRAME_SIZE_MAX])

        channel_spacing = self._get_channel_spacing()

        # if channel index is 0, 1, 2 it could be channel 0, 1, 2 or 16, 17, 18
        start_channel = header_info["channel_index"]
        if start_channel in [0, 1, 2]:
            possible_channels = [
                header_info["channel_index"],
                header_info["channel_index"] + 16
            ]

            candidate_sequences = [
                self._get_hopping_sequence(channel, len(slots), sequence_index) for channel in possible_channels
            ]

            if any(seq is None or len(seq) == 0 for seq in candidate_sequences):
                return None, None, None, None

            # choose the valid sequence by checking the freq of the 3rd data symbol
            # bc 1st 16 symbols are channel 1 (8 preamble + 6 header + 2 data)
            first_data_symbol = self.data[
                data_indices[3] : data_indices[3] + self.samples_per_symbol
            ]
            first_data_freq = self._dominant_symbol_frequency(first_data_symbol)

            expected_channel_gap = (first_data_freq - transmitter_freq) / channel_spacing

            compared_gaps = [
                candidate_sequences[i][1] - possible_channels[i]
                for i in range(2)
            ]

            # TODO: double check if this is a logic flaw
            errors = [abs(expected_channel_gap - g) for g in compared_gaps]

            # let error can deviate by +/- 1
            valid = [i for i, e in enumerate(errors) if e <= 1.0]

            if not valid:
                return None, None, None, None

            # just pick the first valid one
            start_channel = possible_channels[valid[0]]

        hopping_channels = self._get_hopping_sequence(start_channel, len(slots), sequence_index)

        if hopping_channels is None:
            return None, None, None, None

        # TODO: remove debug print
        print("Hopping channels:", hopping_channels)
        
        # 1st channel with it self = 0 gap
        channel_gaps = [0] + [
            hopping_channels[i] - hopping_channels[i - 1]
            for i in range(1, len(hopping_channels))
        ]
        
        demodulated_symbols = []
        for index, channel in enumerate(hopping_channels):
            # next channel base = current base + (channel spacing) * (channel difference)
            transmitter_freq = transmitter_freq + (channel_spacing * channel_gaps[index])
            demodulated_chunk = self._demodulate_symbols_from_indices(
                transmitter_freq,
                slots[index]
            )
            demodulated_symbols.extend(demodulated_chunk)

        dewhitened_symbols = self._dewhitening_symbols(start_channel, demodulated_symbols)
        if dewhitened_symbols is None:
            return None, None, None, None

        # if symbol timing debug is requested, perform the analysis
        if symbol_timing_debug:
            timing_info = self._analyze_packet_timing(data_start, total_symbols)
            return dewhitened_symbols, hopping_channels, payload_length, timing_info
    
        return dewhitened_symbols, hopping_channels, payload_length, None

    def extract_payload(self, demodulated_symbols, payload_length):
        """
        Extract the payload from the demodulated symbols

        Args:
            demodulated_symbols (list): The demodulated symbols
            payload_length (int): The length of the payload in symbols

        Returns:
            dict: The extracted payload information including:
                {
                    "seq_num": int,
                    "device_id": int,
                    "auth_tag": int,
                    "payload_bytes": ndarray,
                    "num_corrected_errors": int
                }
        """
        
        symbols = np.array(demodulated_symbols, dtype=np.uint8)
        total_symbols = self.total_symbols(payload_length)

        if total_symbols < 0:
            return None

        # rs err correction
        num_err_control_symbols = total_symbols - payload_length
        corrected_symbols, errata_pos_len = self._reed_solomon_correct_symbols(symbols, num_err_control_symbols)

        # convert what's left to a binary string
        binary_string = "".join(f"{symbol:06b}" for symbol in corrected_symbols)

        # payload protocol version: first 2 bits
        payload_protocol_version = int(binary_string[0:2], 2)

        # seq num: next 10 bits
        seq_num = int(binary_string[2:12], 2)

        # dynamic netID: next 32 bits
        device_id = int(binary_string[12:44], 2)

        # auth tag: next 32 bits
        auth_tag = int(binary_string[44:76], 2)

        # payload: 6 * payload_length bits
        payload = binary_string[76: (76 + 6 * payload_length)]

        # strip padding bits. The payload contains 1 followed by 0s until the nearest
        # packet length.
        payload = payload.rstrip("0")[:-1]

        payload_bytes = [int(payload[i : i + 8], 2) for i in range(0, len(payload), 8)]
        payload_bytes = np.array(payload_bytes, dtype=np.uint8)

        return {
            "payload_protocol_version": payload_protocol_version,
            "seq_num": seq_num,
            "device_id": device_id,
            "auth_tag": auth_tag,
            "payload": payload_bytes.tobytes().hex(),
            "symbols_corrected": (
                errata_pos_len if errata_pos_len >= 0 else "Packet uncorrectable"
            )
        }
