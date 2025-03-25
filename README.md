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
