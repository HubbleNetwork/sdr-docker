"""Choosing which failed decode attempts to report (rx_failures.FailureTracker)."""

import pytest

from stream_web import config
from stream_web.rx_failures import FAIL_REASONS, REPORTED_REASONS, FailureTracker, plausible

FS = config.SAMPLE_RATE
SR = config.SYNTH_RES["nordic"]
SLOT = config.slot_samples[1]["slot"] / FS
WINDOW = 1.5
CYCLE = 0.6
HOP = 1
N_PDU = 30
PKT_S = (config.PREAMBLE_LEN + config.NUM_HEADER_SYMS + N_PDU) * SLOT


def f0_for(ch, offset=0.0, sr=SR):
    """Symbol-0 frequency of a packet on channel ``ch``, ``offset`` Hz off nominal."""
    return (ch - config.LO_CHANNEL) * config.CHANNEL_SPACING - 31.5 * sr + offset


def phantom_f0(ch, sym, offset=0.0, hop=HOP):
    """A frequency inside the band packet (ch, hop) is on at symbol ``sym``."""
    seq = config.HOPPING_SEQS[hop]
    now = seq[(seq.index(ch) + sym // config.NUM_SYM_PER_HOP) % config.NUM_CHANNELS]
    step = round(config.CHANNEL_SPACING / SR) * SR
    return f0_for(ch, offset) + (now - ch) * step + 20 * SR


class Ev:
    """Something on air: what the decoder reports for it in a window that holds it."""

    def __init__(self, start, reason, f0, *, ch=None, hop=None, score=0.8, chipset="nordic",
                 sr=SR, length=PKT_S):
        self.start, self.length = start, length
        self.attempt = {"phy_ver": 1, "reason": reason, "decoded": reason == "ok",
                        "F0_hz": f0, "chipset": chipset, "measured_synth_res": abs(sr) - 0.2,
                        "synth_res_val": sr, "score": score}
        if ch is not None:  # the header decoded
            self.attempt.update(channel_num=ch, hop_seq_idx=HOP if hop is None else hop,
                                num_pdu_symbols=N_PDU)

    def seen(self, window_start, window_end):
        return window_start <= self.start and self.start + self.length <= window_end

    def cut_short(self, window_start, window_end):
        """Header decoded but the window ends inside the PDU: pdu_incomplete."""
        header_end = self.start + (config.PREAMBLE_LEN + config.NUM_HEADER_SYMS) * SLOT
        return ("channel_num" in self.attempt and window_start <= self.start
                and header_end <= window_end < self.start + self.length)


def packet(start, reason, ch, offset=0.0, **kw):
    """A real packet; header-failed ones don't know their channel."""
    known = ch if reason != "header_fail" else None
    return Ev(start, reason, f0_for(ch, offset), ch=known, **kw)


def phantom(parent_start, ch, sym, offset=0.0, score=0.75):
    """A false detection ``sym`` symbols into a packet on ``ch``."""
    return Ev(parent_start + sym * SLOT, "header_fail", phantom_f0(ch, sym, offset),
              score=score, length=config.PREAMBLE_LEN * SLOT)


def run(events, *, cycle=CYCLE, until=None, stall_at=None, wall_step_at=None, drops=(),
        report_reasons=FAIL_REASONS):
    """Run cycles over ``events``; return every report.

    Reports header failures too by default, so the selection logic is tested on
    both stages; the tracker's own default reports only ``pdu_fail``.

    ``stall_at``: from then on the sample clock stops (the SDR stalled) for 5
    cycles. ``wall_step_at``: the wall clock jumps 2 s then. ``drops`` are
    sample-clock times of sample drops.
    """
    tracker = FailureTracker(window_s=WINDOW, report_reasons=report_reasons)
    until = until or max(e.start for e in events) + 5
    out, end, wall, stalled = [], WINDOW, 1_000.0, 0
    while end < until:
        start = end - WINDOW
        attempts = []
        for e in events:
            at = {"start_sample": round((e.start - start) * FS), "time_s": e.start - start}
            if e.seen(start, end):
                attempts.append(dict(e.attempt, **at))
            elif e.cut_short(start, end):
                attempts.append(dict(e.attempt, **at, reason="pdu_incomplete", decoded=False))
        win_drops = [d - start for d in drops if start <= d < end]
        out += tracker.update(end, attempts, wall, drop_offsets_s=win_drops)
        wall += cycle
        if wall_step_at is not None and abs(end - wall_step_at) < cycle / 2:
            wall += 2.0
        if stall_at is not None and end >= stall_at and stalled < 5:
            stalled += 1  # same samples decoded again
        else:
            end += cycle
    return out


def starts(reports):
    return sorted(round(r["rx_start_unix_ts"] - 1_000.0 + WINDOW, 2) for r in reports)


def test_plausible_filters_by_stage_and_synth_res():
    a = packet(1.0, "header_fail", 4).attempt
    assert plausible(a)
    assert plausible(dict(a, reason="pdu_fail+sample_drop"))
    for reason in ("snr_fail", "pdu_incomplete", "hop_fail", "ok"):
        assert not plausible(dict(a, reason=reason))
    assert not plausible(dict(a, phy_ver=-1))
    assert not plausible(dict(a, chipset="silabs", measured_synth_res=154.8))
    assert not plausible(dict(a, chipset="atmosic", measured_synth_res=4053.6))


def test_phantoms_inside_a_decoded_packet_are_not_reported():
    evs = [packet(2.0, "ok", 4)] + [phantom(2.0, 4, s) for s in (1, 6, 16, 25)]
    assert run(evs) == []


def test_failed_packet_reported_once_with_its_phantoms_absorbed():
    evs = [packet(2.0, "pdu_fail", 4, score=0.9)] + [phantom(2.0, 4, s, score=0.95)
                                                     for s in (1, 16, 30)]
    out = run(evs)
    assert len(out) == 1
    assert out[0]["reason"] == "pdu_fail" and out[0]["channel_num"] == 4
    assert starts(out) == [2.0]


def test_header_failure_absorbs_phantoms_on_any_hopping_sequence():
    evs = [packet(2.0, "header_fail", 7, score=0.9)] + [phantom(2.0, 7, s) for s in (2, 20)]
    out = run(evs)
    assert len(out) == 1 and starts(out) == [2.0] and out[0]["ch0_est"] == 7


def early(start, ch, slots, tones=0, offset=0.0, score=0.6):
    """The preamble of a packet on ``ch`` read ``slots`` early, F0 ``tones`` steps up."""
    return Ev(start - slots * SLOT, "header_fail", f0_for(ch, offset) + tones * SR,
              score=score)


def test_preamble_read_early_is_not_reported():
    # Seen on wa-02: 2-4 slots early at the same F0, 1-3 early with 0/63 swapped
    for slots, tones in ((2, 0), (3, 0), (4, 0), (8, 0), (1, 63), (3, 63)):
        evs = [packet(2.0, "ok", 4), early(2.0, 4, slots, tones)]
        assert run(evs) == [], (slots, tones)


def test_real_failure_behind_an_early_read_is_the_one_reported():
    for slots in (2, 4):
        evs = [packet(2.0, "pdu_fail", 4), early(2.0, 4, slots, score=0.95)]
        out = run(evs)
        assert len(out) == 1 and out[0]["reason"] == "pdu_fail", slots
        assert starts(out) == [2.0]


def test_failure_just_before_a_packet_on_another_frequency_is_kept():
    # Another board, 4 slots before a decoded packet: not a misread of it
    evs = [packet(2.0, "ok", 4), early(2.0, 6, 4)]
    out = run(evs)
    assert len(out) == 1 and out[0]["ch0_est"] == 6
    # Too early to be the same preamble
    assert len(run([packet(2.0, "ok", 4), early(2.0, 4, 12)])) == 1


def test_another_boards_failure_during_a_decoded_packet_is_reported():
    # Board A decodes on ch4; board B on ch14 fails 0.15 s into it
    evs = [packet(2.0, "ok", 4), packet(2.15, "header_fail", 14, chipset="nordic")]
    out = run(evs)
    assert len(out) == 1 and out[0]["ch0_est"] == 14


def test_two_boards_failing_together_are_reported_separately():
    evs = [packet(2.0, "pdu_fail", 4), packet(2.1, "pdu_fail", 14)]
    out = run(evs)
    assert sorted(r["channel_num"] for r in out) == [4, 14]


def test_failure_decoded_in_another_window_is_not_reported():
    # Same packet: fails in the first window that holds it, decodes in the next
    class Flaky(Ev):
        def seen(self, s, e):
            ok = super().seen(s, e)
            if ok:
                self.attempt.update(reason="ok", decoded=True) if self.n else None
                self.n += 1
            return ok
    ev = Flaky(2.0, "pdu_fail", f0_for(4), ch=4)
    ev.n = 0
    assert run([ev]) == []


@pytest.mark.parametrize("report_reasons", [FAIL_REASONS, REPORTED_REASONS])
def test_same_packet_in_several_windows_keeps_the_furthest_attempt(report_reasons):
    # Also when header failures aren't reported: the later pdu_fail still is
    class Improving(Ev):
        def seen(self, s, e):
            ok = super().seen(s, e)
            if ok and self.n == 1:  # the second window gets past the header
                self.attempt.update(reason="pdu_fail", channel_num=4, hop_seq_idx=HOP,
                                    num_pdu_symbols=N_PDU)
            self.n += ok
            return ok
    ev = Improving(2.0, "header_fail", f0_for(4))
    ev.n = 0
    out = run([ev], report_reasons=report_reasons)
    assert len(out) == 1 and out[0]["reason"] == "pdu_fail"


def test_header_failures_are_not_reported_by_default():
    evs = [packet(2.0, "header_fail", 4), packet(4.0, "pdu_fail", 11)]
    assert FailureTracker().report_reasons == REPORTED_REASONS
    out = run(evs, report_reasons=REPORTED_REASONS)
    assert [r["reason"] for r in out] == ["pdu_fail"]


def test_unreported_header_failure_still_absorbs_its_phantoms():
    # A phantom inside a header-failed packet that happens to get a header
    # through must not be reported as a pdu_fail of its own
    ghost = phantom(2.0, 7, 20)
    ghost.attempt.update(reason="pdu_fail", channel_num=7, hop_seq_idx=HOP,
                         num_pdu_symbols=N_PDU)
    evs = [packet(2.0, "header_fail", 7, score=0.9), ghost]
    assert run(evs, report_reasons=REPORTED_REASONS) == []


def test_stalled_sdr_does_not_repeat_a_failure():
    out = run([packet(2.0, "header_fail", 4)], stall_at=3.3)
    assert len(out) == 1


def test_wall_clock_step_does_not_split_a_failure():
    out = run([packet(2.0, "header_fail", 4)], wall_step_at=3.3)
    assert len(out) == 1


def test_slow_cycles_do_not_leak_phantoms_of_decoded_packets():
    for cycle in (0.6, 0.8, 1.0, 1.2):
        for k in range(20):
            start = 2.0 + k * 0.037
            evs = [packet(start, "ok", 4), phantom(start, 4, 30)]
            assert run(evs, cycle=cycle) == [], (cycle, start)


def test_channel_estimate_takes_off_the_chipsets_offset():
    # Decoded packets from a TI-like board 35 kHz high teach the offset
    ti = config.SYNTH_RES["ti"]
    evs = [Ev(1.6 + i * 0.6, "ok", f0_for(3, 35_000, ti), ch=3, chipset="ti", sr=ti)
           for i in range(8)]
    evs.append(Ev(7.0, "header_fail", f0_for(11, 35_000, ti), chipset="ti", sr=ti))
    out = run(evs)
    assert len(out) == 1
    assert out[0]["ch0_est"] == 11
    assert round(out[0]["chipset_offset_hz"]) == 35_000


def test_channel_estimate_without_history_and_out_of_range():
    out = run([packet(2.0, "header_fail", 11, offset=5_000)])
    assert out[0]["ch0_est"] == 11 and out[0]["chipset_offset_hz"] is None
    far = Ev(2.0, "header_fail", 400_000.0)
    assert run([far])[0]["ch0_est"] is None


def test_sample_drop_anywhere_in_the_packet_is_flagged():
    assert run([packet(2.0, "pdu_fail", 4)], drops=[2.3])[0]["sample_drop"] is True
    assert run([packet(2.0, "pdu_fail", 4)], drops=[2.6])[0]["sample_drop"] is False
