FROM ubuntu:22.04

# Install prerequisites for adding PPAs and other setup
RUN apt update && \
    DEBIAN_FRONTEND=noninteractive apt install -y git ssh software-properties-common wget python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Add the GNU Radio PPA repository
RUN add-apt-repository -y ppa:gnuradio/gnuradio-releases && \
    apt update

# Install GNU Radio and dependencies
RUN DEBIAN_FRONTEND=noninteractive apt install -y gnuradio gnuradio-dev

# Download and install libiio
RUN wget https://github.com/analogdevicesinc/libiio/releases/download/v0.26/libiio-0.26.ga0eca0d-Linux-Ubuntu-22.04.deb && \
    DEBIAN_FRONTEND=noninteractive apt install -y ./libiio-0.26.ga0eca0d-Linux-Ubuntu-22.04.deb && \
    rm libiio-0.26.ga0eca0d-Linux-Ubuntu-22.04.deb

# without these lines, you will get this error:
# Uvmcircbuf_prefs::get :info: /root/.gnuradio/prefs/vmcircbuf_default_factory failed to open: bad true, fail true, eof true
RUN mkdir -p /root/.gnuradio/prefs && \
    echo "vmcircbuf_default_factory=shmem" > /root/.gnuradio/prefs/vmcircbuf_default_factory
ENV HOME=/root

# copy the source code to the container
WORKDIR /app
COPY setup.py /app/
COPY run_api.py /app/
COPY src/ /app/src/ 
COPY source_files/ /app/source_files/

# Extract all .tar.gz files into /app/source_files/ and remove the archives
RUN find /app/source_files -name "*.tar.gz" -exec tar -xzf {} -C /app/source_files/ \; && \
    find /app/source_files -name "*.tar.gz" -delete

# verify that ssh is working
RUN mkdir -p /root/.ssh && ssh-keyscan github.com >> /root/.ssh/known_hosts
RUN --mount=type=ssh ssh -T git@github.com || true

# install the python package
RUN pip3 install setuptools wheel
RUN pip3 install .

# install sim-decode
RUN --mount=type=ssh git clone --branch develop --depth 1 git@github.com:HubbleNetwork/sim-decode.git /app/sim-decode
RUN --mount=type=ssh pip3 install /app/sim-decode

# start the http server
EXPOSE 5000
CMD ["python3", "run_api.py"]