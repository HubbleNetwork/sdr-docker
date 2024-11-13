from flask import Flask, request, jsonify
import pluto_tx
import threading
import os

app = Flask(__name__)
pluto = pluto_tx.PlutoTX()


@app.route("/sample_rate", methods=["GET", "POST"])
def set_sample_rate():
    if request.method == "GET":
        return jsonify({"sample_rate": pluto.sample_rate}), 200
    else:
        sample_rate = request.json.get("sample_rate", 781250)
        pluto.sample_rate = sample_rate
        return jsonify({"sample_rate": pluto.sample_rate}), 200


@app.route("/freq", methods=["GET", "POST"])
def set_freq():
    if request.method == "GET":
        return jsonify({"freq": pluto.center_freq}), 200
    else:
        freq = request.json.get("freq", 2.4831e9)
        pluto.center_freq = freq
        return jsonify({"freq": pluto.center_freq}), 200


@app.route("/attn", methods=["GET", "POST"])
def set_attn():
    if request.method == "GET":
        return jsonify({"attn": pluto.attenuation}), 200
    else:
        attn = request.json.get("attn", 0)
        pluto.attenuation = attn
        return jsonify({"attn": pluto.attenuation}), 200


@app.route("/tx", methods=["POST"])
def transmit():
    file_name = request.json.get("file_name", "")
    num_symbols = request.json.get("num_symbols", "")
    single_pkt = request.json.get("single_pkt", False)

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
def stop():
    pluto.stop()
    return jsonify({"message": "Transmission stopped"}), 200


@app.route("/status", methods=["GET"])
def status():
    return jsonify({"transmitting": pluto.transmitting}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
