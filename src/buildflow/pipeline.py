"""Execute one linear build plan and emit truthful step state transitions."""
import time
from .contracts import BuildError, Context, EventSink, Step
from .display_text import plain


def execute(steps: list[Step], context: Context, sink: EventSink) -> None:
    """Stop immediately on a failed action; success requires every action."""
    for index, step in enumerate(steps, 1):
        started = time.monotonic()
        sink({"type": "step_start", "name": step.name, "index": index, "total": len(steps)})
        try:
            step.action(context)
        except Exception as exc:
            code = exc.exit_code if isinstance(exc, BuildError) else 1
            raise BuildError(f"失败阶段 [{index}/{len(steps)}] {plain(step.name)}\n{plain(str(exc))}", code) from exc
        sink({"type": "step_end", "name": step.name, "elapsed": time.monotonic() - started})
