"""Post decoded packets to the HITL log harvester as annotations.

On a HITL hub the hitl-log-harvester logs every board's serial output, including
the HBLR1 ``sat_tx`` record the firmware prints for each satellite packet it
sends::

    HBLR1 {"evt":"sat_tx","seq":42,"tx":0,"t_dev":123,"eid":"03316dba","tag":"1a2b3c4d",
           "ch":[5,9,...],"sym":"..."} *CRC

The harvester also takes ``POST /annotations`` on its Unix socket and writes each
one to the warehouse (``dw.hitl_test_runner_logs``, ``kind='annotation'``). For
every packet the SDR decodes we post an annotation named ``sat_rx`` whose ``data``
uses the same key names and formats as the device record (``seq``, ``eid``,
``tag``: lowercase 8-digit hex), so a received packet joins to the device row
that sent it on ``(eid, seq, tag, ch0 = ch[0])`` within a time window. Only phy v1
packets are posted: the firmware record uses the v1 layout, and legacy v-1 packets
(34-bit network ID, 16-bit tag) could never match one.

Each ``sat_rx`` also carries the codewords as received, before Reed-Solomon
correction (``hdr_syms_rx``, ``pdu_syms_rx``: two hex digits per 6-bit symbol,
the PDU de-whitened) and the symbol positions RS corrected (``hdr_err_pos``,
``pdu_err_pos``), when the decoder provides them.

Packets whose header decoded but whose PDU failed are posted as ``sat_rx_fail``,
one per packet (see rx_failures.py; header failures aren't posted), with whatever
got decoded: the received symbols, the channel (``ch0`` from the header; ``ch0_est``
from the packet's frequency) and the start time. They have no
seq/eid/tag, so they match a device's ``sat_tx`` on channel and time only.

Posting runs on its own thread behind a bounded queue: decoding never waits for
the harvester, and if it is down, slow or absent the annotations are dropped
(and counted), never retried.
"""

import datetime as _dt
import http.client
import json
import math
import queue
import socket
import threading

ANNOTATION_NAME = "sat_rx"
FAIL_ANNOTATION_NAME = "sat_rx_fail"
REQUESTER = "sdr-docker"
_QUEUE_MAX = 1000
_TIMEOUT_S = 2.0


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _hex32(value) -> str | None:
    return None if value is None else f"{int(value):08x}"


def _utc(unix_ts) -> str | None:
    if unix_ts is None:
        return None
    t = _dt.datetime.fromtimestamp(unix_ts, tz=_dt.timezone.utc)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _syms_hex(symbols) -> str | None:
    """6-bit symbols as two hex digits each: [5, 63, 0] -> "053f00"."""
    if symbols is None:
        return None
    return "".join(f"{int(s):02x}" for s in symbols)


def _round(value, digits: int):
    """numpy/NaN handling as _plain, then round (to an int for 0 digits)."""
    value = _plain(value)
    if not isinstance(value, float):
        return value
    return round(value, digits) if digits else round(value)


def _positions(pos) -> list[int] | None:
    return None if pos is None else [int(p) for p in pos]


def annotation_data(entry: dict, center_freq_hz: int | None = None,
                    sdr_type: str | None = None) -> dict:
    """Build the annotation ``data`` for one decode entry (see processor.py).

    ``seq``, ``eid`` and ``tag`` match the device's ``sat_tx`` record for phy v1
    packets (the only ones HarvesterAnnotator posts).
    """
    data = {
        "evt": ANNOTATION_NAME,
        "seq": entry.get("seq_num"),
        "eid": _hex32(entry.get("ntw_id")),
        "tag": _hex32(entry.get("auth_tag")),
        "phy_ver": entry.get("phy_ver"),
        # Channel of the packet's first frame (the device's ch[0])
        "ch0": entry.get("channel_num"),
        # Rough time the packet started on air: host clock at the start of the
        # decode cycle minus the packet's offset in the decode window. Late by the
        # RX buffering latency (tens to hundreds of ms), and by the length of any
        # sample drop after the packet. Don't use it for sub-second matching.
        "rx_start_utc": _utc(entry.get("rx_start_unix_ts")),
        "decoded_utc": _utc(entry.get("unix_ts")),
        "energy_db": entry.get("energy_dB"),
        "freq_delta_hz": entry.get("freq_delta_hz"),
        "chipset": entry.get("chipset") or None,
        "rs_corr_header": entry.get("header_n_corr"),
        "rs_corr_pdu": entry.get("pdu_n_corr"),
        "num_pdu_symbols": entry.get("num_pdu_symbols"),
        "payload_bytes": entry.get("payload_bytes"),
        # As received, before RS correction; positions RS corrected
        "hdr_syms_rx": _syms_hex(entry.get("header_syms")),
        "pdu_syms_rx": _syms_hex(entry.get("pdu_syms_rx")),
        "hdr_err_pos": _positions(entry.get("header_err_pos")),
        "pdu_err_pos": _positions(entry.get("pdu_err_pos")),
        "sdr": sdr_type,
        # The LO when the packet was decoded (it can be retuned at runtime)
        "center_freq_hz": entry.get("lo_hz", center_freq_hz),
    }
    payload = entry.get("payload_val")
    if payload is not None:
        width = 2 * int(entry.get("payload_bytes") or 0)
        data["payload_hex"] = f"{int(payload):0{width}x}"
    return {k: v for k, v in ((k, _plain(v)) for k, v in data.items()) if v is not None}


def failure_annotation_data(entry: dict, center_freq_hz: int | None = None,
                            sdr_type: str | None = None) -> dict:
    """Build the annotation ``data`` for one failed attempt (see rx_failures.py)."""
    reason = str(entry.get("reason", ""))
    data = {
        "evt": FAIL_ANNOTATION_NAME,
        "reason": reason.split("+", 1)[0],
        # The SDR lost samples during the packet: not an RF failure
        "sample_drop": bool(entry.get("sample_drop") or "+sample_drop" in reason) or None,
        "phy_ver": entry.get("phy_ver"),
        # From the header (when it decoded); from the frequency (always)
        "ch0": entry.get("channel_num"),
        "ch0_est": entry.get("ch0_est"),
        # Packet centre relative to the channel-9 centre (CENTER_FREQ_HZ), and
        # the chipset's typical offset taken off it to get ch0_est
        "f_center_hz": _round(entry.get("f_center_hz"), 0),
        "chipset_offset_hz": _round(entry.get("chipset_offset_hz"), 0),
        "rx_start_utc": _utc(entry.get("rx_start_unix_ts")),
        # When the failure was settled and reported, ~2 s after rx_start_utc
        "reported_utc": _utc(entry.get("unix_ts")),
        "energy_db": _round(entry.get("energy_dB"), 1),
        "f0_hz": _round(entry.get("F0_hz"), 0),
        "chipset": entry.get("chipset") or None,
        "synth_res": _round(entry.get("measured_synth_res"), 2),
        "score": _round(entry.get("score"), 3),
        "hop_seq_idx": entry.get("hop_seq_idx"),
        "pkt_len_idx": entry.get("pkt_len_idx"),
        "num_pdu_symbols": entry.get("num_pdu_symbols"),
        "rs_corr_header": entry.get("header_n_corr"),
        "hdr_syms_rx": _syms_hex(entry.get("header_syms")),
        "pdu_syms_rx": _syms_hex(entry.get("pdu_syms_rx")),
        "hdr_err_pos": _positions(entry.get("header_err_pos")),
        "sdr": sdr_type,
        "center_freq_hz": entry.get("lo_hz", center_freq_hz),
    }
    return {k: v for k, v in ((k, _plain(v)) for k, v in data.items()) if v is not None}


_BUILDERS = {ANNOTATION_NAME: annotation_data, FAIL_ANNOTATION_NAME: failure_annotation_data}


def _plain(value):
    """numpy scalars -> Python; NaN/inf -> None (the harvester rejects them)."""
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class HarvesterAnnotator:
    """Queues decode entries and posts them to the harvester on a worker thread."""

    def __init__(self, socket_path: str, center_freq_hz: int | None = None,
                 sdr_type: str | None = None, timeout_s: float = _TIMEOUT_S):
        self.socket_path = socket_path
        self.center_freq_hz = center_freq_hz
        self.sdr_type = sdr_type
        self.timeout_s = timeout_s
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._failing = False  # log the first failure and the recovery, not every packet
        self.sent = 0
        self.sent_by_name = {name: 0 for name in _BUILDERS}
        self.dropped = 0
        self.failed = 0
        self.skipped = 0

    def start(self) -> "HarvesterAnnotator":
        self._thread = threading.Thread(target=self._run, name="hitl-annotator", daemon=True)
        self._thread.start()
        print(f"[hitl] Posting decoded packets to {self.socket_path} /annotations", flush=True)
        return self

    def submit(self, entries, name: str = ANNOTATION_NAME) -> None:
        """Queue phy v1 entries to post as ``name``; never blocks.

        Drops entries if the queue is full.
        """
        if name not in _BUILDERS:
            raise ValueError(f"unknown annotation name {name!r}")
        for entry in entries:
            if entry.get("phy_ver") != 1:
                self.skipped += 1
                continue
            try:
                self._queue.put_nowait((name, entry))
            except queue.Full:
                self.dropped += 1

    def submit_failures(self, entries) -> None:
        """Queue failed attempts from rx_failures.FailureTracker as ``sat_rx_fail``."""
        self.submit(entries, FAIL_ANNOTATION_NAME)

    def stats(self) -> dict:
        alive = self._thread is not None and self._thread.is_alive()
        return {"socket": self.socket_path, "sent": self.sent,
                "sent_by_name": dict(self.sent_by_name), "failed": self.failed,
                "dropped": self.dropped, "skipped": self.skipped,
                "queued": self._queue.qsize(), "alive": alive,
                "ok": alive and not self._failing}

    def post(self, entry: dict, name: str = ANNOTATION_NAME) -> dict:
        """Post one annotation; returns the harvester's reply or raises."""
        body = json.dumps({
            "name": name,
            "requester": REQUESTER,
            "data": _BUILDERS[name](entry, self.center_freq_hz, self.sdr_type),
        }, allow_nan=False).encode()
        conn = _UnixHTTPConnection(self.socket_path, self.timeout_s)
        try:
            conn.request("POST", "/annotations", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            reply = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}: {reply[:200].decode(errors='replace')}")
            return json.loads(reply or b"{}")
        finally:
            conn.close()

    def _run(self) -> None:
        while True:
            name, entry = self._queue.get()
            try:
                self.post(entry, name)
            except Exception as e:  # anything: one bad entry mustn't stop the thread
                self.failed += 1
                if not self._failing:
                    print(f"[hitl] WARNING: annotation to {self.socket_path} failed: {e} "
                          f"(further failures not logged until it recovers)", flush=True)
                    self._failing = True
                continue
            self.sent += 1
            self.sent_by_name[name] += 1
            if self._failing:
                print(f"[hitl] Harvester reachable again ({self.failed} failed so far)",
                      flush=True)
                self._failing = False
