"""Tests for SDR_TYPE=mock mode: mock injector, packet schema, Flask routes."""

import json
import threading
import time

from stream_web.app import _MOCK_DEVICES, _mock_injector, app, state

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_INTERVAL_S = 0.05

# Keys accessed via direct indexing (r["key"]) in api_status / api_packets —
# these MUST be present in every mock packet entry.
_REQUIRED_KEYS = {
    "ntw_id",
    "ntw_id_hex",
    "seq_num",
    "energy_dB",
    "timestamp",
}

# Keys accessed via r.get("key") — safe if missing, but should still be
# present for full fidelity.
_OPTIONAL_KEYS = {
    "phy_ver",
    "chipset",
    "freq_delta_hz",
    "payload_val",
    "payload_bytes",
    "unix_ts",
    "channel_num",
}


def _reset():
    with state.lock:
        state.packet_feed.clear()
        state.decode_results.clear()


def _run_injector(n_packets: int = 4):
    """Run the mock injector until at least *n_packets* are produced."""
    _reset()
    state.running.set()
    t = threading.Thread(
        target=_mock_injector,
        args=(state,),
        kwargs={"interval_s": _MOCK_INTERVAL_S},
        daemon=True,
    )
    t.start()
    # Wait long enough for n_packets (each takes _MOCK_INTERVAL_S)
    time.sleep(_MOCK_INTERVAL_S * (n_packets + 1) + 0.1)
    state.running.clear()
    t.join(timeout=2)


# ---------------------------------------------------------------------------
# 1. Unit test — mock injector populates state correctly
# ---------------------------------------------------------------------------


class TestMockInjector:
    def test_packets_are_produced(self):
        _run_injector(2)
        with state.lock:
            feed = list(state.packet_feed)
            results = list(state.decode_results)
        assert len(feed) >= 2, f"Expected >=2 packets, got {len(feed)}"
        assert len(results) >= 2

    def test_alternating_devices(self):
        _run_injector(4)
        with state.lock:
            feed = list(state.packet_feed)
        ntw_ids = [p["ntw_id"] for p in feed]
        device_ids = {d[0] for d in _MOCK_DEVICES}
        assert set(ntw_ids) == device_ids, f"Expected devices {device_ids}, got {set(ntw_ids)}"

    def test_incrementing_seq_nums(self):
        _run_injector(8)
        with state.lock:
            feed = list(state.packet_feed)
        per_device: dict[int, list[int]] = {}
        for p in feed:
            per_device.setdefault(p["ntw_id"], []).append(p["seq_num"])
        for nid, seqs in per_device.items():
            for i in range(1, len(seqs)):
                assert seqs[i] == (seqs[i - 1] + 1) % 256, (
                    f"Device {nid:#x}: seq_num jumped from {seqs[i-1]} to {seqs[i]}"
                )


# ---------------------------------------------------------------------------
# 2. Packet schema contract test
# ---------------------------------------------------------------------------


class TestPacketSchema:
    def test_all_required_keys_present(self):
        _run_injector(2)
        with state.lock:
            feed = list(state.packet_feed)
        assert feed, "No packets produced"
        for entry in feed:
            missing = _REQUIRED_KEYS - entry.keys()
            assert not missing, f"Missing required keys: {missing}"

    def test_all_optional_keys_present(self):
        _run_injector(2)
        with state.lock:
            feed = list(state.packet_feed)
        for entry in feed:
            missing = _OPTIONAL_KEYS - entry.keys()
            assert not missing, f"Missing optional keys: {missing}"

    def test_field_types(self):
        _run_injector(2)
        with state.lock:
            entry = state.packet_feed[0]
        assert isinstance(entry["ntw_id"], int)
        assert isinstance(entry["ntw_id_hex"], str)
        assert entry["ntw_id_hex"].startswith("0x")
        assert isinstance(entry["seq_num"], int)
        assert isinstance(entry["energy_dB"], (int, float))
        assert isinstance(entry["timestamp"], str)
        assert isinstance(entry["unix_ts"], (int, float))
        assert isinstance(entry["phy_ver"], int)
        assert isinstance(entry["chipset"], str)

    def test_ntw_id_hex_matches_ntw_id(self):
        _run_injector(2)
        with state.lock:
            feed = list(state.packet_feed)
        for entry in feed:
            expected = f"0x{entry['ntw_id']:08X}"
            assert entry["ntw_id_hex"] == expected


# ---------------------------------------------------------------------------
# 3. Flask route integration tests
# ---------------------------------------------------------------------------


class TestFlaskRoutes:
    @staticmethod
    def _client():
        app.config["TESTING"] = True
        return app.test_client()

    def test_api_packets_returns_ndjson(self):
        _run_injector(2)
        client = self._client()
        resp = client.get("/api/packets")
        assert resp.status_code == 200
        assert resp.content_type == "application/x-ndjson"
        lines = resp.data.decode().strip().split("\n")
        assert len(lines) >= 1
        packet = json.loads(lines[0])
        assert "device_id" in packet
        assert "seq_num" in packet
        assert "device_type" in packet
        assert "timestamp" in packet
        assert "rssi_dB" in packet
        assert packet["device_id"].startswith("0x")

    def test_api_packets_drains(self):
        _run_injector(2)
        client = self._client()
        resp1 = client.get("/api/packets")
        lines1 = resp1.data.decode().strip().split("\n")
        assert len(lines1) >= 1
        # Second call should be empty (drained)
        resp2 = client.get("/api/packets")
        assert resp2.data.decode().strip() == ""

    def test_api_status_returns_devices(self):
        _run_injector(4)
        client = self._client()
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "devices" in data
        assert "stats" in data
        assert len(data["devices"]) == len(_MOCK_DEVICES)
        for dev in data["devices"]:
            assert "ntw_id" in dev
            assert "ntw_id_hex" in dev
            assert "seq_nums" in dev
            assert "last_seen" in dev

    def test_api_status_device_fields(self):
        _run_injector(2)
        client = self._client()
        resp = client.get("/api/status")
        dev = resp.get_json()["devices"][0]
        assert isinstance(dev["ntw_id"], int)
        assert isinstance(dev["seq_nums"], list)
        assert isinstance(dev["max_energy_dB"], (int, float))
        assert "chipset" in dev
