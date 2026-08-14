"""D2's scope guard: does the narration belong in this review at all?

`review-narrator`'s served instructions cover faithfulness and authority and
say nothing about a message unrelated to shipping, so it will answer a leetcode
problem. This judges the **response** rather than the question, which is the
stronger requirement: an in-scope question can still produce a wandering
answer, and only checking the output catches both.

## The SDK does the scoring

`LDAIClient.create_judge` returns a `Judge`, and `judge.evaluate(history,
reply)` owns the model call, the prompt framing, the structured-output schema
and the 0.0-1.0 validation. Nothing here reimplements any of it -- that was an
earlier draft of this design and it duplicated the SDK for no benefit.

What is left is the part LaunchDarkly deliberately does not do: deciding. The
judges documentation is explicit that a score becomes a guardrail only when the
application acts on it, and design 6.1 keeps that decision in Python. So the
threshold is a constant here, and the mode -- `off` / `shadow` / `enforce` --
is the flag LaunchDarkly serves.

## Everything that is not a clear refusal passes

A guard that blanks a legitimate narration is worse than the leetcode answer it
exists to prevent, because the operator has no override and no way to see why.
So the only path that suppresses is: the judge ran, returned a score, the score
is below the threshold, and the mode is `enforce`. An unavailable judge, a
sampled-out result, a model error, a missing score -- all pass through and are
recorded. `Judge.evaluate` never raises, so this is the SDK's default too.

## Why the judge's own tracker carries the score

Programmatic judges do not report their own metric: the docs say `create_judge`
"doesn't automatically emit monitoring metrics", and only judges *attached* to a
config get dispatched by `ManagedAgent`. So the score is tracked here.

It goes on the judge config's tracker rather than the narrator's. Attaching it
to `review-narrator`'s tracker would attribute the score to the narration's
variation, which is what design 6.2 would eventually want -- "does variation X
wander more often" is the question worth asking. Doing that means reaching the
raw `LDAIConfigTracker` out of `InvocationMetrics`, which is a two-method
protocol on purpose. Deferred rather than dismissed: the metric key is the
same either way, and only the grouping differs.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent_configs import AgentConfig
from ..capabilities import GuardMode
from ..run import Run, record_agent_invocation

#: The judge AI Config. Created in LaunchDarkly in judge mode, with an
#: `evaluationMetricKey` -- `Judge.evaluate` returns early without one.
JUDGE_KEY = "narration-scope-relevance"

#: Below this, a narration is out of scope. A Python constant because a
#: threshold is control flow (design 6.1), and because it should be set from
#: the score distribution a `shadow` run produces rather than guessed in a
#: console field.
THRESHOLD = 0.5

#: What the operator sees instead of the narration. Python's words, not the
#: model's: a refusal the model composes is one it can be talked out of.
REFUSAL = (
    "That answer was outside this review, so it was withheld. I can only help "
    "with this run's manifest — costs, carriers, ship dates, thermal margins, "
    "who was escalated or suppressed, and edits to the plan."
)


@dataclass(frozen=True)
class Verdict:
    """What the judge said, and what this run does about it."""

    #: Whether the narration may be shown. False only on a real refusal.
    suppress: bool
    #: `in_scope` | `out_of_scope` | `error`. The ledger's fixed vocabulary:
    #: what the judge found, never what the mode did about it, which is
    #: recoverable from the `capability_evaluations` line.
    outcome: str
    score: float | None = None
    #: Model-authored. Reaches the screen and LaunchDarkly, never the ledger.
    reasoning: str = ""


class ScopeGuard:
    """Scores one narration and decides whether the operator sees it.

    Holds an injected `Judge` because `wiring.py` is where things that open
    sockets are built. `None` is a normal state, not a failure: it means the
    mode is `off`, LaunchDarkly is unreachable, no provider package is
    installed, or the judge config is disabled -- and all of them mean the same
    thing here, which is that narrations pass.
    """

    def __init__(
        self,
        run: Run,
        judge: Any | None,
        *,
        ledger_root: Path | str,
        mode: GuardMode,
    ) -> None:
        self.run = run
        self.ledger_root = ledger_root
        self.mode = mode
        self._judge = judge

    @property
    def active(self) -> bool:
        """Whether a narration will be scored.

        LaunchDarkly\'s answer, not the flag\'s: a judge exists when its config
        is enabled and targeted at this run, and `create_judge` returns None
        when it is not. Scoring is the judge config\'s business; the flag only
        decides what is done with the score.
        """
        return self._judge is not None

    @property
    def enforcing(self) -> bool:
        """Whether a low score will withhold the narration.

        Read by the front-ends as well: streaming and enforcement are mutually
        exclusive, because a reply that must be judged before the operator sees
        it cannot already be painted on their screen.
        """
        return self.active and self.mode is GuardMode.ON

    def check(self, history: str, reply: str) -> Verdict:
        """Score one narration. Never raises, and never blocks on a failure."""
        if not self.active:
            return Verdict(suppress=False, outcome="in_scope")

        result = self._evaluate(history, reply)
        if result is None:
            return self._record(Verdict(suppress=False, outcome="error"))

        self._track(result)

        score = getattr(result, "score", None)
        if not getattr(result, "success", False) or score is None:
            # Sampled out, errored, or unparseable. The SDK already logged it;
            # a narration is not withheld because a grader had a bad day.
            return self._record(
                Verdict(suppress=False, outcome="error", reasoning=_reasoning(result))
            )

        in_scope = score >= THRESHOLD
        return self._record(
            Verdict(
                suppress=not in_scope and self.mode is GuardMode.ON,
                outcome="in_scope" if in_scope else "out_of_scope",
                score=float(score),
                reasoning=_reasoning(result),
            )
        )

    def _evaluate(self, history: str, reply: str) -> Any | None:
        """`Judge.evaluate` across the async boundary.

        `asyncio.run` is safe at all three call sites -- the CLI is
        synchronous, the UI worker is its own thread, and the SSE route already
        runs the turn on a daemon thread -- but a caller that *does* hold a
        running loop would get a `RuntimeError` rather than a scored turn, and
        that is a reason to pass rather than to fail.
        """
        try:
            return asyncio.run(self._judge.evaluate(history, reply))
        except Exception:  # noqa: BLE001 - a grader never fails a review
            return None

    def _track(self, result: Any) -> None:
        """Send the score to LaunchDarkly. Best effort, like every metric."""
        with contextlib.suppress(Exception):  # telemetry never fails a run
            self._judge.get_ai_config().create_tracker().track_judge_result(result)

    def _record(self, verdict: Verdict) -> Verdict:
        """One ledger line per scored narration, carrying the score only.

        The reasoning is model-authored free text and the ledger is committed
        and append-only, so it does not go here -- the same rule that makes
        every capability value pass through a `StrEnum`. A bounded float
        cannot carry an address.

        `tools_offered` / `tools_called` are omitted rather than empty: a judge
        has no tool loop, like B1 and D1, and design 7 makes an absent key mean
        "this append knows nothing about the field".
        """
        with contextlib.suppress(Exception):  # a ledger failure is not a refusal
            record_agent_invocation(
                self.ledger_root,
                self.run,
                JUDGE_KEY,
                outcome=verdict.outcome,
                judge_score=verdict.score,
                config=self._identity(),
            )
        return verdict

    def _identity(self) -> AgentConfig:
        """What the ledger records this judge as.

        A judge is not in `run.agent_configs`, so the usual lookup finds
        nothing and the config is passed explicitly. `variation_key` and
        `version` stay unset: the SDK's judge config does not expose them, the
        same gap `agent_configs` works around for agents by reading the raw
        variation. The model and the instruction hash still pin what ran.
        """
        model = None
        instructions = None
        with contextlib.suppress(Exception):
            served = self._judge.get_ai_config()
            model = getattr(getattr(served, "model", None), "name", None)
            instructions = "\n\n".join(
                m.content for m in (getattr(served, "messages", None) or [])
            )
        return AgentConfig(
            agent_key=JUDGE_KEY,
            enabled=True,
            model=model,
            instructions=instructions or None,
            source="launchdarkly",
            reason="JUDGE",
        )


def _reasoning(result: Any) -> str:
    return str(getattr(result, "reasoning", "") or "")
