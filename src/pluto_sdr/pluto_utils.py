import threading
from typing import List, Dict

import numpy as np
import zmq

from datetime import datetime

from sim_decode.receiver.fast_decoder import FastDecoder

class PlutoUtils:
    DEFAULT_ZMQ_SOCKET = "tcp://127.0.0.1:5557"

    def __init__(self):
        self._stream_thread: threading.Thread = None
        self._stop_event: threading.Event = threading.Event()
        self._packets: List[Dict] = []
        self._lock: threading.Lock = threading.Lock()
        self._is_symbol_timing_debug: bool = False
        self._devices: Dict = {}
    
    def decode_packets(self, data, frequency_step=373):
        """
        Decode a window of samples using FastDecoder.

        Args:
            data (list[complex<float>]) - array of complex64 samples
            frequency_step (int)        - frequency step in Hz

        Returns:
            packets (list[dict])    - list of packets
            errors (list or None)   - error message if decoding failed
        """
        decoder = FastDecoder(data, frequency_step)
        preambles = decoder.find_all_preambles()
        valid = preambles != []
        if not valid:
            return [], ["Preamble not found"]

        packets = []
        errors = []
        for preamble in preambles:
            # Demodulate symbols
            demodulated_symbols, hopping_seq, payload_len, timing_info = decoder.demodulate_symbols(preamble, self._is_symbol_timing_debug)
            if demodulated_symbols is None:
                errors.append(f"Unable to demodulate symbols")
                continue
            
            # extract payload
            packet = decoder.extract_payload(demodulated_symbols, payload_len)
            if packet is None:
                errors.append(f"Invalid payload len, payload could not be decoded")
                continue
            
            # Get 1st channel for compare
            channel = hopping_seq[0]
            
            # Get Time
            cur_time = datetime.now()

            # Check if Channel has changed, if so, update time
            device_id = packet["device_id"]
            dev = self._devices.get(device_id, None)
            if dev is None:
                self._devices[device_id] = {"channel": channel, "time": cur_time}
            elif dev["channel"] != channel:
                dev["channel"] = channel
                dev["time"] = cur_time

            # Good packet
            packet["hopping_sequence"] = hopping_seq
            packet["last_channel_change_time"] = self._devices[device_id]["time"]

            if self._is_symbol_timing_debug:
                packet["symbol_timing_info"] = timing_info if timing_info is not None else "Unavailable"

            packets.append(packet)

        # return either the list of packets, or errors if none were valid
        if not packets:
            return [], errors

        return packets, None

    def set_symbol_timing_debug(self, enabled: bool):
        """
        Enable or disable symbol timing debug info in decoded packets.
        """
        self._is_symbol_timing_debug = enabled

    def start_stream_decode(self, socket_str=DEFAULT_ZMQ_SOCKET, window_size=1, frequency_step=373) -> bool:
        """
        Start continuous RX stream (Pluto -> ZMQ) and a
        background consumer that decodes packets with FastDecoder.
        """
        if window_size <= 0:
            raise ValueError("window_size must be > 0")

        # already running
        if self._stream_thread is not None and self._stream_thread.is_alive():
            return False

        self._stop_event.clear()
        self._stream_thread = threading.Thread(
            target=self._stream_decode_loop,
            args=(socket_str, window_size, frequency_step),
            daemon=True,
        )
        self._stream_thread.start()
        return True

    def stop_stream_decode(self) -> bool:
        """
        Stop the background decode thread.
        Returns True if a thread was stopped, False if there was
        nothing to stop.
        """
        # nothing to stop
        if self._stream_thread is None:
            return False

        self._stop_event.set()
        self._stream_thread.join(timeout=1.0)
        self._stream_thread = None

        with self._lock:
            self._packets.clear()

        return True

    def _stream_decode_loop(self, socket_str: str, window_size: int, frequency_step: int):
        """
        Background thread:
        - pulls IQ chunks from ZMQ
        - consume a window of `window_size = sample_rate * duration` samples
        - runs FastDecoder on that window
        - appends decoded packets into self._packets
        """
        context = zmq.Context.instance()
        results_receiver = context.socket(zmq.PULL)
        results_receiver.connect(socket_str)

        buf = np.zeros(0, dtype=np.complex64)

        try:
            while not self._stop_event.is_set():
                # blocking receive of one ZMQ message (a chunk of complex64 samples)
                try:
                    raw = results_receiver.recv()
                except zmq.ZMQError:
                    break

                chunk = np.frombuffer(raw, dtype=np.complex64)
                if chunk.size == 0:
                    continue

                # append to buffer
                buf = np.concatenate([buf, chunk])
                if buf.size < window_size:
                    continue

                # keep only the most recent window_s worth of samples
                while buf.size >= window_size:
                    # decode the window and drop it from the buffer
                    data = buf[:window_size]
                    buf = buf[window_size:]

                    # decode this window
                    packets, err = self.decode_packets(data, frequency_step)

                    if err is not None:
                        continue

                    # store decoded packets
                    with self._lock:
                        self._packets.extend(packets)
        
        finally:
            results_receiver.close()
    
    def get_packets(self):
        """
        Return all packets decoded so far and clear the internal list.
        """
        with self._lock:
            pkts = list(self._packets)
            self._packets.clear()
        return pkts
