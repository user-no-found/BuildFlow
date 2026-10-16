"""Serialize build events and terminal heartbeats through one owned writer."""
from datetime import datetime, timezone
import json
import queue
import threading
import time


class EventStream:
    """Drain producers before closing, keeping render failures out of build errors."""

    def __init__(self, log, view):
        self.log = log
        self.view = view
        self.queue = queue.Queue()
        self.failure = None
        self.error_delivered = False
        self.closed = False
        self.thread = threading.Thread(target=self._write, name="buildflow-events", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __call__(self, event):
        if event["type"] != "tick":
            self.queue.put({"time": datetime.now(timezone.utc).isoformat(), **event})

    def flush(self):
        """Wait until all preceding events have been recorded and presented."""
        if self.closed:
            return
        barrier = threading.Event()
        self.queue.put(barrier)
        barrier.wait()

    def __exit__(self, *exception):
        self.finish()

    def finish(self):
        """Drain pending events and restore terminal state exactly once."""
        if self.closed:
            return
        self.closed = True
        self.queue.put(None)
        self.thread.join()

    def _present(self, event):
        if self.failure is not None:
            return
        try:
            self.view(event)
            if event["type"] == "error":
                self.error_delivered = True
        except BaseException as exc:
            self.failure = exc

    def _write(self):
        active = False
        interactions = 0
        deferred = []
        deadline = time.monotonic() + .1
        while True:
            try:
                event = self.queue.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                event = {"type": "tick"}
            if event is None:
                close = getattr(self.view, "close", None)
                if callable(close):
                    try:
                        close()
                    except BaseException as exc:
                        self.failure = self.failure or exc
                return
            if isinstance(event, threading.Event):
                event.set()
                continue
            kind = event["type"]
            if kind != "tick":
                try:
                    self.log.write(json.dumps(event, ensure_ascii=False) + "\n")
                    self.log.flush()
                except BaseException as exc:
                    self.failure = self.failure or exc
                if kind == "build_start":
                    active = True
                elif kind in {"build_end", "error"}:
                    active = False
            if kind == "interaction_start":
                interactions += 1
                self._present(event)
            elif kind == "interaction_end":
                interactions -= 1
                self._present(event)
                if interactions == 0:
                    for pending in deferred:
                        self._present(pending)
                    deferred.clear()
            elif interactions:
                if kind != "tick":
                    deferred.append(event)
            elif kind != "tick" or active:
                self._present(event)
            if time.monotonic() >= deadline:
                if kind != "tick" and active and not interactions:
                    self._present({"type": "tick"})
                deadline = time.monotonic() + .1
