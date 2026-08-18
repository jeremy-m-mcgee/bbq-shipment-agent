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
threshold is a constant here, and enabling and targeting the judge config is
the whole of the on switch -- there is no flag beside it.

## The judge is looked up per turn, not held

`create_judge` evaluates the config once and freezes it into the `Judge`:
`enabled`, the model, the rubric and the targeting are all read at construction
and never again. A guard holding one instance for the life of a review is
therefore deaf to LaunchDarkly for the whole review -- a console edit lands on
the next *run*, which is the opposite of what design 6.1 buys by putting the
rubric in a console at all.

So this holds a *factory* and calls it at the top of every scored turn. The
cost is one flag evaluation per turn, against a review that is a handful of
turns long, and the SDK is reading a locally cached ruleset rather than making
a network round trip. What it buys is that disabling the judge mid-review stops
the scoring on the next turn, and a rubric edit is live on the next turn too.

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

    #: Whether the narration is withheld. True only on a real refusal: the
    #: judge ran, returned a score, and the score was below the threshold.
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

    Holds an injected *factory* rather than a `Judge`, because `wiring.py` is
    where things that open sockets are built and because a held instance cannot
    see a console edit (see the module docstring). `None` is a normal state,
    not a failure: it means there is no live LaunchDarkly client at all, and so
    there can never be a judge on this run.
    """

    def __init__(
        self,
        run: Run,
        judge_factory: Any | None,
        *,
        ledger_root: Path | str,
    ) -> None:
        self.run = run
        self.ledger_root = ledger_root
        #: Called once per scored turn. Returns a `Judge`, or None when the
        #: config is disabled, untargeted, unreachable, or has no provider
        #: package -- all of which mean narrations pass.
        self._judge_factory = judge_factory

    @property
    def enforcing(self) -> bool:
        """Whether this review *may* withhold a narration.

        Deliberately the weaker claim. Whether a given turn is scored is
        LaunchDarkly's to decide and is asked per turn, so the only thing
        knowable in advance is whether there is a client to ask -- and a
        property that answered from a stale judge would be the held-instance
        bug this class exists to avoid, moved somewhere harder to see.

        Read by the front-ends, because streaming and guarding are mutually
        exclusive: a reply that must be judged before the operator sees it
        cannot already be painted on their screen. The consequence of the
        weaker claim is that a live client suppresses streaming even on a run
        whose judge config turns out to be disabled.
        """
        return self._judge_factory is not None

    def check(self, history: str, reply: str) -> Verdict:
        """Score one narration. Never raises, and never blocks on a failure.

        The judge is resolved here rather than at construction, so a config
        disabled or re-targeted mid-review takes effect on this turn.
        """
        judge = self._resolve()
        if judge is None:
            return Verdict(suppress=False, outcome="in_scope")

        result = self._evaluate(judge, history, reply)
        if result is None:
            return self._record(Verdict(suppress=False, outcome="error"), judge)

        self._track(judge, result)

        score = getattr(result, "score", None)
        if not getattr(result, "success", False) or score is None:
            # Sampled out, errored, or unparseable. The SDK already logged it;
            # a narration is not withheld because a grader had a bad day.
            return self._record(
                Verdict(suppress=False, outcome="error", reasoning=_reasoning(result)),
                judge,
            )

        in_scope = score >= THRESHOLD
        return self._record(
            Verdict(
                suppress=not in_scope,
                outcome="in_scope" if in_scope else "out_of_scope",
                score=float(score),
                reasoning=_reasoning(result),
            ),
            judge,
        )

    def _resolve(self) -> Any | None:
        """This turn's judge, or None if there is not one.

        Never raises, for the reason `wiring.scope_judge` does not: a grader
        that cannot be built must not take the review down with it. A factory
        that throws is the same outcome as a disabled config -- no scoring.
        """
        if self._judge_factory is None:
            return None
        try:
            return self._judge_factory()
        except Exception:  # noqa: BLE001 - no judge is a normal state
            return None

    def _evaluate(self, judge: Any, history: str, reply: str) -> Any | None:
        """`Judge.evaluate` across the async boundary.

        `asyncio.run` is safe at all three call sites -- the CLI is
        synchronous, the UI worker is its own thread, and the SSE route already
        runs the turn on a daemon thread -- but a caller that *does* hold a
        running loop would get a `RuntimeError` rather than a scored turn, and
        that is a reason to pass rather than to fail.
        """
        try:
            return asyncio.run(judge.evaluate(history, reply))
        except Exception:  # noqa: BLE001 - a grader never fails a review
            return None

    def _track(self, judge: Any, result: Any) -> None:
        """Send the score to LaunchDarkly. Best effort, like every metric."""
        with contextlib.suppress(Exception):  # telemetry never fails a run
            judge.get_ai_config().create_tracker().track_judge_result(result)

    def _record(self, verdict: Verdict, judge: Any) -> Verdict:
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
                config=self._identity(judge),
            )
        return verdict

    def _identity(self, judge: Any) -> AgentConfig:
        """What the ledger records this judge as.

        A judge is not in `run.agent_configs`, so the usual lookup finds
        nothing and the config is passed explicitly. `variation_key` and
        `version` stay unset: the SDK's judge config does not expose them, the
        same gap `agent_configs` works around for agents by reading the raw
        variation. The model and the instruction hash still pin what ran.

        Taken from the judge that scored *this* turn rather than from the
        review, which is what makes a mid-review rubric edit legible: two turns
        of one review can now carry two different instruction hashes.
        """
        model = None
        instructions = None
        with contextlib.suppress(Exception):
            served = judge.get_ai_config()
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
