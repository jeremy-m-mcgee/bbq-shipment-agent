"""Running the pipeline for a caller that cannot block on it.

A browser request cannot wait for a plan. A real run is one vision call per
screenshot, a Shippo validation per address, a few hundred rate quotes with
backoff, and a D1 model call -- tens of seconds when the cache is warm and
minutes when it is not. So a POST starts a job and returns, and the page
follows the same stage messages the CLI prints, as they happen.

## One run at a time

Refused rather than queued. Two concurrent runs would append to the same
append-only ledger and quote the same lanes twice, and the operator asking for
a second one has almost always double-clicked. Refusing says so; queueing
would look like nothing happened.

## The thread owns the LaunchDarkly client

`open_run` returns it open, because D1's metrics need the same client that
served the config. The worker closes it in a `finally`, since the SDK runs a
background thread and a leaked one keeps the server alive after ctrl-c.
"""

from __future__ import annotations

import contextlib
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..wiring import RunDepth, RunOptions, open_run, run_with


@dataclass(frozen=True)
class Event:
    """One thing that happened, in the order it happened.

    `seq` is what lets a reconnecting page ask for everything it missed rather
    than replaying the run from the start.
    """

    seq: int
    kind: str  # "stage" | "state" | "error"
    stage: str
    message: str
    fields: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "stage": self.stage,
            "message": self.message,
            **self.fields,
        }


class JobProgress:
    """A `Progress` that appends to a job instead of printing."""

    def __init__(self, job: RunJob) -> None:
        self._job = job

    def emit(self, stage: str, message: str, **fields: Any) -> None:
        self._job.emit("stage", stage, message, **fields)


class RunJob:
    """One planning run, its event log, and whatever it produced.

    Every mutation takes the lock: the worker thread writes and request
    handlers read, and a page that caught a half-appended event list would
    show a run that never happened.
    """

    def __init__(self, job_id: str, options: RunOptions) -> None:
        self.id = job_id
        self.options = options
        self.started_at = datetime.now(UTC)
        self.state = "running"  # running | finished | failed
        self.error: str | None = None
        self.traceback: str | None = None
        self.header: dict[str, Any] | None = None
        self.context: Any = None
        self.view: dict[str, Any] | None = None
        self._events: list[Event] = []
        self._lock = threading.Lock()

    def emit(self, kind: str, stage: str, message: str, **fields: Any) -> Event:
        with self._lock:
            event = Event(len(self._events), kind, stage, message, fields)
            self._events.append(event)
            return event

    def events_after(self, seq: int) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.seq > seq]

    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    @property
    def running(self) -> bool:
        return self.state == "running"


class RunService:
    """The registry. In-process, in-memory, and single-run by design.

    Nothing is persisted here, deliberately: the ledger is the record of what
    a run decided, and a second store of the same facts is a second thing to
    keep honest. What this holds is the live object a page is watching.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, RunJob] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def get(self, job_id: str) -> RunJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 10) -> list[RunJob]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order[-limit:])]

    @property
    def active(self) -> RunJob | None:
        with self._lock:
            for job_id in reversed(self._order):
                if self._jobs[job_id].running:
                    return self._jobs[job_id]
            return None

    def start(self, options: RunOptions) -> RunJob:
        """Begin a run, or refuse because one is already going."""
        with self._lock:
            for job_id in reversed(self._order):
                if self._jobs[job_id].running:
                    raise RunInProgress(
                        f"run {job_id} is still going. One at a time: two runs "
                        "would append to the same ledger and quote the same "
                        "lanes twice."
                    )
            job = RunJob(uuid.uuid4().hex[:12], options)
            self._jobs[job.id] = job
            self._order.append(job.id)

        thread = threading.Thread(target=_execute, args=(job,), daemon=True)
        thread.start()
        return job


class RunInProgress(RuntimeError):
    """A second run was asked for while the first was still going."""


def _execute(job: RunJob) -> None:
    """The worker. Everything it can raise becomes a failed job, not a crash.

    A traceback on a background thread would otherwise go to a terminal nobody
    is reading, and the page would sit on a spinner forever. The operator
    errors the CLI catches by name are the common case here too -- a missing
    roster, a kill switch, an unreachable validator -- and they read better as
    a message on the page than as a 500.
    """
    from .view import extract_view, plan_view, run_header

    progress = JobProgress(job)
    context = None
    try:
        context = open_run(job.options, progress)
        job.context = context
        job.header = run_header(context, job.options)
        job.emit(
            "state",
            "A1",
            f"run {context.run.run_id} opened ({context.connection})",
            run_id=context.run.run_id,
        )
        # `run_with` picks the depth, so the browser and the CLI stop in the
        # same place. Only the rendering differs, because an extract-only run
        # has no manifest to show and that is not a failure.
        run_with(context, job.options, progress)
        # Refreshed after planning: capabilities are evaluated live per stage,
        # so the header built at A1 held none of them and no fingerprint. By
        # here the set is resolved, which is what the page needs to show.
        job.header = run_header(context, job.options)
        job.view = (
            extract_view(context, job.options)
            if job.options.depth is RunDepth.EXTRACT
            else plan_view(context, job.options)
        )
        job.state = "finished"
        job.emit(
            "state",
            "done",
            _outcome_message(context),
            outcome=job.view["outcome"],
        )
    except BaseException as exc:  # noqa: BLE001 - the thread must not die silently
        job.state = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.traceback = traceback.format_exc()
        job.emit("error", "failed", job.error)
    finally:
        # The SDK runs a background thread. Leaving it open keeps the server
        # alive after ctrl-c, which looks like a hung process.
        if context is not None and context.client is not None:
            with contextlib.suppress(Exception):  # closing must not mask a result
                context.client.close()


def _outcome_message(context: Any) -> str:
    result = context.result
    if result is None and context.roster is not None:
        # An extract-only run. No manifest was ever going to exist, so saying
        # "no manifest" here would report a normal run as a disappointing one.
        return (
            f"extracted — {context.roster.packet_count} recipient(s) from "
            f"{len(context.images)} screenshot(s)"
        )
    if result is None or result.manifest is None:
        return "no manifest — no carrier subset covers the run"
    blockers = ()
    if result.verification is not None:
        blockers = result.verification.blockers
    manifest = result.manifest
    suffix = f", {len(blockers)} blocker(s) from D1" if blockers else ""
    return (
        f"manifest ready — {manifest.packet_count} packet(s), "
        f"${manifest.total_cost:,.2f}{suffix}"
    )
