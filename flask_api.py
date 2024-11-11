from flask import Flask, request, jsonify
import pluto_tx
import threading

app = Flask(__name__)


def async_tx(pluto, t):
    pluto.start(t, main=False)


@app.route("/tx", methods=["POST"])
def transmit():
    file_name = request.json.get("file_name", "")
    num_symbols = request.json.get("num_symbols", "")
    attn = request.json.get("attn", 0)
    freq = request.json.get("freq", 2.4831e9)
    sample_rate = request.json.get("sample_rate", 781250)
    single_pkt = request.json.get("single_pkt", False)
    t = request.json.get("time", 1)

    multiple_packets = not single_pkt
    file_dir = "/app/source_files"

    if file_name != "":
        file_name = os.path.join(file_dir, file_name)
    elif num_symbols != "":
        file_name = os.path.join(file_dir, f"tx_hubble_pkts_{num_symbols}symbols.out")

    err = None
    # transmit a tone
    if file_name == "":
        tx_type = "tone"
        pluto = pluto_tx.pluto_tx_tone(
            attenuation=attn, center_freq=freq, sample_rate=sample_rate
        )

    # transmit packets
    else:
        tx_type = "packet"
        if not os.path.exists(file_name):
            err = f"File {file_name} does not exist"

        pluto = pluto_tx.plut_tx_pkt(
            attenuation=attn,
            center_freq=freq,
            sample_rate=sample_rate,
            file_name=file_name,
            multiple_packets=multiple_packets,
        )

    if err:
        return {"error": err}, 400

    print(f"Transmitting {tx_type} for {t} seconds")
    thread = threading.Thread(target=async_tx, args=(pluto, t))
    thread.start()
    return jsonify({"message": f"Transmitting {tx_type} for {t} seconds"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
