import os
import sys
import signal
import time
import pmt
import gnuradio
from gnuradio import gr
from gnuradio import iio
from gnuradio import blocks
from gnuradio import zeromq


class PlutoRX(gr.top_block):
    def __init__(
        self,
        center_freq: float = 2483000000,
        sample_rate: int = 781250,
        gain: int = 64,
    ):
        super().__init__("Pluto Rx", catch_exceptions=True)

        self.iio_pluto_source_0 = iio.fmcomms2_source_fc32(
            "192.168.2.1" if "192.168.2.1" else iio.get_pluto_uri(), [True, True], 32768
        )
        self.iio_pluto_source_0.set_len_tag_key("packet_len")
        self.center_freq = center_freq
        self.sample_rate = sample_rate
        self.gain = gain
        self.iio_pluto_source_0.set_quadrature(True)
        self.iio_pluto_source_0.set_rfdc(True)
        self.iio_pluto_source_0.set_bbdc(True)
        self.iio_pluto_source_0.set_filter_params("Auto", "", 0, 0)

    @property
    def sample_rate(self):
        return self.samp_rate

    @sample_rate.setter
    def sample_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.iio_pluto_source_0.set_samplerate(self.samp_rate)

    @property
    def center_freq(self):
        return self.fc

    @center_freq.setter
    def center_freq(self, fc):
        self.fc = fc
        self.iio_pluto_source_0.set_frequency(self.fc)

    @property
    def gain(self):
        return self.gain_val

    @gain.setter
    def gain(self, gain_val):
        self.gain_val = gain_val
        self.iio_pluto_source_0.set_gain_mode(0, "manual")
        self.iio_pluto_source_0.set_gain(0, self.gain_val)

    def start(self):
        self.vector_sink = blocks.vector_sink_c()
        self.connect((self.iio_pluto_source_0, 0), (self.vector_sink, 0))
        super().start()

    def stop(self):
        super().stop()
        self.wait()

    def capture_for_duration(self, duration):
        self.start()
        time.sleep(duration)
        self.stop()
        data = self.vector_sink.data()
        self.vector_sink.reset()
        self.disconnect((self.iio_pluto_source_0, 0), (self.vector_sink, 0))
        return data

    def start_stream(self, socket_str: str = "tcp://127.0.0.1:5557"):
        """
        Continuous streaming mode:
        Connect Pluto -> ZeroMQ PUSH sink and start the flowgraph.
        Runs until stop_stream() is called.
        """
        # create a ZMQ PUSH sink
        self.zmq_sink = zeromq.push_sink(
            gr.sizeof_gr_complex,
            1,
            socket_str,
            100,
            False,
            -1,
        )

        self.connect((self.iio_pluto_source_0, 0), (self.zmq_sink, 0))
        super().start()

    def stop_stream(self):
        super().stop()
        self.wait()
        if hasattr(self, "zmq_sink"):
            self.disconnect((self.iio_pluto_source_0, 0), (self.zmq_sink, 0))
            self.zmq_sink = None

    def __del__(self):
        self.stop()
        self.disconnect_all()


if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=10)
    args = parser.parse_args()

    tb = PlutoRX()
    tb.capture_for_duration("capture.bin", args.duration)
