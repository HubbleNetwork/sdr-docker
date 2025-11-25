import flask
import os
from functools import wraps
import numpy as np
from io import BytesIO

from pluto_sdr import PlutoManager

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
    except Exception as e:
        return flask.jsonify({"error": f"Failed to initialize Pluto, error: {str(e)}"}), 500
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
        if not os.path.exists(file_name):
            return flask.jsonify({"error": f"File not found: {file_name}"}), 400
        
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

    if duration is None or duration <= 0:
        return flask.jsonify({"error": "duration must be > 0"}), 400

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
    frequency_step = flask.request.args.get("frequency_step", default=373, type=int)
    decode_interval = flask.request.args.get("interval", default=5, type=int)

    data = pluto_manager.pluto.capture_for_duration(decode_interval)

    packets, err = pluto_manager.pluto_utils.decode_packets(data, frequency_step)
    if err is not None:
        return flask.jsonify({"error": err}), 400
    
    return flask.jsonify({"packets": packets}), 200


@app.route("/stream", methods=["GET"])
@ensure_pluto_initialized
@ensure_rx_mode
def stream():
    mode = flask.request.args.get("mode", default="get_packets")

    if mode == "start":
        frequency_step = flask.request.args.get("frequency_step", default=373, type=int)

        # let have the window period be 5s
        window_size = int(5 * pluto_manager.pluto.sample_rate)
        
        try:
            pluto_manager.pluto.start_stream()
        except Exception as e:
            return flask.jsonify({"error": str(e)}), 400

        try:
            ok = pluto_manager.pluto_utils.start_stream_decode(window_size=window_size, frequency_step=frequency_step)
        except Exception as e:
            # stop the stream when decode fails to start
            pluto_manager.pluto.stop_stream()
            return flask.jsonify({"error": str(e)}), 400

        if not ok:
            return flask.jsonify({"message": "Already running"}), 200

        return flask.jsonify({"message": "Stream decode started"}), 200

    elif mode == "stop":
        ok = pluto_manager.pluto_utils.stop_stream_decode()

        if not ok:
            # No decode thread was running
            return flask.jsonify({"message": "No active decode stream"}), 200
        
        # if decode thread was stopped, also stop the pluto stream
        try:
            pluto_manager.pluto.stop_stream()
        except Exception as e:
            return flask.jsonify({"error": f"Failed to stop Pluto stream, error: {str(e)}"}), 400

        return flask.jsonify({"message": "Pluto stream stopped"}), 200

    elif mode == "get_packets":
        packets = pluto_manager.pluto_utils.get_packets()
        return flask.jsonify({"packets": packets}), 200

    else:
        return flask.jsonify({"error": f"Unknown mode: {mode}"}), 400