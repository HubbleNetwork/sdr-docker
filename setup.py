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
    description="PlutoSDR streaming spectrogram + packet decoder",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    package_data={
        "stream_web": ["templates/*.html", "static/*.css"],
    },
    install_requires=[
        "flask",
        "numpy==1.26.4",
        "scipy==1.13.1",
        "reedsolo",
        "matplotlib",
        "opencv-python-headless",
        "Pillow",
        "pyadi-iio",
    ],
)
