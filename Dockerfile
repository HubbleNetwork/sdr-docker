FROM ubuntu:22.04

# Workaround for hash-sum-mismatch on ARM mirrors
RUN echo 'Acquire::http::Pipeline-Depth "0";' > /etc/apt/apt.conf.d/99fixmirror && \
    echo 'Acquire::http::No-Cache=True;' >> /etc/apt/apt.conf.d/99fixmirror && \
    echo 'Acquire::BrokenProxy=true;' >> /etc/apt/apt.conf.d/99fixmirror

# Install prerequisites
RUN rm -rf /var/lib/apt/lists/* && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        software-properties-common wget python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Add the GNU Radio PPA repository and install GNU Radio
RUN add-apt-repository -y ppa:gnuradio/gnuradio-releases && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --fix-missing gnuradio gnuradio-dev

# Download and install libiio
RUN wget https://github.com/analogdevicesinc/libiio/releases/download/v0.26/libiio-0.26.ga0eca0d-Linux-Ubuntu-22.04.deb && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y ./libiio-0.26.ga0eca0d-Linux-Ubuntu-22.04.deb && \
    rm libiio-0.26.ga0eca0d-Linux-Ubuntu-22.04.deb

# Without these lines the GNU Radio vmcircbuf backend fails to initialise
RUN mkdir -p /root/.gnuradio/prefs && \
    echo "vmcircbuf_default_factory=shmem" > /root/.gnuradio/prefs/vmcircbuf_default_factory
ENV HOME=/root

# Copy source code into the container
WORKDIR /app
COPY setup.py /app/
COPY run_stream.py /app/
COPY src/ /app/src/

# Install the python package
RUN python3 -m pip install --upgrade pip setuptools wheel
RUN python3 -m pip install --ignore-installed -e .

# Start the live spectrogram + decoder web server
EXPOSE 8050
CMD ["python3", "run_stream.py"]
