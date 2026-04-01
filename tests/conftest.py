import os

# Must be set before any stream_web imports so config.SDR_TYPE == "mock".
os.environ["SDR_TYPE"] = "mock"
