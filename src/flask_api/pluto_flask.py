import flask
import os
from functools import wraps
import numpy as np
from io import BytesIO

from pluto_sdr import PlutoManager
from sim_decode.receiver.fast_decoder import FastDecoder

app = flask.Flask(__name__)
pluto_manager = PlutoManager()


def ensure_pluto_initialized(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not pluto_manager.is_initialized():
            return flask.jsonify({"error": "Pluto not initialized"}), 400
        return f(*args, **kwargs)

    return wrapper


def ensure_tx_mode(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not pluto_manager.is_tx_mode():
            return flask.jsonify({"error": "Pluto is not in TX mode"}), 400
        return f(*args, **kwargs)

    return wrapper


def ensure_rx_mode(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not pluto_manager.is_rx_mode():
            return flask.jsonify({"error": "Pluto is not in RX mode"}), 400
        return f(*args, **kwargs)

    return wrapper


@app.route("/")
def home():
    return flask.jsonify({"status": "Server is running"}), 200


@app.route("/mode", methods=["POST"])
def set_mode():
    mode = flask.request.json.get("mode", "tx")
    try:
        pluto_manager.initialize(mode)
    except ValueError as e:
        return flask.jsonify({"error": str(e)}), 400
    return flask.jsonify({"mode": mode}), 200


@app.route("/sample_rate", methods=["GET", "POST"])
@ensure_pluto_initialized
def set_sample_rate():
    pluto = pluto_manager.pluto
    if flask.request.method == "GET":
        return flask.jsonify({"sample_rate": pluto.sample_rate}), 200
    else:
        sample_rate = flask.request.json.get("sample_rate", 781250)
        pluto.sample_rate = sample_rate
        return flask.jsonify({"sample_rate": pluto.sample_rate}), 200


@app.route("/freq", methods=["GET", "POST"])
@ensure_pluto_initialized
def set_freq():
    pluto = pluto_manager.pluto
    if flask.request.method == "GET":
        return flask.jsonify({"freq": pluto.center_freq}), 200
    else:
        freq = flask.request.json.get("freq", 2.4831e9)
        pluto.center_freq = freq
        return flask.jsonify({"freq": pluto.center_freq}), 200


@app.route("/attn", methods=["GET", "POST"])
@ensure_pluto_initialized
@ensure_tx_mode
def set_attn():
    pluto = pluto_manager.pluto
    if flask.request.method == "GET":
        return flask.jsonify({"attn": pluto.attenuation}), 200
    else:
        attn = flask.request.json.get("attn", 0)
        pluto.attenuation = attn
        return flask.jsonify({"attn": pluto.attenuation}), 200


@app.route("/tx", methods=["POST"])
@ensure_pluto_initialized
@ensure_tx_mode
def transmit():
    pluto = pluto_manager.pluto
    if flask.request.is_json:
        file_name = flask.request.json.get("file_name", "")
        num_symbols = flask.request.json.get("num_symbols", "")
        single_pkt = flask.request.json.get("single_pkt", False)
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
    return flask.jsonify({"message": msg}), 200


@app.route("/stop", methods=["POST"])
@ensure_pluto_initialized
def stop():
    pluto_manager.pluto.stop()
    return flask.jsonify({"message": "Pluto stopped"}), 200


@app.route("/gain", methods=["GET", "POST"])
@ensure_pluto_initialized
@ensure_rx_mode
def set_gain():
    pluto = pluto_manager.pluto
    if flask.request.method == "GET":
        return flask.jsonify({"gain": pluto.gain}), 200
    else:
        gain = flask.request.json.get("gain", 0)
        pluto.gain = gain
        return flask.jsonify({"gain": pluto.gain}), 200


@app.route("/rx", methods=["GET"])
@ensure_pluto_initialized
@ensure_rx_mode
def receive():
    duration = flask.request.args.get("duration", default=2.0, type=float)
    data = pluto_manager.pluto.capture_for_duration(duration)

    data_stream = BytesIO()
    np.save(data_stream, data)
    data_stream.seek(0)

    return flask.send_file(
        data_stream,
        as_attachment=True,
        download_name="capture.npy",
        mimetype="application/octet-stream",
    )


@app.route("/decode", methods=["GET"])
@ensure_pluto_initialized
@ensure_rx_mode
def decode_packets():
    data = pluto_manager.pluto.capture_for_duration(5)
    frequency_step = flask.request.args.get("frequency_step", default=373, type=int)
    decoder = FastDecoder(data, frequency_step)
    preambles = decoder.find_all_preambles()
    valid = preambles != []
    if not valid:
        return flask.jsonify({"error": "Preamble not found"}), 400
    packets = []
    for preamble in preambles:
        demodulated_symbols = decoder.demodulate_symbols(preamble)
        device_id, payload = decoder.extract_device_id_and_payload(
            demodulated_symbols
        )
        packets.append({"device_id": device_id, "payload": payload.tobytes().hex()})
    return flask.jsonify({"packets": packets}), 200
