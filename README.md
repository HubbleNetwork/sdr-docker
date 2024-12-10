# pluto-sdr-docker

Docker container for working with the PlutoSDR

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

### Clone and build docker container

```shell
git clone git@github.com:HubbleNetwork/pluto-sdr-docker.git
cd pluto-sdr-docker/
docker build -t pluto_container .
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
