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
        "sdr": sdr_type,
        "center_freq_hz": center_freq_hz,
    }
    payload = entry.get("payload_val")
    if payload is not None:
        width = 2 * int(entry.get("payload_bytes") or 0)
        data["payload_hex"] = f"{int(payload):0{width}x}"
    return {k: v for k, v in ((k, _plain(v)) for k, v in data.items()) if v is not None}


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
        self.dropped = 0
        self.failed = 0
        self.skipped = 0

    def start(self) -> "HarvesterAnnotator":
        self._thread = threading.Thread(target=self._run, name="hitl-annotator", daemon=True)
        self._thread.start()
        print(f"[hitl] Posting decoded packets to {self.socket_path} /annotations", flush=True)
        return self

    def submit(self, entries) -> None:
        """Queue phy v1 decode entries; never blocks. Drops entries if the queue is full."""
        for entry in entries:
            if entry.get("phy_ver") != 1:
                self.skipped += 1
                continue
            try:
                self._queue.put_nowait(entry)
            except queue.Full:
                self.dropped += 1

    def stats(self) -> dict:
        alive = self._thread is not None and self._thread.is_alive()
        return {"socket": self.socket_path, "sent": self.sent, "failed": self.failed,
                "dropped": self.dropped, "skipped": self.skipped,
                "queued": self._queue.qsize(), "alive": alive,
                "ok": alive and not self._failing}

    def post(self, entry: dict) -> dict:
        """Post one annotation; returns the harvester's reply or raises."""
        body = json.dumps({
            "name": ANNOTATION_NAME,
            "requester": REQUESTER,
            "data": annotation_data(entry, self.center_freq_hz, self.sdr_type),
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
            entry = self._queue.get()
            try:
                self.post(entry)
            except Exception as e:  # anything: one bad entry mustn't stop the thread
                self.failed += 1
                if not self._failing:
                    print(f"[hitl] WARNING: annotation to {self.socket_path} failed: {e} "
                          f"(further failures not logged until it recovers)", flush=True)
                    self._failing = True
                continue
            self.sent += 1
            if self._failing:
                print(f"[hitl] Harvester reachable again ({self.failed} failed so far)",
                      flush=True)
                self._failing = False
