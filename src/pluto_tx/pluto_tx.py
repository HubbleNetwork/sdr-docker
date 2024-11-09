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

        ##################################################
        # Blocks
        ##################################################
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

    def start(self, dur: float = 1):
        def sig_handler(sig=None, frame=None):
            self.stop()
            self.wait()
            sys.exit(0)

        signal.signal(signal.SIGINT, sig_handler)
        signal.signal(signal.SIGTERM, sig_handler)
        super().start()
        time.sleep(dur)
        self.stop()
        self.wait()
        print(f"Finished transmission after {dur:.2f} seconds")


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

    def start(self, dur: float = 1):
        print(f"Starting tone transmission at {self.fc/1e6:.2f} MHz for {dur} seconds")
        super().start(dur)


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

        self.multiple_packets = multiple_packets

        self.blocks_file_source_0 = blocks.file_source(
            gr.sizeof_gr_complex * 1, file_name, multiple_packets, 0, 0
        )

        self.blocks_file_source_0.set_begin_tag(pmt.PMT_NIL)

        self.connect((self.blocks_file_source_0, 0), (self.iio_pluto_sink_0, 0))

    def start(self, dur: float = 1):
        if self.multiple_packets:
            print(f"Starting transmission of packets for {dur} seconds")
        else:
            print(f"Starting transmission of a single packet for {dur} seconds")
        super().start(dur)


if __name__ == "__main__":
    """
    python3 pluto_tx.py -time 20 -attn 20 -freq 2.48316e9
    """

    import argparse

    parser = argparse.ArgumentParser(description="Pluto SDR TX")
    parser.add_argument(
        "-attn", "--attn", type=int, default=0, help="Attenuation (in dB)"
    )
    parser.add_argument(
        "-freq", "--freq", type=float, default=2.4831e9, help="Center Frequency (in Hz)"
    )
    parser.add_argument(
        "-sample_rate", "--sample_rate", type=int, default=781250, help="Sampling Rate"
    )
    parser.add_argument(
        "-time", "--time", type=float, default=1, help="Run time in seconds"
    )
    parser.add_argument(
        "-single_pkt",
        "--single_pkt",
        action="store_false",
        help="To transmit a single packet only else continuous",
    )
    parser.add_argument(
        "-num_symbols", "--num_symbols", type=str, default="", help="number of symbols"
    )
    parser.add_argument(
        "-file_name",
        "--file_name",
        type=str,
        default="",
        help="File name of the source file in source_files/ directory",
    )

    args = parser.parse_args()

    file_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    file_dir = os.path.join(file_dir, "source_files")

    # 3 possible cases:
    # 1. transmit a tone: num_symbols and file_name = ""
    # 2. transmit a packet based on num_symbols: file_name = "" and num_symbols != ""
    # 3. transmit a packet based on file_name: file_name != ""

    if args.file_name != "":
        args.file_name = os.path.join(file_dir, args.file_name)
    elif args.num_symbols != "":
        args.file_name = os.path.join(
            file_dir, f"tx_hubble_pkts_{args.num_symbols}symbols.out"
        )

    # transmit a tone
    if args.file_name == "":
        pluto = pluto_tx_tone(
            attenuation=args.attn, center_freq=args.freq, sample_rate=args.sample_rate
        )
    # transmit packets
    else:
        if not os.path.exists(args.file_name):
            print(f"File {args.file_name} does not exist")
            sys.exit(1)

        pluto = plut_tx_pkt(
            attenuation=args.attn,
            center_freq=args.freq,
            sample_rate=args.sample_rate,
            file_name=args.file_name,
            multiple_packets=args.single_pkt,
        )

    pluto.start(args.time)
