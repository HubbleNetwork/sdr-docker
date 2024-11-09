# Tx hubble pkts details
The source files are generated using Matlab and here are the details for the generated files:
- The number of symbols include all symbols including data, packet length and Error correcting bits

For details on the packet structure refer: https://hubblenetwork.atlassian.net/wiki/spaces/HN/pages/265519169/Test+Vector+Generation+for+FPGA+Validation

For details on packet generation refer: https://hubblenetwork.atlassian.net/wiki/spaces/~64016f952847866310fef902/pages/281313305/Transmit+packets+using+PlutoSDR

The data symbols are written with 5 ms gap at the begining of the file so if the same packet is repeated multiple times, there is a 5 ms delay between subsequent transmissions.

The payload details for the different symbol length source files are as follows:

tx_hubble_pkts_24symbols.out

[0     9    10    11    12    13    14    15    16     0    17    18    19    16    63    34    41    39     0    54    23    17    19    49]

tx_hubble_pkts_26symbols.out

[1     9    10    11    12    13    14    15    16     1    17    18    19    20    21     8    21
31     1    45     5    55    45    51    57    45]

tx_hubble_pkts_30symbols.out

[2     9    10    11    12    13    14    15    16     2    17    18    19    20    21    22    23
26     2     3    52    61    40    26    34    17    35    47    58    10]

tx_hubble_pkts_32symbols.out

[3     9    10    11    12    13    14    15    16     3    17    18    19    20    21    22    23
24     3    25    51    37    39    21    23    33     0    40    50    30    39     8]

tx_hubble_pkts_36symbols.out

[4     9    10    11    12    13    14    15    16     4    17    18    19    20    21    22    23
24     4    25    26    27    35    45    43    36    19     9    20    31     3    58    31    37
35    44]

tx_hubble_pkts_38symbols.out

[5     9    10    11    12    13    14    15    16     5    17    18    19    20    21    22    23
24     5    25    26    27    28    29     2    62    53    18    18    12    20    42    53    16
30    11    61    46]

tx_hubble_pkts_42symbols.out

[6     9    10    11    12    13    14    15    16     6    17    18    19    20    21    22    23
24     6    25    26    27    28    29    30    31    57    62    52    55    42    57    14    31
1    63    52    11    21    32    52    59]

tx_hubble_pkts_44symbols.out

[7     9    10    11    12    13    14    15    16     7    17    18    19    20    21    22    23    24     7    25    26    27    28    29    30    31    32    33    61    10    6    37    26    18    13    30    60    49     8    49    20    13    31    52]

tx_hubble_pkts_concat_24_26_30_32_36_38_42_44symbols.out

Concatenation of all above packets with a spacing of 10 ms.

