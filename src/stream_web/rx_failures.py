"""Pick out real v1 packets that failed to decode, one report per packet.

The decoder returns an attempt for every preamble detection, and most failed
attempts aren't packets. The data symbols of a real packet often look like a
preamble, so each packet also yields "phantom" detections inside its own span,
and because decode windows overlap (DECODE_WINDOW_S long, one every
DECODE_INTERVAL_S) the same packet is attempted two or three times. A failure
is reported only if:

  * it's phy v1 and got past preamble/chipset detection, failing on the header
    RS or the PDU RS (``header_fail``, ``pdu_fail``). ``snr_fail`` (no clear
    preamble) and ``pdu_incomplete`` (the window cut the packet short; it is
    attempted again in the next one) are not reported;
  * its measured synth resolution is within SYNTH_RES_TOLERANCE of the
    identified chipset's;
  * it isn't a phantom of a packet that decoded (in any window), of one the
    window cut short after its header decoded (``pdu_incomplete``: slow cycles
    can leave a packet never whole in any window), or of another failure: it
    starts inside that packet and its frequency is inside the band that packet
    occupies at that moment (from its hopping sequence; for a header failure,
    whose sequence is unknown, any of the sequences). Nor may it be that
    packet's preamble read from too early a start: the preamble also matches
    1-8 slots before the packet, at the same F0 or 63 tone steps higher.

The same packet seen in several windows (same start and frequency) is reported
once, keeping the attempt that got furthest (PDU over header), then the best
preamble score. Packets from other boards on other channels are kept apart by
frequency; one whose frequency happens to fall in another packet's band at
that moment is taken for a phantom.

Times are on a sample clock (samples written to the IQ buffer), so a stalled
SDR re-decoding the same samples, wall-clock steps and sample drops don't
create duplicates. A failure is reported once no later window can contain it,
about DECODE_WINDOW_S + one packet after it started.

Free of numpy and decoder imports, like td_capture.py, so it is tested with
plain dicts (tests/test_rx_failures.py).
"""

from collections import defaultdict, deque
from statistics import median

from . import config

FAIL_REASONS = ("header_fail", "pdu_fail")
_STAGE = {"header_fail": 1, "pdu_fail": 2}
# Decoded attempts (energy_too_low decoded too, but was below the energy floor)
_DECODED_REASONS = ("ok", "energy_too_low")
SYNTH_RES_TOLERANCE = 0.03  # fraction of the chipset's synth resolution
SAME_PACKET_S = 0.02        # same start: the refined start is sample-exact
F0_TOLERANCE_HZ = 1000.0    # same frequency (~2 FSK bins)
PAD_S = 0.02                # around a packet's span when matching phantoms
_OFFSET_HISTORY = 50        # decoded packets per chipset for the offset median
_MIN_OFFSET_SAMPLES = 5


def _slot_s() -> float:
    return config.slot_samples[1]["slot"] / config.SAMPLE_RATE


def max_packet_s() -> float:
    """Longest v1 packet on air (preamble, header and the longest PDU)."""
    return (config.PREAMBLE_LEN + config.NUM_HEADER_SYMS + max(config.RS_N_V1)) * _slot_s()


def base_reason(attempt: dict) -> str:
    return str(attempt.get("reason", "")).split("+", 1)[0]


def plausible(attempt: dict) -> bool:
    """A v1 attempt that got to header/PDU decoding with a real chipset's synth res."""
    if attempt.get("decoded") or attempt.get("phy_ver") != 1:
        return False
    if base_reason(attempt) not in FAIL_REASONS:
        return False
    nominal = config.SYNTH_RES.get(attempt.get("chipset"))
    measured = attempt.get("measured_synth_res")
    if not nominal or measured is None or attempt.get("F0_hz") is None:
        return False
    return abs(abs(measured) - nominal) <= SYNTH_RES_TOLERANCE * nominal


def centre_hz(attempt: dict) -> float | None:
    """Middle of the packet's 64 tones, relative to the SDR centre.

    Uses the signed synth resolution, so it's right for inverted spectra too.
    """
    f0, sr = attempt.get("F0_hz"), attempt.get("synth_res_val")
    if f0 is None or sr is None:
        return None
    return f0 + 31.5 * sr


def _nominal_hz(channel: int) -> float:
    return (channel - config.LO_CHANNEL) * config.CHANNEL_SPACING


def channel_for(freq_hz: float) -> int | None:
    """Channel whose nominal centre is nearest ``freq_hz`` (relative to channel LO_CHANNEL)."""
    ch = round(freq_hz / config.CHANNEL_SPACING) + config.LO_CHANNEL
    return ch if 0 <= ch < config.NUM_CHANNELS else None


class _Packet:
    """A detection on the sample clock: start/end, frequency, and its hop bands."""

    def __init__(self, attempt: dict, window_start: float, channel: int | None = None):
        self.attempt = attempt
        start_sample = attempt.get("start_sample")
        offset = (start_sample / config.SAMPLE_RATE if start_sample is not None
                  else attempt.get("time_s", 0.0))
        self.start = window_start + offset
        n_pdu = attempt.get("num_pdu_symbols")
        n_sym = (config.PREAMBLE_LEN + config.NUM_HEADER_SYMS + n_pdu if n_pdu
                 else None)
        self.end = self.start + (n_sym * _slot_s() if n_sym else max_packet_s())
        self.f0 = attempt.get("F0_hz")
        self.sr = attempt.get("synth_res_val") or 0.0
        # The header's channel and sequence, else the estimate and every sequence
        self.channel = attempt.get("channel_num", channel)
        hop = attempt.get("hop_seq_idx")
        self.sequences = ([config.HOPPING_SEQS[hop]] if hop is not None
                          else list(config.HOPPING_SEQS))

    def same_as(self, other: "_Packet") -> bool:
        return (abs(self.start - other.start) <= SAME_PACKET_S
                and abs(self.f0 - other.f0) <= F0_TOLERANCE_HZ)

    def misread_early(self, other: "_Packet") -> bool:
        """``other`` is this packet's preamble read from too early a start.

        The preamble's alternating 63/0 tones also match the detector a few
        slots before the packet; the decoder then reads part of the preamble
        as the header (``hdr_syms_rx`` starting 3f 00 3f ...). Seen on air:
        ``other`` starts 1-8 slots early with the same F0, or with F0 on this
        packet's symbol-63 tone (63 tone steps higher; the 0 and 63 tones
        swapped).
        """
        lead = self.start - other.start
        if not (SAME_PACKET_S < lead <= (config.PREAMBLE_LEN + 0.5) * _slot_s()):
            return False
        df = other.f0 - self.f0
        return min(abs(df), abs(df - 63 * self.sr)) <= F0_TOLERANCE_HZ

    def covers(self, other: "_Packet", same_counts: bool = True) -> bool:
        """``other`` is a phantom of this packet.

        It starts inside this packet, at a frequency this packet is on then,
        or it's this packet misread from too early a start (``misread_early``).
        With ``same_counts``, ``other`` being this very packet counts too.
        """
        if self.misread_early(other):
            return True
        if not (self.start - PAD_S <= other.start <= self.end + PAD_S):
            return False
        if self.same_as(other):
            return same_counts
        if self.channel is None or not self.sr:
            return False
        sr = abs(self.sr)
        step = round(config.CHANNEL_SPACING / sr) * sr
        sym = max(0, int((other.start - self.start) / _slot_s()))
        # The phantom's 8 preamble symbols may straddle a hop
        hops = {sym // config.NUM_SYM_PER_HOP,
                (sym + config.PREAMBLE_LEN - 1) // config.NUM_SYM_PER_HOP}
        for seq in self.sequences:
            if self.channel not in seq:
                continue
            first = seq.index(self.channel)
            for h in hops:
                ch = seq[(first + h) % config.NUM_CHANNELS]
                lo = self.f0 + (ch - self.channel) * step
                hi = lo + 63 * self.sr
                if min(lo, hi) - F0_TOLERANCE_HZ <= other.f0 <= max(lo, hi) + F0_TOLERANCE_HZ:
                    return True
        return False


def _rank(attempt: dict) -> tuple:
    return (_STAGE.get(base_reason(attempt), 0), attempt.get("score") or 0.0)


class FailureTracker:
    """Carries failed attempts across decode cycles until they can be reported."""

    def __init__(self, window_s: float | None = None):
        self.window_s = config.DECODE_SAMPLES / config.SAMPLE_RATE if window_s is None else window_s
        self.packet_s = max_packet_s()
        self._pending: list[_Packet] = []
        self._decoded: list[_Packet] = []
        self._partial: list[_Packet] = []  # header decoded, cut short by the window
        self._drops: list[float] = []
        self._offsets: dict[str, deque] = defaultdict(lambda: deque(maxlen=_OFFSET_HISTORY))

    def chipset_offset_hz(self, chipset: str) -> float | None:
        """Median frequency offset of this chipset's decoded packets (None until enough)."""
        hist = self._offsets.get(chipset)
        return median(hist) if hist and len(hist) >= _MIN_OFFSET_SAMPLES else None

    def update(self, stream_end_s: float, attempts: list[dict], wall_end: float,
               lo_offset_hz: float = 0.0, drop_offsets_s=()) -> list[dict]:
        """Take one cycle's attempts; return failures now ready to report.

        ``stream_end_s`` is the end of this cycle's decode window on the sample
        clock (samples written so far / sample rate) and ``wall_end`` the wall
        clock then. ``lo_offset_hz`` is how far the SDR is tuned from
        CENTER_FREQ_HZ, ``drop_offsets_s`` where samples were dropped, from the
        window start. Each returned dict is the chosen attempt plus
        ``rx_start_unix_ts``, ``sample_drop``, ``f_center_hz`` (packet centre
        relative to CENTER_FREQ_HZ), ``chipset_offset_hz`` and ``ch0_est``.
        """
        window_start = stream_end_s - self.window_s
        self._drops += [window_start + d for d in drop_offsets_s]

        for a in attempts:
            if a.get("phy_ver") != 1:
                continue
            if a.get("decoded") or base_reason(a) in _DECODED_REASONS:
                self._add_decoded(_Packet(a, window_start), lo_offset_hz)
            elif base_reason(a) == "pdu_incomplete" and a.get("channel_num") is not None:
                part = _Packet(a, window_start)
                if not any(part.same_as(q) for q in self._partial):
                    self._partial.append(part)

        candidates = sorted((_Packet(dict(a), window_start) for a in attempts if plausible(a)),
                            key=lambda p: p.start)
        for p in candidates:
            p.attempt["rx_start_unix_ts"] = wall_end - (stream_end_s - p.start)
            if p.channel is None:
                p.channel = self._estimate_channel(p.attempt, lo_offset_hz)
            if self._known_phantom(p):
                continue
            self._merge(p)

        # Nothing that started before `settled` can show up in a later window
        settled = window_start - self.packet_s
        ready = [p for p in self._pending if p.start < settled]
        self._pending = [p for p in self._pending if p.start >= settled]

        out = []
        for p in ready:
            if self._known_phantom(p):
                continue  # e.g. decoded in a later window
            out.append(self._report(p, lo_offset_hz))

        # Spans and drops that can't touch anything still pending
        self._decoded = [d for d in self._decoded if d.end + PAD_S >= settled]
        self._partial = [d for d in self._partial if d.end + PAD_S >= settled]
        self._drops = [t for t in self._drops if t >= settled - self.packet_s]
        return out

    def _known_phantom(self, p: _Packet) -> bool:
        """``p`` is (a phantom of) a packet that decoded, or a phantom of a cut-short one."""
        return (any(d.covers(p) for d in self._decoded)
                or any(q.covers(p, same_counts=False) for q in self._partial))

    def _add_decoded(self, packet: _Packet, lo_offset_hz: float) -> None:
        if any(packet.same_as(d) for d in self._decoded):
            return
        self._decoded.append(packet)
        a = packet.attempt
        centre, ch = centre_hz(a), a.get("channel_num")
        if centre is not None and ch is not None and a.get("chipset"):
            self._offsets[a["chipset"]].append(centre + lo_offset_hz - _nominal_hz(ch))

    def _estimate_channel(self, attempt: dict, lo_offset_hz: float) -> int | None:
        centre = centre_hz(attempt)
        if centre is None:
            return None
        offset = self.chipset_offset_hz(attempt["chipset"]) or 0.0
        return channel_for(centre + lo_offset_hz - offset)

    def _merge(self, packet: _Packet) -> None:
        # An early misread of this packet came first: the packet replaces it
        # (else the packet would look like a phantom inside the misread's span)
        self._pending = [p for p in self._pending if not packet.misread_early(p)]
        for i, p in enumerate(self._pending):
            if packet.same_as(p):
                # The same packet in another window: keep the attempt that got further
                if _rank(packet.attempt) > _rank(p.attempt):
                    self._pending[i] = packet
                return
            if p.covers(packet):
                return  # a phantom of a failure already pending
        # A window can hold a phantom before it holds the whole packet behind it
        self._pending = [p for p in self._pending if not packet.covers(p, same_counts=False)]
        self._pending.append(packet)

    def _report(self, p: _Packet, lo_offset_hz: float) -> dict:
        a = p.attempt
        centre = centre_hz(a)
        offset = self.chipset_offset_hz(a.get("chipset"))
        a["f_center_hz"] = None if centre is None else centre + lo_offset_hz
        a["chipset_offset_hz"] = offset
        a["ch0_est"] = self._estimate_channel(a, lo_offset_hz)
        a["sample_drop"] = ("+sample_drop" in str(a.get("reason", ""))
                            or any(p.start <= t <= p.end for t in self._drops))
        return a
