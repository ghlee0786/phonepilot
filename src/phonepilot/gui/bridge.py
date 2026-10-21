"""Thread plumbing between the Qt GUI thread and the CoreRuntime asyncio thread.

- `Bridge` turns runtime listener callbacks (core thread) into Qt signals (GUI thread)
  and lets any thread post a callable to run on the GUI thread.
- `InputPump` serializes human input onto the core loop in submission order, so a
  DOWN can never overtake the MOVE/UP that follows it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal, Slot

if TYPE_CHECKING:
    from phonepilot.core.arbiter import InputArbiter
    from phonepilot.runtime import CoreRuntime

log = logging.getLogger(__name__)


class Bridge(QObject):
    """Lives on the GUI thread. Emitting from another thread is delivered as a queued call."""

    event = Signal(str, object)  # (runtime event name, data dict)
    _invoke = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._invoke.connect(self._run)

    def forward(self, event: str, data: dict[str, Any]) -> None:
        """Runtime listener: safe to call from any thread."""
        try:
            self.event.emit(event, data)
        except RuntimeError:  # bridge already deleted (window closed)
            pass

    def post(self, fn: Callable[[], Any]) -> None:
        """Run `fn` on the GUI thread (from any thread)."""
        try:
            self._invoke.emit(fn)
        except RuntimeError:
            pass

    @Slot(object)
    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - a broken UI callback must not kill the event loop
            log.exception("GUI callback failed")


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    return fn(*args)


def core_call(runtime: CoreRuntime, fn: Callable[..., Any], *args: Any) -> None:
    """Run a synchronous core method on the core loop (asyncio objects are not thread-safe)."""
    try:
        runtime.call(_call(fn, *args))
    except RuntimeError:  # loop closed
        pass


ArbiterOp = Callable[["InputArbiter"], Awaitable[Any]]


class InputPump:
    """Single consumer on the core loop that executes human input operations in FIFO order."""

    def __init__(self, runtime: CoreRuntime) -> None:
        self.runtime = runtime
        self._queue: asyncio.Queue[Callable[[], Awaitable[Any] | None]] | None = None
        self._task: asyncio.Task[None] | None = None

    def submit(self, factory: Callable[[], Awaitable[Any] | None]) -> None:
        try:
            self.runtime.loop.call_soon_threadsafe(self._enqueue, factory)
        except RuntimeError:  # loop closed during shutdown
            pass

    def arbiter(self, op: ArbiterOp) -> None:
        """Queue an operation on the current InputArbiter (resolved when it runs)."""

        def factory() -> Awaitable[Any] | None:
            stack = self.runtime.stack
            return None if stack is None else op(stack.arbiter)

        self.submit(factory)

    def _enqueue(self, factory: Callable[[], Awaitable[Any] | None]) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue()
            self._task = self.runtime.loop.create_task(self._worker())
        self._queue.put_nowait(factory)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            factory = await self._queue.get()
            try:
                aw = factory()
                if aw is not None:
                    await aw
            except Exception as e:  # noqa: BLE001
                log.debug("input op failed: %s", e)
            finally:
                self._queue.task_done()

    def close(self) -> None:
        """Stop the worker (call before the core loop stops so the task is not left pending)."""

        def cancel() -> None:
            if self._task is not None:
                self._task.cancel()
            self._task = None
            self._queue = None

        try:
            self.runtime.loop.call_soon_threadsafe(cancel)
        except RuntimeError:
            pass

    async def _join(self) -> None:
        if self._queue is not None:
            await self._queue.join()

    def flush(self, timeout: float = 5.0) -> None:
        """Block until everything submitted so far has been sent (tests, shutdown)."""
        self.runtime.call_sync(self._join(), timeout=timeout)
