# pluto-sdr-docker

Docker container for working with the PlutoSDR

```shell
docker build -t pluto_container .
docker run -it pluto_container
python3 test_tx.py -time 20 -attn 20 -freq 2.48316e9
```
