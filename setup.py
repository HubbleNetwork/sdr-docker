from setuptools import setup, find_packages

import os
import re


def get_version():
    version_file = os.path.join(os.path.dirname(__file__), "src", "pluto_sdr", "__init__.py")
    with open(version_file, "r") as f:
        content = f.read()

    version_match = re.search(r"^__version__ = ['\"]([^'\"]*)['\"]", content, re.M)
    if version_match:
        return version_match.group(1)

    raise RuntimeError("Unable to find version string.")


setup(
    name="pluto-sdr-docker",
    version=get_version(),
    description="Multi-SDR streaming spectrogram + packet decoder (PlutoSDR, bladeRF, …)",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.10",
    package_data={
        "stream_web": ["templates/*.html", "static/*.css"],
    },
    install_requires=[
        "flask",
        "numpy>=1.26,<2",
        "scipy>=1.13",
        "reedsolo",
        "matplotlib",
        "opencv-python-headless",
        "Pillow",
    ],
    # ── System-level dependencies (not pip-installable) ──────────────
    #
    # These must be installed via the system package manager (apt, brew)
    # or built from source BEFORE running `pip install -e .`.
    # Use `python3 -m venv --system-site-packages .venv` so the venv
    # can see system-installed packages.
    #
    # REQUIRED:
    #   gnuradio >= 3.9    GNU Radio with gr-soapy (the unified SDR backend)
    #                      • macOS:  brew install gnuradio
    #                      • Linux:  apt install gnuradio
    #
    #   soapysdr           SoapySDR abstraction library
    #                      • macOS:  installed as a gnuradio dependency
    #                      • Linux:  apt install libsoapysdr-dev python3-soapysdr
    #
    # BUILD TOOLS (needed to compile SoapySDR device modules):
    #   cmake, g++         • Linux: apt install cmake g++
    #                      • macOS: brew install cmake (g++ via Xcode CLT)
    #
    # PER-SDR (install the one(s) you need):
    #   SoapyPlutoSDR      PlutoSDR support (requires libiio)
    #                      • Build from source: github.com/pothosware/SoapyPlutoSDR
    #                      • macOS: libiio must also be built from source (not in
    #                        Homebrew); see README for full instructions
    #                      • Linux: apt install libiio-dev libiio-utils,
    #                        then build SoapyPlutoSDR
    #
    #   SoapyBladeRF       bladeRF 2.0 Micro A4 support (requires libbladerf)
    #                      • Build from source: github.com/pothosware/SoapyBladeRF
    #                      • macOS: brew install libbladerf, then build SoapyBladeRF
    #                      • Linux: apt install libbladerf-dev, then build SoapyBladeRF
    #
    # NUMPY CONSTRAINT: numpy must be < 2.0 because the GNU Radio packages
    # (both apt and Homebrew) are compiled against NumPy 1.x ABI. NumPy 2.x
    # will cause `gnuradio` imports to fail with `_ARRAY_API not found`.
    #
    # See README.md for detailed step-by-step installation instructions.
)
