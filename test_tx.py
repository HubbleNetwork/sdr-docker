import os
import sys
import argparse
import pluto_tx

import argparse

parser = argparse.ArgumentParser(description="Pluto SDR TX")
parser.add_argument("-attn", "--attn", type=int, default=0, help="Attenuation (in dB)")
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

file_dir = "/app/source_files"

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
    tx_type = "tone"
    pluto = pluto_tx.pluto_tx_tone(
        attenuation=args.attn, center_freq=args.freq, sample_rate=args.sample_rate
    )
# transmit packets
else:
    tx_type = "packet"
    if not os.path.exists(args.file_name):
        print(f"File {args.file_name} does not exist")
        sys.exit(1)

    pluto = pluto_tx.plut_tx_pkt(
        attenuation=args.attn,
        center_freq=args.freq,
        sample_rate=args.sample_rate,
        file_name=args.file_name,
        multiple_packets=args.single_pkt,
    )

print(f"Transmitting {tx_type} for {args.time:.2f} seconds")
pluto.start(args.time)
