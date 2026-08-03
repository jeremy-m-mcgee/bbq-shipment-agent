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

## A metrics failure never fails a run

Every call is best-effort. LaunchDarkly being unreachable is design 6.10's
normal path, and a run that planned a shipment correctly must not be
reported failed because a telemetry event could not be delivered. The ledger
is the durable record either way -- `agent_invocations` carries the variation
key, version, instruction hash, model, iterations and outcome, committed and
offline. This is the console view on top of that, not a substitute for it.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..agent_configs import AgentConfig


class InvocationMetrics(Protocol):
    """Where one invocation's outcome is reported."""

    def track_tokens(self, input_tokens: int, output_tokens: int) -> None: ...
    def track_success(self) -> None: ...
    def track_error(self) -> None: ...


class NoMetrics:
    """Reports nowhere. Offline runs, and every test.

    Not a degraded path: an offline run has no LaunchDarkly to report to, and
    the ledger already holds the invocation.
    """

    def track_tokens(self, input_tokens: int, output_tokens: int) -> None: ...
    def track_success(self) -> None: ...
    def track_error(self) -> None: ...


class SdkMetrics:
    """Wraps one `LDAIConfigTracker` from the SDK's factory.

    Thin on purpose. The only thing this adds is that a failure to report
    cannot propagate -- see the module docstring.
    """

    def __init__(self, tracker: Any) -> None:
        self._tracker = tracker

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
    """
    factory = getattr(config, "create_tracker", None)
    if factory is None:
        return NoMetrics()
    try:
        return SdkMetrics(factory())
    except Exception:  # noqa: BLE001 - telemetry never fails a run
        return NoMetrics()
