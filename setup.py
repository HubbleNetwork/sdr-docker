from setuptools import setup, find_packages

import os
import re


def get_version():
    version_file = os.path.join(os.path.dirname(__file__), "src", "__init__.py")
    with open(version_file, "r") as f:
        content = f.read()

    version_match = re.search(r"^__version__ = ['\"]([^'\"]*)['\"]", content, re.M)
    if version_match:
        return version_match.group(1)

    raise RuntimeError("Unable to find version string.")


setup(
    name="pluto-sdr-docker",
    version=get_version(),
    description="Pluto SDR Docker",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "flask",
    ]
)
