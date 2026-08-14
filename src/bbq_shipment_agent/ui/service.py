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

## The thread owns the LaunchDarkly client -- until a review takes it

`open_run` returns it open, because D1's metrics need the same client that
served the config. On a run that never enters review the worker closes it in a
`finally`, since the SDK runs a background thread and a leaked one keeps the
server alive after ctrl-c.

A run that produces a covering manifest does not finish at planning: it parks in
`awaiting_review` and hands the client to a `ReviewController`, because D2's
per-turn metrics report against the same client (narrator.py) and the review is
a sequence of HTTP turns over minutes. The controller then owns teardown -- on a
terminal state, on abandon, or via the app's shutdown hook -- and the worker's
`finally` must not close a client it handed off. See `_execute`.
"""

from __future__ import annotations

import contextlib
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..wiring import RunDepth, RunOptions, open_run, run_with


def _default_conversing_model(options: RunOptions) -> Any:
    """The live-only D2 model factory, imported lazily so a plain import of
    this module opens no socket and drags in no Anthropic SDK."""
    from ..wiring import conversing_model

    return conversing_model(options)


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

    def __init__(
        self,
        job_id: str,
        options: RunOptions,
        conversing_model: Callable[[RunOptions], Any] = _default_conversing_model,
    ) -> None:
        self.id = job_id
        self.options = options
        self.conversing_model = conversing_model
        self.started_at = datetime.now(UTC)
        # running   -- worker planning
        # awaiting_review -- planning done, parked in D2, client still open
        # approved | approved_with_exclusions | rejected -- E2 terminal states
        # abandoned -- operator left the review; nothing recorded
        # finished  -- extract-only or no-manifest; never entered review
        # failed    -- the worker raised
        self.state = "running"
        self.error: str | None = None
        self.traceback: str | None = None
        self.header: dict[str, Any] | None = None
        self.context: Any = None
        self.view: dict[str, Any] | None = None
        #: The parked D2 review, once planning produced a covering manifest.
        #: String annotation: `ReviewController` is defined lower in this module.
        self.review: ReviewController | None = None
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
        """The worker thread is still planning. Drives the planning SSE's end:
        a run that parks in `awaiting_review` is no longer running, so the
        stream closes and the page reloads into the review."""
        return self.state == "running"

    @property
    def occupies_slot(self) -> bool:
        """Whether this job holds the single run slot. A parked review still
        does: it *is* the run, continued, and a second POST /runs while it is
        open would append to the same ledger and re-quote the same lanes -- the
        exact thing one-at-a-time exists to prevent. This is why the refusal
        check is `occupies_slot` and the SSE end is `running`; they diverge the
        moment a review parks."""
        return self.state in {"running", "awaiting_review"}


class ReviewController:
    """A parked D2 review: the live `ReviewSession`, its optional `Narrator`,
    and the `RunContext` whose LaunchDarkly client must stay open until the
    review ends.

    One at a time, like a run -- it is the run, continued. `turn_lock`
    serialises operator turns: `Narrator.say` and `ReviewSession.propose` both
    mutate shared state and a turn makes blocking model calls, so an overlapping
    request is refused (409) rather than interleaved. It is deliberately *not*
    `RunJob._lock`, which guards only the event list -- a seconds-long model call
    must never hold that, or the read and SSE paths stall behind it.

    `close` is the single owner of client teardown once the worker hands the
    context over, and is idempotent because three paths reach it: a terminal
    state, an abandon, and the app's shutdown hook for a review left open.
    """

    def __init__(self, context: Any, session: Any, narrator: Any) -> None:
        self.context = context
        self.session = session
        self.narrator = narrator
        self.turn_lock = threading.Lock()
        #: Set when the operator left without approving or rejecting. Distinct
        #: from a terminal state: nothing is written, but the pane must show the
        #: review is over rather than offering controls again on a reload.
        self.abandoned = False
        self._closed = False
        self._close_lock = threading.Lock()

    @property
    def narrator_available(self) -> bool:
        return self.narrator is not None


    def abandon(self) -> None:
        """Leave the review without recording anything, and free the client."""
        self.abandoned = True
        self.close()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        client = self.context.client
        if client is not None:
            with contextlib.suppress(Exception):  # closing must not mask a result
                client.close()


class RunService:
    """The registry. In-process, in-memory, and single-run by design.

    Nothing is persisted here, deliberately: the ledger is the record of what
    a run decided, and a second store of the same facts is a second thing to
    keep honest. What this holds is the live object a page is watching.

    `conversing_model` is the D2 model factory, injectable so a test can supply
    a scripted model and drive the conversation without a socket. It defaults to
    the live-only factory, which raises `ModelUnavailable` offline and leaves the
    review button-driven.
    """

    def __init__(
        self,
        conversing_model: Callable[[RunOptions], Any] = _default_conversing_model,
    ) -> None:
        self._jobs: dict[str, RunJob] = {}
        self._order: list[str] = []
        self._conversing_model = conversing_model
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
                if self._jobs[job_id].occupies_slot:
                    return self._jobs[job_id]
            return None

    def start(self, options: RunOptions) -> RunJob:
        """Begin a run, or refuse because one is already going.

        A parked review counts as going -- see `RunJob.occupies_slot`. The slot
        is freed only when the review reaches a terminal state or is abandoned,
        not when planning finishes, because until then the ledger and the lanes
        are still the parked run's.
        """
        with self._lock:
            for job_id in reversed(self._order):
                if self._jobs[job_id].occupies_slot:
                    raise RunInProgress(
                        f"run {job_id} is still going. One at a time: two runs "
                        "would append to the same ledger and quote the same "
                        "lanes twice."
                    )
            job = RunJob(uuid.uuid4().hex[:12], options, self._conversing_model)
            self._jobs[job.id] = job
            self._order.append(job.id)

        thread = threading.Thread(target=_execute, args=(job,), daemon=True)
        thread.start()
        return job

    def close_all(self) -> None:
        """Close any still-open review's client. The app registers this on
        shutdown, so a review the operator walked away from does not keep the
        SDK's background thread alive after ctrl-c."""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.review is not None:
                job.review.close()


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
    handed_off = False
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
        result = context.result
        manifest = result.manifest if result is not None else None
        if job.options.depth is not RunDepth.EXTRACT and manifest is not None:
            # A covering plan does not finish here: D2 review is the last stage.
            # Parking hands the client to the controller, which now owns closing
            # it -- so the `finally` below must not. `_park_for_review` runs the
            # opening narration on this thread, before the state flips, so the
            # SSE keeps streaming until the first render already has it.
            _park_for_review(job, context)
            handed_off = True
        else:
            # Extract-only, or a plan no carrier subset covers. Neither enters
            # review -- the CLI returns before `_review` in the same cases -- so
            # the run finishes and the client is closed in the `finally`.
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
        # alive after ctrl-c, which looks like a hung process. But a review that
        # parked owns the client now -- closing it here would kill D2's per-turn
        # metrics -- so skip teardown when it was handed off; `ReviewController.
        # close` does it at a terminal state, on abandon, or via the shutdown
        # hook.
        if not handed_off and context is not None and context.client is not None:
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


def _park_for_review(job: RunJob, context: Any) -> None:
    """Build the D2 review over a covering manifest and park the job in it.

    The session construction is exactly the CLI's (`cli._review`): the deduped
    *eligible* set through `to_shipments`, not B2's output, so the operator is
    never offered an edit on a shipment the manifest never had -- and the seam
    that once crashed `run review` on a `Recipient` where a `Shipment` was
    wanted. The quoter is rebuilt from options (a `RecordedQuoter` for replay,
    the cached `ShippoQuoter` otherwise), which is what re-solves an edit.

    The narrator is best-effort: offline, or with no key, `conversing_model`
    raises `ModelUnavailable` and the review is button-driven, exactly the CLI's
    fallback. Its opening narration runs here, on the worker thread, so the
    first render already carries it rather than the pane appearing empty.
    """
    from ..agents import ModelUnavailable, NarratorUnavailable
    from ..recipients import to_shipments
    from ..review import ReviewSession
    from ..wiring import quoter

    result = context.result
    session = ReviewSession(
        context.run,
        to_shipments(result.suppression.eligible),
        context.roster.origin,
        context.roster.ship_dates,
        ledger_root=job.options.ledger,
        quoter=quoter(job.options),
        # `result.escalated`, not B2's alone: the review re-assembles the
        # manifest on every edit, so anything the planned manifest carried and
        # this does not disappears the moment the operator touches anything.
        escalated=result.escalated,
        suppressed=result.suppression.suppressed,
        advisories=result.validation.advisories(),
    )

    narrator = None
    try:
        from ..wiring import narrator_for

        narrator = narrator_for(
            context.run,
            session,
            options=job.options,
            ledger_root=job.options.ledger,
            client=context.client,
            model_factory=job.conversing_model,
        )
        narrator.open()
    except (NarratorUnavailable, ModelUnavailable) as exc:
        narrator = None
        job.emit(
            "state",
            "D2",
            f"review-narrator unavailable ({exc}); button-driven review",
        )

    # Set last, once everything that can fail has: if session construction or a
    # narrator error had raised uncaught, `handed_off` stays False and the
    # worker's `finally` still closes the client.
    job.review = ReviewController(context, session, narrator)
    job.state = "awaiting_review"
    job.emit("state", "D2", "review ready", outcome="review")
