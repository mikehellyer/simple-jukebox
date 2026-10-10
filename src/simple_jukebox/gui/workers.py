"""Running slow things (library scans, podcast searches, update checks,
device syncs) off the GUI thread, with results delivered back on it."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot


class _CallableWorker(QObject):
    finished = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            outcome = (True, self._fn())
        except Exception as error:  # handed to on_error on the GUI thread
            outcome = (False, error)
        self.finished.emit(outcome)


class _Relay(QObject):
    """Lives on the GUI thread, so queued signals into its slots are
    guaranteed to run there. (Connecting a worker's signal straight to a
    plain Python function gives no such guarantee in PySide — even with
    Qt.QueuedConnection the call runs on the worker's thread, where
    touching widgets crashes.)

    It also keeps the job's thread and worker referenced until the thread
    has actually finished: dropping a QThread that's still running
    aborts the whole app."""

    def __init__(self, runner: "BackgroundRunner", thread: QThread, worker: QObject, on_finished, on_error):
        super().__init__(runner)
        self._runner = runner
        self._thread = thread
        self._worker = worker
        self._on_finished = on_finished
        self._on_error = on_error

    @Slot(object)
    def deliver(self, outcome) -> None:
        ok, value = outcome
        if ok:
            self._on_finished(value)
        elif self._on_error is not None:
            self._on_error(value)
        else:
            raise value

    @Slot()
    def thread_finished(self) -> None:
        # `finished` is emitted from the job's thread just *before* that
        # thread actually exits, and dropping our references below can
        # destroy the QThread at once — while it's technically still
        # running, which aborts the app. Waiting first closes that window
        # (it's only ever a moment).
        self._thread.wait()
        self._runner._jobs.remove(self)
        self._worker.deleteLater()
        self._thread.deleteLater()
        self.deleteLater()


class BackgroundRunner(QObject):
    """Owns the threads it starts, so they can't be collected mid-flight
    and can all be waited for on shutdown."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs: list[_Relay] = []

    def run(self, fn: Callable, on_finished: Callable, on_error: Optional[Callable] = None) -> None:
        """fn() runs on a background thread; on_finished(result), or
        on_error(exception) if it raised, runs back on the GUI thread."""
        thread = QThread()
        worker = _CallableWorker(fn)
        relay = _Relay(self, thread, worker, on_finished, on_error)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(relay.deliver, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        thread.finished.connect(relay.thread_finished, Qt.QueuedConnection)
        self._jobs.append(relay)
        thread.start()

    def shutdown(self, timeout_ms: int) -> None:
        for relay in list(self._jobs):
            relay._thread.quit()
            relay._thread.wait(timeout_ms)
