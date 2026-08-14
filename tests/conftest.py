"""Shared test helpers.

`CapabilityGate` is the test stand-in for the live flag seam. Capabilities are
evaluated live per stage now, so a test that wants a run under a particular
capability set injects a gate serving those values by name rather than writing
a config file.
"""

from __future__ import annotations

from bbq_shipment_agent.capabilities import (
    CAPABILITIES,
    GUARD,
    KILL_SWITCH_FLAG,
    FlagEvaluation,
)

#: Every capability a test can name, including the ones deliberately outside
#: `CAPABILITIES`. `guard` is not registered because it is read at D2, after
#: the fingerprint is folded -- but it is still evaluated through the same
#: gate, so a test has to be able to serve it.
_FLAG_KEYS = {name: cap.flag_key for name, cap in CAPABILITIES.items()} | {
    GUARD.name: GUARD.flag_key
}


class CapabilityGate:
    """A `FlagGate` that serves fixed capability values, keyed by name.

    `CapabilityGate(verification="on", planner="shadow")` serves those two and
    the code default for anything else; `kill=True` engages the kill switch.
    Values are passed by capability name (``planner``), not flag key
    (``planner-mode``), so a test reads the way the spine does.
    """

    def __init__(self, *, kill: bool = False, reason: str = "RULE_MATCH:test", **caps):
        self._values: dict[str, object] = {KILL_SWITCH_FLAG: kill}
        for name, value in caps.items():
            self._values[_FLAG_KEYS[name]] = value
        self._reason = reason

    def evaluate(self, flag_key, context, default):
        if flag_key in self._values:
            return FlagEvaluation(self._values[flag_key], "launchdarkly", self._reason)
        return FlagEvaluation(default, "launchdarkly", "FALLTHROUGH")
