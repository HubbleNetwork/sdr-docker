from flask import Flask, request, jsonify, send_file
import pluto_sdr
import threading
import os
from functools import wraps
import hubble_decoder
import numpy as np

app = Flask(__name__)
capture_file = "/app/capture.npy"


class PlutoManager:
    def __init__(self):
        self.pluto = None

    def initialize(self, mode):

        if mode == "tx":
            if self.is_tx_mode():
                return
            if self.is_initialized():
                del self.pluto
            self.pluto = pluto_sdr.PlutoTX()
        elif mode == "rx":
            if self.is_rx_mode():
                return
            if self.is_initialized():
                del self.pluto
            self.pluto = pluto_sdr.PlutoRX()
        else:
            raise ValueError(f"Invalid mode: {mode}")

    def is_initialized(self):
        return self.pluto is not None

    def is_tx_mode(self):
        return isinstance(self.pluto, pluto_sdr.PlutoTX)

    def is_rx_mode(self):
        return isinstance(self.pluto, pluto_sdr.PlutoRX)


pluto_manager = PlutoManager()


def ensure_pluto_initialized(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not pluto_manager.is_initialized():
            return jsonify({"error": "Pluto not initialized"}), 400
        return f(*args, **kwargs)

    return wrapper


def ensure_tx_mode(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not pluto_manager.is_tx_mode():
            return jsonify({"error": "Pluto is not in TX mode"}), 400
        return f(*args, **kwargs)

    return wrapper


def ensure_rx_mode(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not pluto_manager.is_rx_mode():
            return jsonify({"error": "Pluto is not in RX mode"}), 400
        return f(*args, **kwargs)

    return wrapper


@app.route("/")
def home():
    return jsonify({"status": "Server is running"}), 200


@app.route("/mode", methods=["POST"])
def set_mode():
    mode = request.json.get("mode", "tx")
    try:
        pluto_manager.initialize(mode)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"mode": mode}), 200


@app.route("/sample_rate", methods=["GET", "POST"])
@ensure_pluto_initialized
def set_sample_rate():
    pluto = pluto_manager.pluto
    if request.method == "GET":
        return jsonify({"sample_rate": pluto.sample_rate}), 200
    else:
        sample_rate = request.json.get("sample_rate", 781250)
        pluto.sample_rate = sample_rate
        return jsonify({"sample_rate": pluto.sample_rate}), 200


@app.route("/freq", methods=["GET", "POST"])
@ensure_pluto_initialized
def set_freq():
    pluto = pluto_manager.pluto
    if request.method == "GET":
        return jsonify({"freq": pluto.center_freq}), 200
    else:
        freq = request.json.get("freq", 2.4831e9)
        pluto.center_freq = freq
        return jsonify({"freq": pluto.center_freq}), 200


@app.route("/attn", methods=["GET", "POST"])
@ensure_pluto_initialized
@ensure_tx_mode
def set_attn():
    pluto = pluto_manager.pluto
    if request.method == "GET":
        return jsonify({"attn": pluto.attenuation}), 200
    else:
        attn = request.json.get("attn", 0)
        pluto.attenuation = attn
        return jsonify({"attn": pluto.attenuation}), 200


@app.route("/tx", methods=["POST"])
@ensure_pluto_initialized
@ensure_tx_mode
def transmit():
    pluto = pluto_manager.pluto
    if request.is_json:
        file_name = request.json.get("file_name", "")
        num_symbols = request.json.get("num_symbols", "")
        single_pkt = request.json.get("single_pkt", False)
    else:
        file_name = ""
        num_symbols = ""
        single_pkt = False

    multiple_packets = not single_pkt
    file_dir = "/app/source_files"

    if file_name != "":
        file_name = os.path.join(file_dir, file_name)
    elif num_symbols != "":
        file_name = os.path.join(file_dir, f"tx_hubble_pkts_{num_symbols}symbols.out")

    if file_name == "":
        msg = "Transmitting tone"
        pluto.tone_mode()
    else:
        msg = "Transmitting packets"
        pluto.packet_mode(file_name, multiple_packets)

    pluto.start()
    return jsonify({"message": msg}), 200


@app.route("/stop", methods=["POST"])
@ensure_pluto_initialized
def stop():
    pluto_manager.pluto.stop()
    return jsonify({"message": "Pluto stopped"}), 200


@app.route("/gain", methods=["GET", "POST"])
@ensure_pluto_initialized
@ensure_rx_mode
def set_gain():
    pluto = pluto_manager.pluto
    if request.method == "GET":
        return jsonify({"gain": pluto.gain}), 200
    else:
        gain = request.json.get("gain", 0)
        pluto.gain = gain
        return jsonify({"gain": pluto.gain}), 200


@app.route("/rx", methods=["POST"])
@ensure_pluto_initialized
@ensure_rx_mode
def receive():
    if request.is_json:
        duration = request.json.get("duration", 2)
    else:
        duration = 2
    data = pluto_manager.pluto.capture_for_duration(duration)
    # save to file
    np.save(capture_file, data)
    return jsonify({"message": f"Captured for {duration} seconds"}), 200


@app.route("/transfer_file", methods=["GET"])
def transfer_file():
    if not os.path.exists(capture_file):
        return jsonify({"error": "File not found"}), 404

    return send_file(capture_file, as_attachment=True)


@app.route("/decode", methods=["GET"])
@ensure_pluto_initialized
@ensure_rx_mode
def decode_packets():
    data = pluto_manager.pluto.capture_for_duration(10)
    decoder = hubble_decoder.FastDecoder(data)
    valid, preamble_symbols, data_symbols = decoder.detect_and_validate_preamble()
    if not valid:
        return jsonify({"error": "Preamble not found"}), 400
    decoded_symbols = decoder.demodulate_symbols(preamble_symbols, data_symbols)
    device_id, payload = decoder.symbols_to_byte_fields(decoded_symbols)
    return jsonify({"device_id": device_id, "payload": payload.tobytes().hex()}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
