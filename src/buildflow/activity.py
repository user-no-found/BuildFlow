"""Semantic activity scopes shared by application contexts and command runners."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
import uuid


class Activities:
    """Publish safe caller-supplied labels without inspecting command arguments."""

    def __init__(self, sink=None):
        self.sink = sink or (lambda event: None)
        self.current = ContextVar("buildflow_activity", default="")

    @contextmanager
    def activity(self, name: str, *, target: str = ""):
        """Keep nested operations associated until the caller leaves the scope."""
        if not isinstance(name, str) or not name.strip() or not isinstance(target, str):
            raise ValueError("activity name must be nonempty text and target must be text")
        identity = uuid.uuid4().hex
        parent = self.current.get()
        started = time.monotonic()
        self.sink({"type": "activity_start", "id": identity, "parent_id": parent,
                   "name": name, "target": target})
        token = self.current.set(identity)
        outcome = "completed"
        try:
            yield
        except BaseException:
            outcome = "interrupted"
            raise
        finally:
            self.current.reset(token)
            self.sink({"type": "activity_end", "id": identity, "name": name, "target": target,
                       "outcome": outcome, "elapsed": time.monotonic() - started})

    def status(self, message: str):
        """Update the active operation using deliberate, non-secret prose."""
        if not isinstance(message, str):
            raise ValueError("status must be text")
        self.sink({"type": "activity_status", "activity_id": self.current.get(), "message": message})
