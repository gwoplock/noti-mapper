"""The outbound push pool.

The core thread must never block on a network call. It hands "apply this value
to this output" to this pool and carries on; the pool reports back through the
same core queue everything else arrives on, so the core thread stays the only
writer of persistent state.

This is also where v2's quiet hours will live. The dispatcher already sits
between a state change and the outbound I/O, which is the correct place to
defer a notification without deferring the latch. Nothing here needs to
anticipate it; it just must not be designed around its absence.
"""

import abc
import logging
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from noti_mapper.messages import PushResultMessage
from noti_mapper.plugin import OutputPlugin, OutputUpdate

ReportCallback = Callable[[PushResultMessage], None]


@dataclass(frozen=True)
class _PushRequest:
    instance_name: str
    update: OutputUpdate


class Dispatcher(abc.ABC):
    """What the core needs from whatever performs outbound pushes.

    An interface rather than a concrete class because the core must be
    testable without threads: a test drives an implementation that applies on
    the calling thread and gets a deterministic ordering.
    """

    @abc.abstractmethod
    def start(self) -> None:
        """Begin accepting work."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop accepting work and wait for in-flight pushes."""

    @abc.abstractmethod
    def set_outputs(self, outputs: Mapping[str, OutputPlugin]) -> None:
        """Replace the output table, on reload."""

    @abc.abstractmethod
    def dispatch(self, *, instance_name: str, update: OutputUpdate) -> None:
        """Queue a push. Must return promptly; the core thread is waiting."""


class OutputDispatcher(Dispatcher):
    """A small pool of threads that call ``OutputPlugin.apply``."""

    def __init__(
        self,
        *,
        outputs: Mapping[str, OutputPlugin],
        report: ReportCallback,
        thread_count: int,
        logger: logging.Logger | None = None,
    ) -> None:
        self._outputs: dict[str, OutputPlugin] = dict(outputs)
        self._outputs_lock = threading.Lock()
        self._report = report
        self._thread_count = max(1, thread_count)
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._queue: queue.Queue[_PushRequest | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._running = False

    def set_outputs(self, outputs: Mapping[str, OutputPlugin]) -> None:
        """Replace the output table, on reload."""
        with self._outputs_lock:
            self._outputs = dict(outputs)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for index in range(self._thread_count):
            thread = threading.Thread(
                target=self._worker, name=f"noti-dispatch-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        self._logger.debug("dispatcher started with %d threads", self._thread_count)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for _ in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=10.0)
        self._threads.clear()

    def dispatch(self, *, instance_name: str, update: OutputUpdate) -> None:
        """Queue a push. Returns immediately."""
        self._queue.put(_PushRequest(instance_name=instance_name, update=update))

    def _worker(self) -> None:
        while True:
            request = self._queue.get()
            if request is None:
                return
            self._perform(request)

    def _perform(self, request: _PushRequest) -> None:
        with self._outputs_lock:
            output = self._outputs.get(request.instance_name)

        if output is None:
            # The instance went away between the core enqueueing this and the
            # worker picking it up -- a reload, most likely.
            self._report(
                PushResultMessage(
                    instance_name=request.instance_name,
                    pushed_value=request.update.state,
                    succeeded=False,
                    error="output instance is no longer configured",
                )
            )
            return

        try:
            output.apply(request.update)
        except BaseException as error:  # noqa: BLE001 - a plugin must not kill the pool
            self._logger.warning(
                "push to %s failed: %s",
                request.instance_name,
                error,
                extra={"instance": request.instance_name, "value": request.update.state},
            )
            self._report(
                PushResultMessage(
                    instance_name=request.instance_name,
                    pushed_value=request.update.state,
                    succeeded=False,
                    error=f"{type(error).__name__}: {error}",
                )
            )
            return

        self._report(
            PushResultMessage(
                instance_name=request.instance_name,
                pushed_value=request.update.state,
                succeeded=True,
            )
        )
