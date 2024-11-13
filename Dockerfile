# Use Ubuntu 20.04 as the base image
FROM ubuntu:20.04

# Install prerequisites for adding PPAs and other setup
RUN apt-get update && \
    apt-get install -y software-properties-common wget

# Add the GNU Radio PPA repository
RUN add-apt-repository -y ppa:gnuradio/gnuradio-releases && \
    apt-get update

# Install GNU Radio and dependencies
RUN apt-get install -y gnuradio gnuradio-dev

# Download and install libiio
RUN wget https://github.com/analogdevicesinc/libiio/releases/download/v0.25/libiio-0.25.gb6028fd-Linux-Ubuntu-20.04.deb && \
    apt install -y ./libiio-0.25.gb6028fd-Linux-Ubuntu-20.04.deb && \
    rm libiio-0.25.gb6028fd-Linux-Ubuntu-20.04.deb

# Install the Python dependencies
RUN apt-get install -y python3-pip 

# without these lines, you will get this error:
# Uvmcircbuf_prefs::get :info: /root/.gnuradio/prefs/vmcircbuf_default_factory failed to open: bad true, fail true, eof true
RUN mkdir -p /root/.gnuradio/prefs && \
    echo "vmcircbuf_default_factory=shmem" > /root/.gnuradio/prefs/vmcircbuf_default_factory
ENV HOME=/root

COPY setup.py /app/
COPY flask_api.py /app/
COPY src/ /app/src/ 
COPY source_files/ /app/source_files/

# Set up a working directory for your PlutoSDR scripts
WORKDIR /app

RUN pip3 install .

EXPOSE 5000
CMD ["python3", "flask_api.py"]