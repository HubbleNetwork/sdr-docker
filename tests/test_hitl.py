"""Annotations to the HITL log harvester over its Unix socket."""

import json
import math
import os
import socketserver
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler

import numpy as np
import pytest

from stream_web.app import _drain_results, app, state
from stream_web.hitl import HarvesterAnnotator, annotation_data, failure_annotation_data

ENTRY = {
    "phy_ver": 1, "ntw_id": 0x03316DBA, "seq_num": 42, "auth_tag": 0x1A2B3C4D,
    "channel_num": 5, "energy_dB": np.float64(-61.2), "freq_delta_hz": np.float32(812.5),
    "chipset": "nrf21", "header_n_corr": 0, "pdu_n_corr": 2, "num_pdu_symbols": 26,
    "payload_val": 0xBEEF, "payload_bytes": 2, "unix_ts": 1_791_000_000.25,
    "rx_start_unix_ts": 1_791_000_000.0,
}


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def harvester():
    """A stand-in for the harvester's POST /annotations, recording what it got."""
    received = []
    status = {"code": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, body))
            reply = json.dumps({"status": "ok", "line_no": len(received),
                                "host_rx_utc": "2026-11-05T00:00:00Z"}).encode()
            self.send_response(status["code"])
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "harvester.sock")
        server = _Server(path, Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        yield path, received, status
        server.shutdown()
        server.server_close()


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_data_matches_device_record_formats():
    data = annotation_data(ENTRY, center_freq_hz=2_482_440_375, sdr_type="pluto")
    # Same keys and formats as the firmware's HBLR1 sat_tx record
    assert data["seq"] == 42
    assert data["eid"] == "03316dba"
    assert data["tag"] == "1a2b3c4d"
    assert data["ch0"] == 5
    assert data["rx_start_utc"] == "2026-10-03T04:00:00.000Z"
    assert data["decoded_utc"] == "2026-10-03T04:00:00.250Z"
    assert data["payload_hex"] == "beef"
    assert type(data["freq_delta_hz"]) is float  # numpy scalars made plain
    json.dumps(data, allow_nan=False)


def test_payload_hex_keeps_leading_zeros():
    data = annotation_data({**ENTRY, "payload_val": 0x00BEEF, "payload_bytes": 3})
    assert data["payload_hex"] == "00beef"
    assert annotation_data({**ENTRY, "payload_val": 0, "payload_bytes": 0})["payload_hex"] == "0"


def test_nan_and_missing_fields_are_left_out():
    data = annotation_data({**ENTRY, "energy_dB": float("nan"), "chipset": "",
                            "freq_delta_hz": np.float32("inf"), "payload_val": None})
    assert not {"energy_db", "chipset", "freq_delta_hz", "payload_hex"} & data.keys()
    assert not any(isinstance(v, float) and not math.isfinite(v) for v in data.values())


def test_received_codewords_and_error_positions():
    data = annotation_data({**ENTRY, "header_syms": [16, 4, 63, 0, 7, 33],
                            "header_err_pos": [], "pdu_err_pos": [3, 21],
                            "pdu_syms_rx": np.array([5, 0, 63])})
    assert data["hdr_syms_rx"] == "10043f000721"
    assert data["pdu_syms_rx"] == "05003f"
    assert data["hdr_err_pos"] == [] and data["pdu_err_pos"] == [3, 21]
    json.dumps(data, allow_nan=False)
    # An older decoder without these fields: left out
    raw_keys = {"hdr_syms_rx", "pdu_syms_rx", "hdr_err_pos", "pdu_err_pos"}
    assert not raw_keys & annotation_data(ENTRY).keys()


FAILURE = {
    "phy_ver": 1, "decoded": False, "reason": "pdu_fail+sample_drop", "time_s": 0.4,
    "chipset": "nordic", "measured_synth_res": 488.1, "synth_res_val": 488.28125,
    "F0_hz": np.float64(-144125.0), "energy_dB": -26.1, "score": 1.0,
    "channel_num": 4, "hop_seq_idx": 1, "pkt_len_idx": 1, "num_pdu_symbols": 30,
    "header_n_corr": 0, "header_syms": [16, 4, 63, 0, 7, 33], "header_err_pos": [],
    "pdu_syms_rx": [5, 0, 63], "rx_start_unix_ts": 1_791_000_000.0, "unix_ts": 1_791_000_002.0,
    "ch0_est": 4, "f_center_hz": -128745.3, "chipset_offset_hz": np.float64(8_412.6),
    "sample_drop": False, "lo_hz": 2_482_465_375,
}


def test_failure_data():
    data = failure_annotation_data(FAILURE, center_freq_hz=2_482_440_375, sdr_type="pluto")
    assert data["evt"] == "sat_rx_fail"
    assert data["reason"] == "pdu_fail" and data["sample_drop"] is True
    assert data["ch0"] == 4 and data["ch0_est"] == 4  # both, so disagreements show
    assert (data["f_center_hz"], data["chipset_offset_hz"]) == (-128745, 8413)
    assert data["center_freq_hz"] == 2_482_465_375  # the LO then, not the startup one
    assert data["rx_start_utc"] == "2026-10-03T04:00:00.000Z"
    assert data["reported_utc"] == "2026-10-03T04:00:02.000Z" and "decoded_utc" not in data
    assert data["hdr_syms_rx"] == "10043f000721" and data["pdu_syms_rx"] == "05003f"
    assert type(data["f0_hz"]) is int  # numpy float, rounded to the Hz
    assert not {"seq", "eid", "tag"} & data.keys()
    json.dumps(data, allow_nan=False)

    header_fail = {k: v for k, v in FAILURE.items()
                   if k not in ("channel_num", "hop_seq_idx", "pkt_len_idx", "num_pdu_symbols",
                                "header_n_corr", "header_err_pos", "pdu_syms_rx")}
    data = failure_annotation_data({**header_fail, "reason": "header_fail", "ch0_est": 4})
    assert data["reason"] == "header_fail" and "sample_drop" not in data
    assert failure_annotation_data({**FAILURE, "reason": "pdu_fail",
                                    "sample_drop": True})["sample_drop"] is True
    assert data["ch0_est"] == 4 and "ch0" not in data and "pdu_syms_rx" not in data


def test_unknown_annotation_name_is_refused(harvester):
    ann = HarvesterAnnotator(harvester[0])
    with pytest.raises(ValueError):
        ann.submit([ENTRY], name="bogus")


def test_posts_each_entry(harvester):
    path, received, _ = harvester
    ann = HarvesterAnnotator(path, sdr_type="pluto").start()
    ann.submit([ENTRY, {**ENTRY, "seq_num": 43}])
    assert _wait(lambda: ann.sent == 2)
    assert [p for p, _ in received] == ["/annotations"] * 2
    body = received[0][1]
    assert body["name"] == "sat_rx" and body["requester"] == "sdr-docker"
    assert "board" not in body  # the SDR doesn't know which board sent it
    assert [b["data"]["seq"] for _, b in received] == [42, 43]


def test_harvester_absent_or_erroring_is_counted_not_raised(harvester, tmp_path):
    ann = HarvesterAnnotator(str(tmp_path / "missing.sock"), timeout_s=0.5).start()
    ann.submit([ENTRY])
    assert _wait(lambda: ann.failed == 1)
    assert ann.stats()["ok"] is False

    path, _, status = harvester
    status["code"] = 400
    ann = HarvesterAnnotator(path).start()
    ann.submit([ENTRY])
    assert _wait(lambda: ann.failed == 1) and ann.sent == 0


def test_legacy_packets_are_skipped(harvester):
    path, received, _ = harvester
    ann = HarvesterAnnotator(path).start()
    ann.submit([{**ENTRY, "phy_ver": -1, "ntw_id": 0x3FFFFFFFF}, ENTRY])
    assert _wait(lambda: ann.sent == 1)
    assert ann.skipped == 1 and len(received) == 1


def test_unexpected_error_does_not_stop_the_worker(harvester):
    path, received, _ = harvester
    ann = HarvesterAnnotator(path).start()
    ann.submit([{**ENTRY, "rx_start_unix_ts": 1e20},  # OverflowError in datetime
                {**ENTRY, "ntw_id": object()},          # TypeError
                ENTRY])
    assert _wait(lambda: ann.sent == 1)
    stats = ann.stats()
    assert stats["failed"] == 2 and stats["alive"] and stats["ok"]


def test_recovers_when_harvester_comes_back(harvester):
    path, _, status = harvester
    status["code"] = 503
    ann = HarvesterAnnotator(path).start()
    ann.submit([ENTRY])
    assert _wait(lambda: ann.failed == 1) and ann.stats()["ok"] is False
    status["code"] = 200
    ann.submit([ENTRY])
    assert _wait(lambda: ann.sent == 1) and ann.stats()["ok"] is True


def test_drain_loop_submits_decoded_packets_and_status_reports_them(harvester):
    path, received, _ = harvester
    ann = HarvesterAnnotator(path).start()
    state.running.set()
    t = threading.Thread(target=_drain_results, args=(state, ann), daemon=True)
    t.start()
    try:
        state.result_queue.put({"decode_entries": [
            {**ENTRY, "ntw_id_hex": "0x03316DBA", "timestamp": "17:20:00",
             "energy_dB": -61.2, "freq_delta_hz": 812.5}]})
        state.result_queue.put({"stats": {}})  # a result without packets posts nothing
        state.result_queue.put({"decode_failures": [FAILURE]})
        assert _wait(lambda: ann.sent == 2)
        assert [(b["name"], b["data"]["evt"]) for _, b in received] == [
            ("sat_rx", "sat_rx"), ("sat_rx_fail", "sat_rx_fail")]
        assert received[0][1]["data"]["seq"] == 42
        state.hitl_annotator = ann
        status = app.test_client().get("/api/status").get_json()
        assert status["hitl_annotations"]["sent"] == 2
        assert status["hitl_annotations"]["sent_by_name"] == {"sat_rx": 1, "sat_rx_fail": 1}
    finally:
        state.hitl_annotator = None
        state.running.clear()
        t.join(timeout=2)
        with state.lock:
            state.decode_results.clear()
            state.packet_feed.clear()
    assert app.test_client().get("/api/status").get_json()["hitl_annotations"] is None
