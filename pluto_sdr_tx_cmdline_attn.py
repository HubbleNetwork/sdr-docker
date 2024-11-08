#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Not titled yet
# Author: vamsitalla
# GNU Radio version: 3.10.1.1

import os
from gnuradio import analog
from gnuradio import blocks
import pmt
from gnuradio import gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import sys
import signal
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import iio
import argparse
import time

class pluto_sdr_tx_tone(gr.top_block):

    def __init__(self):
        gr.top_block.__init__(self, "Single tone tx", catch_exceptions=True)

        ##################################################
        # Variables
        ##################################################
        self.samp_rate = samp_rate = 781250
        self.fc = fc = 2483100000
        self.attn = attn = 0

        ##################################################
        # Blocks
        ##################################################
        self.iio_pluto_sink_0 = iio.fmcomms2_sink_fc32('192.168.2.1' if '192.168.2.1' else iio.get_pluto_uri(), [True, True], 32768, False)
        self.iio_pluto_sink_0.set_len_tag_key('')
        self.iio_pluto_sink_0.set_bandwidth(5000000)
        self.iio_pluto_sink_0.set_frequency(fc)
        self.iio_pluto_sink_0.set_samplerate(samp_rate)
        self.iio_pluto_sink_0.set_attenuation(0, attn)
        self.iio_pluto_sink_0.set_filter_params('Auto', '', 0, 0)
        self.analog_const_source_x_0 = analog.sig_source_c(0, analog.GR_CONST_WAVE, 0, 1, 0)

        ##################################################
        # Connections
        ##################################################
        self.connect((self.analog_const_source_x_0, 0), (self.iio_pluto_sink_0, 0))


    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.iio_pluto_sink_0.set_samplerate(self.samp_rate)

    def get_fc(self):
        return self.fc

    def set_fc(self, fc):
        self.fc = fc
        self.iio_pluto_sink_0.set_frequency(self.fc)

    def get_attn(self):
        return self.attn

    def set_attn(self, attn):
        self.attn = attn
        self.iio_pluto_sink_0.set_attenuation(0, self.attn)



class pluto_sdr_pkt_tx(gr.top_block):

    def __init__(self):
        gr.top_block.__init__(self, "Hubble packet tx", catch_exceptions=True)

        ##################################################
        # Variables
        ##################################################
        self.samp_rate = samp_rate = 781250
        self.fc = fc = 2483100000
        self.attn = attn = 0

        ##################################################
        # Blocks
        ##################################################
        self.iio_pluto_sink_0 = iio.fmcomms2_sink_fc32('192.168.2.1' if '192.168.2.1' else iio.get_pluto_uri(), [True, True], 32768, False)
        self.iio_pluto_sink_0.set_len_tag_key('')
        self.iio_pluto_sink_0.set_bandwidth(20000000)
        self.iio_pluto_sink_0.set_frequency(fc)
        self.iio_pluto_sink_0.set_samplerate(samp_rate)
        self.iio_pluto_sink_0.set_attenuation(0, attn)
        self.iio_pluto_sink_0.set_filter_params('Auto', '', 0, 0)




    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.iio_pluto_sink_0.set_samplerate(self.samp_rate)

    def get_fc(self):
        return self.fc

    def set_fc(self, fc):
        self.fc = fc
        self.iio_pluto_sink_0.set_frequency(self.fc)

    def get_attn(self):
        return self.attn

    def set_attn(self, attn):
        self.attn = attn
        self.iio_pluto_sink_0.set_attenuation(0, self.attn)

    def set_filesource(self, num_symbols: int = 24,
                        file_name: str = '', 
                        single_pkt: bool = False):
        
        file_dir = os.path.dirname(os.path.abspath(sys.argv[0]))+ '/source_files/'
        
        if file_name == '':
            file_name = f'tx_hubble_pkts_{num_symbols}symbols.out'

        self.blocks_file_source_0 = blocks.file_source(gr.sizeof_gr_complex*1,
            file_dir+file_name, single_pkt, 0, 0)
        
        self.blocks_file_source_0.set_begin_tag(pmt.PMT_NIL)

        ##################################################
        # Connections
        ##################################################
        self.connect((self.blocks_file_source_0, 0), (self.iio_pluto_sink_0, 0))

def pluto_sdr_setup_tx(args: argparse = None):

    assert args.sig_type in ["tone", "hubble_pkt"], "Invalid signal type"

    if args.file_name != '':
        args.file_path = os.path.join('source_files', args.file_name)

    if args.sig_type == "tone":
        tb = pluto_sdr_tx_tone()

        tb.set_attn(args.attn)
        tb.set_fc(args.freq)
        tb.set_samp_rate(args.sample_rate)

    elif args.sig_type == "hubble_pkt":
        tb = pluto_sdr_pkt_tx()

        tb.set_attn(args.attn)
        tb.set_fc(args.freq)
        tb.set_samp_rate(args.sample_rate)
        tb.set_filesource(args.num_symbols, args.file_name, args.single_pkt)

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    return tb

def main(top_block_cls=pluto_sdr_pkt_tx, options=None):

    # Initialize parser
    parser = argparse.ArgumentParser()

    # Adding optional argument
    parser.add_argument("-attn", "--attn", type=int, default = 0, help = "Attenuation (in dB)")
    parser.add_argument("-freq", "--freq", type=float, default = 2.4831e9, help = "Center Frequency (in Hz)")
    parser.add_argument("-sample_rate", "--sample_rate", type=int, default = 781250, help = "Sampling Rate")

    parser.add_argument("-single_pkt", "--single_pkt", action='store_false', help = "To transmit a single packet only else continuous")
    parser.add_argument("-time", "--time", type=float, default = 1, help = "Run time in seconds")
    parser.add_argument("-num_symbols", "--num_symbols", type=str, default = '24', help = "number of symbols")
    parser.add_argument("-sig_type", "--sig_type", type=str, default = 'hubble_pkt', help = "Signal type: hubble_pkt or tone")
    parser.add_argument("-file_name", "--file_name", type=str, default = '', help = "File name of the source file to be fed directly")

    # Read arguments from command line
    args = parser.parse_args()

    tb = pluto_sdr_setup_tx(args)

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    print("Starting transmission")
    tb.start()

    t_start = time.process_time()

    while (time.process_time()-t_start) < args.time:
        continue

    tb.stop()
    tb.wait()
    print("Stopped transmission after {} seconds".format(args.time))

if __name__ == '__main__':
    main()
