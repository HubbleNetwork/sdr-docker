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


class PlutoTX(gr.top_block):
    def __init__(
        self,
        center_freq: float = 2483100000,
        sample_rate: int = 781250,
        attenuation: int = 0,
        bandwidth: int = 20000000,
    ):
        super().__init__("Pluto Tx", catch_exceptions=True)

        self.bw = bandwidth

        self.iio_pluto_sink_0 = iio.fmcomms2_sink_fc32(
            "192.168.2.1" if "192.168.2.1" else iio.get_pluto_uri(),
            [True, True],
            32768,
            False,
        )
        self.iio_pluto_sink_0.set_len_tag_key("")
        self.iio_pluto_sink_0.set_bandwidth(self.bw)
        self.center_freq = center_freq
        self.sample_rate = sample_rate
        self.attenuation = attenuation
        self.iio_pluto_sink_0.set_filter_params("Auto", "", 0, 0)
        self.connected = None

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

    def tone_mode(self):
        if self.connected:
            self.disconnect_all()
        self.analog_const_source_x_0 = analog.sig_source_c(
            0, analog.GR_CONST_WAVE, 0, 1, 0
        )
        self.connect((self.analog_const_source_x_0, 0), (self.iio_pluto_sink_0, 0))
        self.connected = True

    def packet_mode(self, file_name, multiple_packets):
        if self.connected:
            self.disconnect_all()
        self.blocks_file_source_0 = blocks.file_source(
            gr.sizeof_gr_complex * 1, file_name, multiple_packets, 0, 0
        )

        self.blocks_file_source_0.set_begin_tag(pmt.PMT_NIL)
        self.connect((self.blocks_file_source_0, 0), (self.iio_pluto_sink_0, 0))
        self.connected = True

    def start(self):
        super().start()

    def stop(self):
        super().stop()
        self.wait()


if __name__ == "__main__":
    tb = PlutoTX()
    tb.tone_mode()
    tb.start()
    time.sleep(5)
    tb.stop()
