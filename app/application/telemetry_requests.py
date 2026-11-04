"""Trusted request ownership shared with a synchronous endpoint's worker thread.

The owner is local to one request; child operation state remains independent.
Composition injects these functions into the HTTP observer, without SDK imports.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

from . import telemetry_contract as contract, telemetry_semantics


class _Request:
    def __init__(self, channel):
        self.channel = channel if channel in contract.CHANNELS else "unknown"
        self.started = time.monotonic()
        self.result = {}
        self.lock = threading.Lock()

    def finish(self, outcome):
        if outcome not in contract.OUTCOMES:
            return
        with self.lock:
            if not self.result:
                self.result = {
                    "nkb.request.outcome": outcome,
                    "nkb.request.summary": "request.handle completed",
                    "nkb.request.duration_ms": max(0.0, time.monotonic() - self.started) * 1000,
                }

    def snapshot(self):
        with self.lock:
            return {"nkb.request.channel": self.channel, "nkb.request.operation": "handle", **self.result}


_owner = ContextVar("nkb_request_owner", default=None)


@contextmanager
def request_scope(channel):
    token = _owner.set(_Request(channel))
    try:
        with telemetry_semantics.operation_context(**{"nkb.request.channel": channel}):
            yield
    finally:
        _owner.reset(token)


def record_result(outcome):
    """Called only by trusted route code; never read request headers or extras."""
    try:
        owner = _owner.get()
        if owner is not None:
            owner.finish(outcome)
    except Exception:
        pass


def current_request_facts():
    owner = _owner.get()
    return owner.snapshot() if owner is not None else {}
