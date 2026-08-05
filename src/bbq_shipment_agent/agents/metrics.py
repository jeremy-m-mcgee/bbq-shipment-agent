"""Reporting invocation outcomes to LaunchDarkly, using the SDK's tracker.

Design 6.1 justifies the medium split partly on "per-variant metric
attribution without a code change", and 6.2 gives each configured stage a
metric it is supposed to move.

## This file existed once before and was deleted, deliberately

The first version hand-built `LDAIConfigTracker` and reimplemented the SDK's
`create_tracker` badly: one tracker held for a whole conversation, then a
home-grown run-id counter bolted on when that failed. A tracker records
tokens and success exactly once and then refuses, so the first invocation
reported and the rest were dropped with a warning. That was debugged twice,
in live runs, before the warning was recognised as the SDK saying *call
create_tracker*.

It is back because the factory now comes from `LDAIClient.agent_config()` --
see `agent_configs.LaunchDarklyAgentConfigs` -- so there is nothing left to
reimplement. `AgentConfig.create_tracker` is that factory. Each call mints a
fresh UUIDv4 run id, which is how LaunchDarkly correlates one AI
invocation's events, and mixing two run ids in one view is meaningless.

The rule that follows: **one tracker per invocation, never per run, never
per conversation.** For `review-narrator` an invocation is one operator
turn, which may span several model calls; the tokens are summed across the
turn and reported once, because that is the unit the ledger's `iterations`
counts too.

## What is reported, and what each number means

Tokens, success and error were here first. Three more follow the same
at-most-once-per-tracker rule the SDK enforces:

**Duration** is the whole invocation, wall clock, **including tool
execution**. That is deliberate and it is the number the operator
experiences: a B3 instruction variation that calls the validator six times
before answering is genuinely slower than one that calls it twice, and
timing only the model round trips would hide exactly the difference design
6.2 wants attributed per variant. The ledger's `iterations` and
`tools_called` are what separate the two halves after the fact.

**Time to first token** is the first token of the first model call in the
invocation, which is the only reading of "first" that survives a tool loop.
Measuring it at all requires streaming -- a non-streaming call returns the
whole message at once and has no first token to time -- so `AnthropicModel`
streams. Anything replayed from a fixture reports nothing rather than
reporting zero: an unmeasured latency and an instant one must not look the
same in a chart.

**Tool calls** are reported per call, in order, repeats kept, from the same
list the ledger's `tools_called` holds. B1 and D1 have no tool loop and
report nothing at all, which is the ledger's own distinction between a field
this invocation knows nothing about and an empty measurement.

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

Every call is best-effort. LaunchDarkly being unreachable is design 6.10's
normal path, and a run that planned a shipment correctly must not be
reported failed because a telemetry event could not be delivered. The ledger
is the durable record either way -- `agent_invocations` carries the variation
key, version, instruction hash, model, iterations and outcome, committed and
offline. This is the console view on top of that, not a substitute for it.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Protocol

from ..agent_configs import AgentConfig


class InvocationMetrics(Protocol):
    """Where one invocation's outcome is reported.

    `track_duration` takes no arguments because the reporter owns the clock:
    it starts when `metrics_for` mints it, which is the top of the
    invocation at all four call sites, and stops when the invocation ends.
    A caller passing its own elapsed time would be a second opinion about
    when an invocation began, which is how two stages end up measuring
    different things and calling them the same metric.
    """

    def track_tokens(self, input_tokens: int, output_tokens: int) -> None: ...
    def track_duration(self) -> None: ...
    def track_time_to_first_token(self, milliseconds: float | None) -> None: ...
    def track_tools_called(self, names: Iterable[str]) -> None: ...
    def track_success(self) -> None: ...
    def track_error(self) -> None: ...


class NoMetrics:
    """Reports nowhere. Offline runs, and every test.

    Not a degraded path: an offline run has no LaunchDarkly to report to, and
    the ledger already holds the invocation.
    """

    def track_tokens(self, input_tokens: int, output_tokens: int) -> None: ...
    def track_duration(self) -> None: ...
    def track_time_to_first_token(self, milliseconds: float | None) -> None: ...
    def track_tools_called(self, names: Iterable[str]) -> None: ...
    def track_success(self) -> None: ...
    def track_error(self) -> None: ...


class SdkMetrics:
    """Wraps one `LDAIConfigTracker` from the SDK's factory.

    Thin on purpose. Two things are added on top of the tracker, and both are
    about staying quiet: a failure to report cannot propagate, and a repeat of
    an at-most-once metric is dropped here rather than at the tracker, which
    logs a warning about it. The tracker is right to warn -- a repeat usually
    means someone reused a tracker across invocations -- but a tool loop
    legitimately produces one completion per iteration and only the first
    carries a first-token time, so forwarding all of them would print a
    warning per iteration for behaviour that is correct.
    """

    def __init__(self, tracker: Any) -> None:
        self._tracker = tracker
        self._started = time.perf_counter()
        self._reported_duration = False
        self._reported_first_token = False

    def track_tokens(self, input_tokens: int, output_tokens: int) -> None:
        from ldai.tracker import TokenUsage

        self._safely(
            lambda: self._tracker.track_tokens(
                TokenUsage(
                    total=input_tokens + output_tokens,
                    input=input_tokens,
                    output=output_tokens,
                )
            )
        )

    def track_duration(self) -> None:
        """Elapsed since this reporter was minted. Once, when the run ends."""
        if self._reported_duration:
            return
        self._reported_duration = True
        elapsed = round((time.perf_counter() - self._started) * 1000)
        self._safely(lambda: self._tracker.track_duration(elapsed))

    def track_time_to_first_token(self, milliseconds: float | None) -> None:
        """The first measured first-token latency of the invocation.

        `None` is the normal answer from a replayed completion and reports
        nothing: design 6.10 makes offline a normal path, and a zero here
        would put a fictional latency in the same chart as real ones.
        """
        if milliseconds is None or self._reported_first_token:
            return
        self._reported_first_token = True
        self._safely(lambda: self._tracker.track_time_to_first_token(round(milliseconds)))

    def track_tools_called(self, names: Iterable[str]) -> None:
        """One event per call, in order, repeats kept -- as the ledger has it."""
        called = list(names)
        if not called:
            return
        self._safely(lambda: self._tracker.track_tool_calls(called))

    def track_success(self) -> None:
        self._safely(self._tracker.track_success)

    def track_error(self) -> None:
        self._safely(self._tracker.track_error)

    @staticmethod
    def _safely(call: Any) -> None:
        try:
            call()
        except Exception:  # noqa: BLE001 - see the module docstring
            pass


def metrics_for(config: AgentConfig | None) -> InvocationMetrics:
    """A reporter for **one** invocation, from the config captured at A1.

    Call once per invocation and discard. `NoMetrics` when the config came
    from the snapshot or from nowhere, which is exactly when there is no
    LaunchDarkly to report to.

    **Call it at the top of the invocation**, not just once within it: this is
    where the duration clock starts, so a reporter minted after the first
    model call would report a latency the operator never waited.
    """
    factory = getattr(config, "create_tracker", None)
    if factory is None:
        return NoMetrics()
    try:
        return SdkMetrics(factory())
    except Exception:  # noqa: BLE001 - telemetry never fails a run
        return NoMetrics()
