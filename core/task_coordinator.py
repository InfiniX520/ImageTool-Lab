"""Bounded local scheduling for the new ledger, independent of Tk and HTTP."""
from __future__ import annotations

import queue
import re
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass

from core.http_client import close_current_http_session
from core.library_repository import BudgetExceeded
from core.task_contracts import BatchBudget, CollectionRequest, SourcePage


class CoordinatorClosed(RuntimeError):
    pass


class CommandQueueFull(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """A completed outcome is accepted only after durable completion in P3."""
    state: str = "failed"
    error_code: str = ""

    def __post_init__(self):
        if self.state not in {"completed", "failed", "suspended", "cancelled", "needs_verification", "recovery_needed", "blocked"}:
            raise ValueError("Invalid executor outcome")
        if not isinstance(self.error_code, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{0,64}", self.error_code):
            raise ValueError("Executor errors must use safe codes")


class TaskCoordinator:
    """One control owner and a fixed pool; all UI commands return Futures.

    Starting this object only recovers local state.  ``run_queued`` is the
    explicit authorization to dispatch; reopening a library never downloads.
    The caller-supplied executor must observe the stop Event and commit any
    successful file delivery through the P3 protocol before returning.
    """

    def __init__(self, repository, executor, *, workers=3, queue_capacity=64, command_capacity=32):
        for value, maximum in ((workers, 8), (queue_capacity, 1024), (command_capacity, 128)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Invalid coordinator capacity")
        if not callable(executor):
            raise TypeError("A page executor is required")
        self.repository, self.executor = repository, executor
        self._commands = queue.Queue(command_capacity)
        self._control = queue.Queue(4)
        self._pages = queue.Queue(queue_capacity)
        self._results = queue.SimpleQueue()
        self._capacity = queue_capacity
        self._guard = threading.RLock()
        self._wake, self._stop, self._shutdown = (threading.Event() for _ in range(3))
        self._active = None
        self._queued, self._inflight = set(), {}
        self._dispatch = False
        self._closing = False
        self._started = False
        self._fatal = None
        self._ready = Future()
        self._owner = threading.Thread(target=self._run, name="collection-coordinator", daemon=False)
        self._workers = [threading.Thread(target=self._worker, name=f"collection-page-{n}", daemon=False)
                         for n in range(workers)]

    def start(self) -> Future:
        with self._guard:
            if self._closing:
                raise CoordinatorClosed("Coordinator is closed")
            if not self._started:
                self._started = True
                self._owner.start()
                for worker in self._workers:
                    worker.start()
        return self._ready

    def _command(self, name, *args, priority=False, **kwargs) -> Future:
        result = Future()
        with self._guard:
            if not self._started or self._closing or self._shutdown.is_set():
                result.set_exception(CoordinatorClosed("Coordinator is not accepting commands"))
                return result
            try:
                (self._control if priority else self._commands).put_nowait((name, args, kwargs, result))
            except queue.Full:
                result.set_exception(CommandQueueFull("Command capacity reached; try again"))
                return result
            if priority and self._active and self._active["job_id"] == args[0]:
                self._stop.set()
        self._wake.set()
        return result

    def submit(self, request: CollectionRequest, command_id: str, *, parent_job_id: str | None = None) -> Future:
        return self._command("submit", request, command_id, parent_job_id=parent_job_id)

    def resume(self, job_id: str, command_id: str, budget: BatchBudget | None = None, *, retry_failed=False) -> Future:
        return self._command("resume", job_id, command_id, budget=budget, retry_failed=retry_failed)

    def accept_page(self, batch_id: str, page: SourcePage) -> Future:
        return self._command("accept_source_page", batch_id, page)

    def finish_source(self, batch_id: str, *, coverage_complete: bool, reason_code="source_exhausted") -> Future:
        return self._command("finish_source", batch_id, coverage_complete=coverage_complete, reason_code=reason_code)

    def run_queued(self) -> Future:
        return self._command("run_queued")

    def pause(self, job_id: str, command_id: str) -> Future:
        return self._command("request_stop", job_id, command_id, priority=True)

    def cancel(self, job_id: str, command_id: str) -> Future:
        return self._command("request_stop", job_id, command_id, priority=True, cancel=True)

    def snapshot(self, job_id: str) -> Future:
        return self._command("snapshot", job_id)

    @property
    def active_batch_id(self):
        with self._guard:
            return self._active["batch_id"] if self._active else None

    def cancellation_requested(self,batch_id):
        with self._guard:
            return (self._closing or self._shutdown.is_set() or self._active is None
                or self._active['batch_id']!=batch_id or self._stop.is_set())

    def _execute(self, command):
        name, args, kwargs, future = command
        if not future.set_running_or_notify_cancel():
            return
        try:
            if name == "run_queued":
                self._dispatch = True
                result = True
            else:
                result = getattr(self.repository, name)(*args, **kwargs)
                if name == "request_stop":
                    self._dispatch = False
                    with self._guard:
                        if self._active and self._active["job_id"] == args[0]:
                            self._stop.set()
            future.set_result(result)
        except BaseException as exc:
            future.set_exception(exc)

    def _drain_pages(self):
        while True:
            try:
                delivery_id = self._pages.get_nowait()
            except queue.Empty:
                break
            with self._guard:
                self._queued.discard(delivery_id)
            self._pages.task_done()

    def _settle_results(self):
        while True:
            try:
                delivery_id, batch_id, claimed, outcome = self._results.get_nowait()
            except queue.Empty:
                break
            try:
                if claimed:
                    row = self.repository.get_delivery(delivery_id)
                    if row["state"] in {"prepared", "recovery_needed"}:
                        self.repository.request_block(batch_id, "recovery_needed")
                        self._stop.set()
                        self._dispatch = False
                    elif outcome.state == "blocked":
                        if row["state"] in {"transferring", "validating"}:
                            self.repository.release_delivery(delivery_id, batch_id, state="suspended",
                                                             error_code=outcome.error_code)
                        self.repository.request_block(batch_id, outcome.error_code or "page_blocked")
                        self._stop.set()
                        self._dispatch = False
                    elif row["state"] not in {"completed", "cancelled", "failed", "needs_verification", "suspended"}:
                        if self._stop.is_set():
                            outcome = DeliveryOutcome("suspended")
                        elif outcome.state not in {"failed", "cancelled"}:
                            outcome = DeliveryOutcome("failed", "uncommitted_result")
                        self.repository.release_delivery(delivery_id, batch_id,
                                                         state=outcome.state, error_code=outcome.error_code)
            finally:
                with self._guard:
                    self._inflight.pop(delivery_id, None)

    def _fill_pages(self):
        if not self._active or self._stop.is_set():
            return
        with self._guard:
            excluded = tuple(self._queued | self._inflight.keys())
        room = self._capacity - self._pages.qsize()
        if room <= 0:
            return
        ids = self.repository.list_dispatchable(self._active["job_id"], limit=min(room, 200), exclude_ids=excluded)
        for delivery_id in ids:
            with self._guard:
                if self._stop.is_set():
                    break
                self._queued.add(delivery_id)
                try:
                    self._pages.put_nowait(delivery_id)
                except queue.Full:
                    self._queued.discard(delivery_id)
                    break

    def _worker(self):
        try:
            while not self._shutdown.is_set():
                try:
                    delivery_id = self._pages.get(timeout=0.1)
                except queue.Empty:
                    continue
                batch_id, claimed = None, False
                try:
                    with self._guard:
                        self._queued.discard(delivery_id)
                        if not self._active or self._stop.is_set() or self._shutdown.is_set():
                            continue
                        batch_id = self._active["batch_id"]
                        self._inflight[delivery_id] = batch_id
                    try:
                        task = self.repository.claim_delivery(delivery_id, batch_id)
                        claimed = task is not None
                        if not claimed or self._stop.is_set():
                            outcome = DeliveryOutcome("suspended")
                        else:
                            outcome = self.executor(task, self._stop)
                            if not isinstance(outcome, DeliveryOutcome):
                                outcome = DeliveryOutcome("failed", "invalid_executor_result")
                    except BudgetExceeded:
                        outcome = DeliveryOutcome("suspended", "work_budget")
                    except Exception:
                        outcome = DeliveryOutcome("failed", "page_executor_error")
                    self._results.put((delivery_id, batch_id, claimed, outcome))
                    self._wake.set()
                finally:
                    self._pages.task_done()
        finally:
            try:
                close_current_http_session()
            except Exception as exc:
                with self._guard:
                    if self._fatal is None:
                        self._fatal = exc

    def _run(self):
        try:
            self.repository.recover_interrupted()
            recover = getattr(self.executor, "recover_local", None)
            if callable(recover):
                recover(limit=200)
            self._ready.set_result(True)
            while True:
                self._settle_results()
                for channel in (self._control, self._commands):
                    try:
                        self._execute(channel.get_nowait())
                    except queue.Empty:
                        continue
                    else:
                        channel.task_done()
                        break
                if self._closing:
                    self._dispatch = False
                    if self._active:
                        batch = self.repository.get_batch(self._active["batch_id"])
                        if batch["phase"] in {"running", "cooling", "settling"}:
                            self.repository.request_stop(batch["job_id"], "close-" + uuid.uuid4().hex,
                                                         reason_code="closing")
                        self._stop.set()
                if self._active:
                    batch = self.repository.get_batch(self._active["batch_id"])
                    if batch["phase"] in {"pausing", "cancelling"}:
                        self._stop.set()
                        self._drain_pages()
                        with self._guard:
                            busy = bool(self._inflight)
                        if not busy:
                            self.repository.settle_stop(batch["batch_id"])
                            batch = self.repository.get_batch(batch["batch_id"])
                    if batch["phase"] not in {"running", "cooling", "settling", "pausing", "cancelling"}:
                        with self._guard:
                            self._active = None
                            self._stop.clear()
                    elif batch["phase"] in {"running", "settling"}:
                        self._fill_pages()
                        with self._guard:
                            idle = not self._queued and not self._inflight
                        if idle:
                            budget = self.repository.budget_status(batch["batch_id"])
                            if budget["deferred_works"] and budget["used_works"] >= budget["max_works"]:
                                self.repository.request_stop(batch["job_id"], "budget-" + uuid.uuid4().hex,
                                                             reason_code="work_budget")
                                self._dispatch = False
                                self._stop.set()
                            elif batch["phase"] == "settling":
                                self.repository.settle_batch(batch["batch_id"])
                if not self._active and self._dispatch:
                    activated = self.repository.activate_next()
                    with self._guard:
                        self._active = activated
                        self._stop.clear()
                if self._closing and not self._active:
                    break
                self._wake.wait(0.05)
                self._wake.clear()
        except BaseException as exc:
            self._fatal = exc
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            self._stop.set()
            self._shutdown.set()
            self._drain_pages()
            for channel in (self._control, self._commands):
                while True:
                    try:
                        _, _, _, future = channel.get_nowait()
                    except queue.Empty:
                        break
                    if not future.done():
                        future.set_exception(CoordinatorClosed("Coordinator stopped"))
                    channel.task_done()

    def close(self, timeout=20):
        if threading.current_thread() in (self._owner, *self._workers):
            raise RuntimeError("Coordinator cannot be closed from its own worker")
        deadline = time.monotonic() + timeout
        with self._guard:
            self._closing = True
            self._stop.set()
            self._wake.set()
        if self._started:
            for thread in (self._owner, *self._workers):
                thread.join(max(0, deadline - time.monotonic()))
            if any(thread.is_alive() for thread in (self._owner, *self._workers)):
                raise TimeoutError("Collection workers are still settling; repository remains open")
        self.repository.close(timeout=max(0, deadline - time.monotonic()))
        if self._fatal:
            raise RuntimeError("Coordinator stopped after a storage/control failure") from self._fatal

    def __enter__(self):
        self.start().result(timeout=20)
        return self

    def __exit__(self, *_):
        self.close()
