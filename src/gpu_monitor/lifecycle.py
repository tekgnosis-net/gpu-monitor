"""Process lifecycle: SIGTERM handling, asyncio task supervision,
graceful NVML shutdown.

The supervisor runs the four async tasks (collector, server,
scheduler, alert_checker, housekeeping) under one event loop, and
on SIGTERM/SIGINT cancels them all and waits for clean shutdown
before calling `pynvml.nvmlShutdown()`.

Why one supervisor function rather than asyncio.gather inline in
__main__: signal handling needs to be wired to the running loop,
and exception handling (one task crashing → cancel the rest) is
easier to reason about in a small dedicated module.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

import pynvml

log = logging.getLogger("gpu-monitor.lifecycle")


async def supervise(tasks: list[Callable[[], Awaitable[None]]]) -> None:
    """Run a list of zero-arg async coroutine factories concurrently.

    On SIGTERM/SIGINT, cancels all tasks and waits for them to exit
    cleanly. If any task raises a non-CancelledError exception, the
    other tasks are cancelled and the exception propagates up so the
    container supervisor can surface it.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _signal_handler():
        if not stop.is_set():
            log.info("lifecycle: signal received, initiating graceful shutdown")
            stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # add_signal_handler is unavailable on Windows; fall back
            # to signal.signal which is callable from any thread.
            signal.signal(sig, lambda s, f: _signal_handler())

    # Spawn each task. Wrap in a watchdog that propagates the first
    # failure to the stop event so the others get a chance to drain.
    spawned = [asyncio.create_task(_wrap(factory(), stop)) for factory in tasks]

    # Wait for either: (a) the stop event (signal received) or
    # (b) any task to fail with a non-cancellation exception.
    stop_waiter = asyncio.create_task(stop.wait())
    done, pending = await asyncio.wait(
        spawned + [stop_waiter],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel everything still running, then await all so cancellation
    # propagates and finally-clauses run.
    for task in spawned:
        if not task.done():
            task.cancel()
    if not stop_waiter.done():
        stop_waiter.cancel()

    # Surface the first non-cancellation exception, if any
    first_error: BaseException | None = None
    for task in spawned:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            log.error("lifecycle: task raised %s", exc)
            if first_error is None:
                first_error = exc

    if first_error is not None:
        raise first_error


async def _wrap(coro: Awaitable[None], stop: asyncio.Event) -> None:
    """Run a task; on unexpected exit, signal stop so siblings
    can shut down too."""
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except BaseException:
        stop.set()
        raise


def shutdown_nvml() -> None:
    """Best-effort NVML cleanup. Called once after supervise() returns."""
    try:
        pynvml.nvmlShutdown()
        log.info("lifecycle: NVML shutdown clean")
    except pynvml.NVMLError as exc:
        log.warning("lifecycle: nvmlShutdown failed (%s); continuing", exc)
