import os
import sys
import signal
import time
import pmt
import gnuradio
from gnuradio import gr
from gnuradio import iio
from gnuradio import analog
from gnuradio import blocks


class pluto_tx(gr.top_block):
    def __init__(
        self,
        name: str,
        center_freq: float,
        sample_rate: int,
        attenuation: int,
        bandwidth: int,
    ):
        super().__init__(name, catch_exceptions=True)

        self.samp_rate = sample_rate
        self.fc = center_freq
        self.attn = attenuation
        self.bw = bandwidth

        self.iio_pluto_sink_0 = iio.fmcomms2_sink_fc32(
            "192.168.2.1" if "192.168.2.1" else iio.get_pluto_uri(),
            [True, True],
            32768,
            False,
        )
        self.iio_pluto_sink_0.set_len_tag_key("")
        self.iio_pluto_sink_0.set_bandwidth(self.bw)
        self.iio_pluto_sink_0.set_frequency(self.fc)
        self.iio_pluto_sink_0.set_samplerate(self.samp_rate)
        self.iio_pluto_sink_0.set_attenuation(0, self.attn)
        self.iio_pluto_sink_0.set_filter_params("Auto", "", 0, 0)

    @property
    def sample_rate(self):
        return self.samp_rate

    @sample_rate.setter
    def sample_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.iio_pluto_sink_0.set_samplerate(self.samp_rate)

    @property
    def center_freq(self):
        return self.fc

    @center_freq.setter
    def center_freq(self, fc):
        self.fc = fc
        self.iio_pluto_sink_0.set_frequency(self.fc)

    @property
    def attenuation(self):
        return self.attn

    @attenuation.setter
    def attenuation(self, attn):
        self.attn = attn
        self.iio_pluto_sink_0.set_attenuation(0, self.attn)

    def start(self, dur: float = 1, main=True):
        """Start transmitting

        Args:
            dur (float, optional): Duration to transmit for. Defaults to 1.
            main (bool, optional): Flag for running in main thread, where SIGINT/SIGTERM will end the transmission early. Defaults to True.
        """
        if main:

            def sig_handler(sig=None, frame=None):
                self.stop()
                self.wait()
                sys.exit(0)

            signal.signal(signal.SIGINT, sig_handler)
            signal.signal(signal.SIGTERM, sig_handler)
        print("Starting transmission")
        super().start()
        time.sleep(dur)
        print("Finished transmission")
        self.stop()
        self.wait()


class pluto_tx_tone(pluto_tx):
    def __init__(
        self,
        name: str = "Single tone tx",
        center_freq: float = 2483100000,
        sample_rate: int = 781250,
        attenuation: int = 0,
        bandwidth: int = 5000000,
    ):
        super().__init__(name, center_freq, sample_rate, attenuation, bandwidth)

        self.analog_const_source_x_0 = analog.sig_source_c(
            0, analog.GR_CONST_WAVE, 0, 1, 0
        )
        self.connect((self.analog_const_source_x_0, 0), (self.iio_pluto_sink_0, 0))


class plut_tx_pkt(pluto_tx):
    def __init__(
        self,
        name: str = "Hubble packet tx",
        center_freq: float = 2483100000,
        sample_rate: int = 781250,
        attenuation: int = 0,
        bandwidth: int = 20000000,
        file_name: str = "",
        multiple_packets: bool = False,
    ):
        super().__init__(name, center_freq, sample_rate, attenuation, bandwidth)

        self.blocks_file_source_0 = blocks.file_source(
            gr.sizeof_gr_complex * 1, file_name, multiple_packets, 0, 0
        )

        self.blocks_file_source_0.set_begin_tag(pmt.PMT_NIL)
        self.connect((self.blocks_file_source_0, 0), (self.iio_pluto_sink_0, 0))
