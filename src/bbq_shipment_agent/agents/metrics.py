"""Reporting invocation outcomes to LaunchDarkly, using the SDK's tracker.

Design 6.1 justifies the medium split partly on "per-variant metric
attribution without a code change", and 6.2 gives each configured stage a
metric it is supposed to move.

## This file has been wrong twice, in opposite directions

The first version hand-built `LDAIConfigTracker` and reimplemented the SDK's
`create_tracker` badly: one tracker held for a whole conversation, then a
home-grown run-id counter bolted on when that failed. A tracker records
tokens and success exactly once and then refuses, so the first invocation
reported and the rest were dropped with a warning. That was debugged twice,
in live runs, before the warning was recognised as the SDK saying *call
create_tracker*.

The second version fixed that and then hand-rolled the layer above it: its
own `perf_counter` for duration, its own `TokenUsage` construction, its own
`track_success` / `track_error` calls. All of that is what
`tracker.track_metrics_of(extractor, func)` does, and it is the tracking half
of the integration LaunchDarkly's own guides teach -- the same guides whose
other half (call the provider yourself, with config served by LD) `model.py`
already follows. So it is gone.

**What is left here is only what the SDK does not do**, which is the test to
apply before adding anything back.

## What `record` reports, and what each number means

One call to `record` is one invocation. It wraps the whole thing -- including
a tool loop and any retries -- and the SDK derives four metrics from it:

**Duration** is wall clock for the wrapped work, **including tool
execution**. That is deliberate and it is the number the operator
experiences: a B3 instruction variation that calls the validator six times
before answering is genuinely slower than one that calls it twice, and
timing only the model round trips would hide exactly the difference design
6.2 wants attributed per variant. The ledger's `iterations` and
`tools_called` are what separate the two halves after the fact.

**Success or error** comes from `Outcome.success`, and it is about the
invocation rather than the answer: an agent that correctly reports six
blockers did its job. When the wrapped work raises, the SDK records an error
and the duration up to the failure without the extractor running at all.

**Tokens** are summed across the invocation by the caller, because only the
caller knows which completions belonged to it.

**Tool calls** are reported in order with repeats kept, from the same list
the ledger's `tools_called` holds. `Outcome.tools_called` left `None` -- or
empty -- reports nothing at all, which is B1 and D1: the ledger's own
distinction between a field this invocation knows nothing about and an empty
measurement. Note the SDK reports an empty list if you hand it one, so the
emptiness has to be dropped here rather than there.

## Time to first token is the one metric still measured by hand

It stays because nothing in the SDK produces it. `LDAIMetrics` has four
fields and TTFT is not among them, so no extractor can carry it, and
`track_metrics_of` covers duration, tokens and success/error only.
LaunchDarkly owns the *channel* -- `track_time_to_first_token` is a real
metric on the tracker -- but the milliseconds have to come from whoever
watched the stream.

That is why `AnthropicModel` streams: a non-streaming call returns the whole
message at once and has no first token to time. In a tool loop the first
measured value wins, since the operator's wait began with the first call.
Anything replayed from a fixture reports nothing rather than zero -- an
unmeasured latency and an instant one must not look the same in a chart.

## What is deliberately not reported

`track_feedback` is the one built-in metric left unwired, and the reason is
design 8 rather than effort. The feedback the SDK means is a judgement on
*this invocation's output*, and the only operator judgement this system
collects is D2's terminal state -- approved, approved with exclusions,
rejected. That is a verdict on the **plan**, which C5 computed and the
narrator is forbidden from participating in (design 6.3). Wiring it would
attribute a rejected plan to whichever instruction variation happened to
describe it. Design 8 already names the right metric for `review-narrator`
-- operator edit count -- and that is a custom metric, not this one.

## A metrics failure never fails a run

Reporting is best-effort. LaunchDarkly being unreachable is design 6.10's
normal path, and a run that planned a shipment correctly must not be
reported failed because a telemetry event could not be delivered. The
wrapped work's own exceptions still propagate -- those are the caller's --
but a failure in the reporting around it is swallowed, and a result the work
already produced is never lost to one. The ledger is the durable record
either way: `agent_invocations` carries the variation key, version,
instruction hash, model, iterations and outcome, committed and offline. This
is the console view on top of that, not a substitute for it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from ..agent_configs import AgentConfig

T = TypeVar("T")


@dataclass(frozen=True)
class Outcome:
    """What one invocation produced, in the shape LaunchDarkly's metrics want.

    Deliberately not the SDK's `LDAIMetrics`: that type is constructed at the
    reporting boundary, and building it in four stages would put an `ldai`
    import in every one of them. This carries the same information in the
    vocabulary the stages already speak.
    """

    #: The invocation, not the answer. See the module docstring.
    success: bool
    input_tokens: int = 0
    output_tokens: int = 0
    #: In call order, repeats kept. `None` or empty reports nothing -- a
    #: stage with no tool loop must not claim it declined tools it never had.
    tools_called: Sequence[str] | None = None


class InvocationMetrics(Protocol):
    """Where one invocation's outcome is reported.

    `record` owns the clock, so there is no `track_duration`: the invocation
    is the callable it wraps, and its duration is that callable's. A caller
    timing its own work would be a second opinion about when an invocation
    began, which is how two stages end up measuring different things and
    calling them the same metric.
    """

    def record(self, work: Callable[[], T], outcome: Callable[[T], Outcome]) -> T: ...
    def track_time_to_first_token(self, milliseconds: float | None) -> None: ...


class NoMetrics:
    """Reports nowhere. Offline runs, and every test.

    Not a degraded path: an offline run has no LaunchDarkly to report to, and
    the ledger already holds the invocation. `record` still runs the work --
    it is the invocation, not the reporting.
    """

    def record(self, work: Callable[[], T], outcome: Callable[[T], Outcome]) -> T:
        return work()

    def track_time_to_first_token(self, milliseconds: float | None) -> None: ...


class SdkMetrics:
    """Wraps one `LDAIConfigTracker` from the SDK's factory.

    Thin on purpose. Two things are added on top of `track_metrics_of`, and
    both are about staying quiet. A failure to *report* cannot propagate, and
    cannot discard a result the work already produced -- `track_metrics_of`
    tracks after the call returns, so a telemetry error there would otherwise
    lose the value. And a repeat of an at-most-once metric is dropped here
    rather than at the tracker, which logs a warning about it. The tracker is
    right to warn -- a repeat usually means someone reused a tracker across
    invocations -- but a tool loop legitimately produces one completion per
    iteration and only the first carries a first-token time, so forwarding all
    of them would print a warning per iteration for behaviour that is correct.
    """

    def __init__(self, tracker: Any) -> None:
        self._tracker = tracker
        self._reported_first_token = False

    def record(self, work: Callable[[], T], outcome: Callable[[T], Outcome]) -> T:
        """Run `work`, and report what `outcome` says about its result.

        Duration, success/error, tokens and tool calls all come from the SDK
        here. An exception from `work` propagates, having been recorded as an
        error -- that is the caller's failure and the caller handles it.
        """
        produced: list[T] = []

        def _work() -> T:
            result = work()
            produced.append(result)
            return result

        try:
            return self._tracker.track_metrics_of(
                lambda result: _ld_metrics(outcome(result)), _work
            )
        except Exception:
            if produced:  # the work succeeded; the reporting did not
                return produced[0]
            raise

    def track_time_to_first_token(self, milliseconds: float | None) -> None:
        """The first measured first-token latency of the invocation.

        `None` is the normal answer from a replayed completion and reports
        nothing: design 6.10 makes offline a normal path, and a zero here
        would put a fictional latency in the same chart as real ones.
        """
        if milliseconds is None or self._reported_first_token:
            return
        self._reported_first_token = True
        with contextlib.suppress(Exception):  # see the module docstring
            self._tracker.track_time_to_first_token(round(milliseconds))


def _ld_metrics(outcome: Outcome) -> Any:
    """`Outcome` in the SDK's vocabulary, built at the boundary and nowhere else."""
    from ldai.providers.types import LDAIMetrics
    from ldai.tracker import TokenUsage

    called = list(outcome.tools_called or ())
    return LDAIMetrics(
        success=outcome.success,
        tokens=TokenUsage(
            total=outcome.input_tokens + outcome.output_tokens,
            input=outcome.input_tokens,
            output=outcome.output_tokens,
        ),
        # Empty means "no tool loop", not "declined every tool": the SDK
        # reports whatever list it is handed, so the distinction is made here.
        tool_calls=called or None,
    )


def metrics_for(config: AgentConfig | None) -> InvocationMetrics:
    """A reporter for **one** invocation, from the config captured at A1.

    Call once per invocation and discard. `NoMetrics` when the config came
    from the snapshot or from nowhere, which is exactly when there is no
    LaunchDarkly to report to.
    """
    factory = getattr(config, "create_tracker", None)
    if factory is None:
        return NoMetrics()
    try:
        return SdkMetrics(factory())
    except Exception:  # noqa: BLE001 - telemetry never fails a run
        return NoMetrics()
