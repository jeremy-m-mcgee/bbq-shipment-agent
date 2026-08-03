"""Reporting invocation metrics back to LaunchDarkly.

Design 6.1 justifies the medium split partly on "per-variant metric
attribution without a code change", and design 6.2 gives every agent a metric
it is supposed to move. Neither is worth anything unless something reports
what happened, which is this module.

## Built from A1's identity, not from a second lookup

`LDAIClient.agent_config()` returns a tracker alongside the config, and using
it would mean re-evaluating the AI Config at invocation time. That would
attribute the metric to whatever the console is serving now rather than to the
text the agent actually ran on -- the same confusion the instruction hash
exists to prevent. The tracker is therefore constructed directly from the
variation key and version captured at run start.

## A metrics failure never fails a run

Every call here is best-effort. LaunchDarkly being unreachable is design
6.10's normal path, and a run that planned a shipment correctly should not be
reported as failed because a telemetry event could not be delivered.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..agent_configs import AgentConfig
from ..context import to_ld_context


class AgentMetrics(Protocol):
    """Where an invocation's outcome is reported.

    `begin` returns a reporter for *one* invocation. That is not ceremony:
    `LDAIConfigTracker` records tokens and success once and then refuses,
    logging "already recorded on this tracker. Call create_tracker on the AI
    Config for a new run". A conversational agent makes one invocation per
    operator turn, so a single tracker held for a whole review reports the
    first turn and silently drops every one after it — which is the opposite
    of what design 8's operator-edit-count metric needs.
    """

    def begin(self) -> AgentMetrics: ...
    def track_success(self) -> None: ...
    def track_error(self) -> None: ...
    def track_tokens(self, input_tokens: int, output_tokens: int) -> None: ...


class NoMetrics:
    """Reports nowhere. The default, and what every test uses.

    Not a degraded path: an offline run has no LaunchDarkly to report to, and
    the ledger already records the invocation. This only gives up the console
    -side comparison between instruction variations.
    """

    def begin(self) -> NoMetrics:
        return self

    def track_success(self) -> None: ...
    def track_error(self) -> None: ...
    def track_tokens(self, input_tokens: int, output_tokens: int) -> None: ...


class LaunchDarklyMetrics:
    """Reports to LaunchDarkly against the variation A1 captured.

    Holds a factory rather than a tracker: see `AgentMetrics.begin`. A tracker
    is single-use, so one is made per invocation and discarded.
    """

    def __init__(self, make_tracker: Any, tracker: Any = None) -> None:
        self._make_tracker = make_tracker
        self._tracker = tracker if tracker is not None else make_tracker()

    def begin(self) -> LaunchDarklyMetrics:
        return LaunchDarklyMetrics(self._make_tracker)

    def track_success(self) -> None:
        self._safely(lambda: self._tracker.track_success())

    def track_error(self) -> None:
        self._safely(lambda: self._tracker.track_error())

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

    @staticmethod
    def _safely(call) -> None:
        try:
            call()
        except Exception:  # noqa: BLE001 - see the module docstring
            pass


def launchdarkly_metrics(
    client: Any, run: Any, config: AgentConfig, provider: str = "anthropic"
) -> AgentMetrics:
    """A tracker for one agent on one run, or `NoMetrics` if one cannot be made.

    Returns `NoMetrics` rather than raising when the config carries no
    variation identity: a cached snapshot with a missing variation key is
    still perfectly usable for *running* the agent, and losing the metric is a
    smaller loss than losing the run.
    """
    if client is None or not config.variation_key or config.version is None:
        return NoMetrics()

    from itertools import count

    from ldai.tracker import LDAIConfigTracker

    from ..agent_configs import LD_CONFIGURED_STAGES

    invocations = count(1)

    def make_tracker() -> Any:
        return LDAIConfigTracker(
            ld_client=client,
            # LaunchDarkly's `run_id` identifies one AI invocation, not one
            # pipeline run, and a repeated value makes the tracker refuse to
            # record. Our ledger run id is the prefix so a metric in the
            # console is still greppable back to a line in the ledger.
            run_id=f"{run.run_id}:{config.agent_key}:{next(invocations)}",
            config_key=config.agent_key,
            variation_key=config.variation_key,
            version=config.version,
            context=to_ld_context(
                run.context_for_stage(LD_CONFIGURED_STAGES[config.agent_key])
            ),
            model_name=config.model or "unknown",
            provider_name=provider,
        )

    try:
        return LaunchDarklyMetrics(make_tracker)
    except Exception:  # noqa: BLE001 - see the module docstring
        return NoMetrics()
