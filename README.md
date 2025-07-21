# pluto-sdr-docker

Docker container for working with the PlutoSDR. With the docker container running, you can use the PlutoSDR to transmit tones and hubble packets, as well as receiving and recoding packets. Examples for transmitting and receiving are found in the `hast_test_sw` repository.

## Setup

### Install docker

Official instructions [are located here.](https://docs.docker.com/engine/install/)

Here's how you can quickly install using the official convenience script:

```shell
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh ./get-docker.sh
```

Verify the installation:

```shell
sudo docker run hello-world
# Hello from Docker!
# This message shows that your installation appears to be working correctly.
# ...
```

### Add user to docker group

```shell
sudo groupadd docker
sudo usermod -aG docker $USER
```

Logout and log back in and verify that your user is added to the docker group.
If you have issues here, you might need a full reboot.

```shell
groups | grep docker
# ... docker ...
```

Now you should be able to run docker containers without sudo:

```shell
docker run hello-world
```

### Add SSH key to SSH agent

Building the container requires cloning various Hubble git repos during the build process, so the container will need SSH authorization. Add your SSH key to ssh-agent (example using a key named `id_ed25519`):

```shell
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
```

### Clone and build docker container

The tx files are stored using git-lfs, so you'll need to install git-lfs before cloning the repo. `sudo apt install git-lfs` or `brew install git-lfs` should work.

```shell
git clone git@github.com:HubbleNetwork/pluto-sdr-docker.git
cd pluto-sdr-docker/
git lfs pull
docker build -t pluto_container --ssh default .
```

## Run the container

### Run the container in the background

```shell
docker run -d -p 5000:5000 pluto_container
```

### Run the container in a terminal window

```shell
docker run -it -p 5000:5000 pluto_container
```

## Stop the container

If you need to stop the docker container, get the container ID and use `docker kill`.

```shell
docker ps
# CONTAINER ID   IMAGE             ...
# a2bbd6636d3e   pluto_container   ...
docker kill a2bbd6636d3e
# a2bbd6636d3e
```

Or with one line (be careful if you have multiple containers running):

```shell
docker kill $(docker ps -q)
```

## Information about source files

The pluto can transmit packets using files saved in the source_files/ directory. The files are generated using Matlab, which saves them as `.out` files. To add additional source files, you can just place `.out` files in the source_files/ directory and rebuild the container, or compress them into a .tar.gz archive and place them in the source_files/ directory. The archive will be extracted when the container is built.

The following source files are currently included in the container:

### Preamble-only transmissions saved in preambles.tar.gz

- only_preambles_10_per_1sec.out
- only_preambles_11_per_1sec.out
- only_preambles_12_per_1sec.out
- only_preambles_13_per_1sec.out
- only_preambles_1_per_1sec.out
- only_preambles_2_per_1sec.out
- only_preambles_3_per_1sec.out
- only_preambles_4_per_1sec.out
- only_preambles_5_per_1sec.out
- only_preambles_6_per_1sec.out
- only_preambles_7_per_1sec.out
- only_preambles_8_per_1sec.out
- only_preambles_9_per_1sec.out
- only_preambles_10_per_1sec_short_file.out
- only_preambles_11_per_1sec_short_file.out
- only_preambles_12_per_1sec_short_file.out
- only_preambles_13_per_1sec_short_file.out
- only_preambles_1_per_1sec_short_file.out
- only_preambles_2_per_1sec_short_file.out
- only_preambles_3_per_1sec_short_file.out
- only_preambles_4_per_1sec_short_file.out
- only_preambles_5_per_1sec_short_file.out
- only_preambles_6_per_1sec_short_file.out
- only_preambles_7_per_1sec_short_file.out
- only_preambles_8_per_1sec_short_file.out
- only_preambles_9_per_1sec_short_file.out
- tx_hubble_pkts_preamble_repeat.out

### Hubble packet transmissions with Nordic frequency step (373 Hz) saved in nordic.tar.gz

- tx_hubble_pkts_nordic_24symbols_seq_num_1pkt_per_sec_extra_100000_preambles.out
- tx_hubble_pkts_nordic_24symbols_seq_num_1pkt_per_sec_extra_10000_preambles.out
- SEA_hubble_pkts_nordic_24symbols_seq_num_1pkt_per_1s.out
- fixed_SEA_hubble_pkts_nordic_24symbols_seq_num_1pkt_per_1s.out (same as above, but with the sequence number bits in the correct place. Both kept for any tests using the old one.)
- tx_hubble_pkts_nordic_24symbols_seq_num_1pkt_per_sec_extra_1000_preambles.out
- tx_hubble_pkts_nordic_24symbols_1pkt_per_sec_doppler.out
- tx_hubble_pkts_nordic_24symbols_seq_num_1pkt_per_sec.out
- tx_hubble_pkts_nordic_24symbols.out
- tx_hubble_pkts_nordic_24symbols_seq_num_1pkt_per_sec_extra_100_preambles.out

### Hubble packet transmissions with older (TI) frequency step saved in old.tar.gz

- tx_hubble_pkts_concat_24_26_30_32_36_38_42_44symbols.out
- tx_hubble_pkts_24symbols_1pkt_per_sec.out
- tx_two_tones_always_ON.out
- tx_hubble_pkts_44symbols.out
- tx_hubble_pkts_42symbols.out
- tx_hubble_pkts_38symbols.out
- tx_hubble_pkts_36symbols.out
- tx_hubble_pkts_32symbols.out
- tx_hubble_pkts_30symbols.out
- tx_hubble_pkts_26symbols.out
- tx_hubble_pkts_24symbols.out
- tx_hubble_pkts_24symbols_rand_1.out
- tx_hubble_pkts_24symbols_rand_2.out
- tx_hubble_pkts_24symbols_rand_3.out
- tx_hubble_pkts_8symbols.out
- tx_hubble_pkts_8symbols_reverse.out
- tx_hubble_two_pkts_200KHz_16dB_offset.out
- tx_hubble_two_pkts_200KHz_offset.out
- tx_two_tones_8msON_1.6msOFF.out
