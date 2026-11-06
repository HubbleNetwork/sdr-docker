"""Recovering from an SDR that can't be opened, and not leaking worker processes.

gnuradio_rx needs GNU Radio, which only the Docker image has: these tests give it
a stand-in `gnuradio` module and replace SDRFlowgraph.
"""

import multiprocessing as mp
import os
import sys
import time
import types

import pytest


class _Block:
    def __init__(self, *args, **kwargs):
        pass


if "gnuradio" not in sys.modules:
    _gnuradio = types.ModuleType("gnuradio")
    _gnuradio.gr = types.SimpleNamespace(sync_block=_Block, top_block=_Block)
    _gnuradio.soapy = types.SimpleNamespace(source=None)
    sys.modules["gnuradio"] = _gnuradio

from stream_web import config, gnuradio_rx  # noqa: E402
from stream_web.td_renderer import td_renderer_main  # noqa: E402

_ctx = mp.get_context("fork")


class _Exited(Exception):
    pass


def _fake_state():
    state = types.SimpleNamespace(running=_ctx.Event(), workers={},
                                  rx_connected=types.SimpleNamespace(set=lambda: None))
    state.running.set()
    return state


def _wait_gone(pid, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_connect_exits_for_fresh_process_after_repeated_failures(monkeypatch):
    attempts = []

    class FailingFlowgraph:
        def __init__(self, state):
            attempts.append(1)
            raise RuntimeError("no device found in this context")

    def fake_exit(state):
        raise _Exited

    monkeypatch.setattr(gnuradio_rx, "SDRFlowgraph", FailingFlowgraph)
    monkeypatch.setattr(gnuradio_rx, "_exit_for_restart", fake_exit)
    monkeypatch.setattr(config, "SDR_RETRY_INTERVAL_S", 0)
    monkeypatch.setattr(config, "SDR_OPEN_ATTEMPTS_BEFORE_RESTART", 4)
    with pytest.raises(_Exited):
        gnuradio_rx._connect(_fake_state())
    assert len(attempts) == 4


def test_connect_keeps_retrying_in_process_below_the_limit(monkeypatch):
    attempts = []

    class FlakyFlowgraph:
        info_string = "fake"

        def __init__(self, state):
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("no device found in this context")

        def start(self):
            pass

    monkeypatch.setattr(gnuradio_rx, "SDRFlowgraph", FlakyFlowgraph)
    monkeypatch.setattr(gnuradio_rx, "_exit_for_restart",
                        lambda state: pytest.fail("restarted below the limit"))
    monkeypatch.setattr(config, "SDR_RETRY_INTERVAL_S", 0)
    monkeypatch.setattr(config, "SDR_OPEN_ATTEMPTS_BEFORE_RESTART", 4)
    assert isinstance(gnuradio_rx._connect(_fake_state()), FlakyFlowgraph)
    assert len(attempts) == 3


def _polite_worker(running):
    while running.is_set():
        time.sleep(0.05)


def _stuck_worker(running):
    while True:
        time.sleep(0.05)


def test_exit_for_restart_stops_workers_first(monkeypatch):
    state = _fake_state()
    state.workers = {"polite": _ctx.Process(target=_polite_worker, args=(state.running,)),
                     "stuck": _ctx.Process(target=_stuck_worker, args=(state.running,))}
    for p in state.workers.values():
        p.start()
    codes = []

    def fake_os_exit(code):
        codes.append(code)
        raise _Exited

    monkeypatch.setattr(gnuradio_rx, "_WORKER_STOP_TIMEOUT_S", 1.0)
    monkeypatch.setattr(gnuradio_rx.os, "_exit", fake_os_exit)
    with pytest.raises(_Exited):
        gnuradio_rx._exit_for_restart(state)
    assert codes == [gnuradio_rx._EXIT_CODE_CONNECTION_LOST]
    assert not state.running.is_set()
    for p in state.workers.values():
        p.join(timeout=2)
        assert not p.is_alive()
    assert state.workers["polite"].exitcode == 0      # left its loop
    assert state.workers["stuck"].exitcode == -15     # had to be terminated


def _parent_that_dies(running, pid_queue):
    worker = _ctx.Process(target=td_renderer_main,
                          args=(_ctx.Queue(), _ctx.Queue(), running), daemon=True)
    worker.start()
    pid_queue.put(worker.pid)
    time.sleep(0.3)
    os._exit(3)  # like the app on SDR loss: no multiprocessing cleanup


def test_worker_stops_when_its_parent_is_gone():
    running = _ctx.Event()
    running.set()  # never cleared: only the parent check can stop the worker
    pid_queue = _ctx.Queue()
    parent = _ctx.Process(target=_parent_that_dies, args=(running, pid_queue))
    parent.start()
    worker_pid = pid_queue.get(timeout=5)
    parent.join(timeout=5)
    assert parent.exitcode == 3
    assert _wait_gone(worker_pid), "worker outlived its parent"
