"""Scoring D2's narrations with a LaunchDarkly judge.

Design 8 asks every LaunchDarkly-configured stage for a metric that says
whether it is doing its job. `review-narrator` had none that could be
computed: design 6.2 names "operator edit count", which is a custom metric
nobody wired, and design 10's own evidence -- the narrator naming the wrong
recipient for two turns -- was found by a human reading a transcript.

This is that metric. A judge scores every narration for whether it belongs in
the review at all, and the score lands on the invocation's ledger line and on
LaunchDarkly's monitoring for the judge config.

## It observes and does not intervene

Nothing here changes what the operator sees. That is deliberate, and it is a
narrowing of what this started as.

An earlier version withheld a low-scoring narration behind a canned refusal.
It was removed after five live turns produced no suppressions at all: asked a
leetcode question, `review-narrator` declines it *itself*, and the decline --
which talks about carriers, costs and thermal margins -- is genuinely in scope
and correctly scores 1.0. The narrator held even when LaunchDarkly served it
Haiku instead of Sonnet 5, which was the exact scenario the suppression was
justified by. So the suppression path guarded a failure with a measured rate
of zero, at the cost of a doubled turn latency, no streaming, and a refusal an
operator could not override.

What is left is the part that earns its keep: a number per narration, so the
day a console edit or a new variation drops the scope discipline from
`review-narrator`'s instructions, something notices. Design 6.1 makes that
text editable without a deploy; this is the measurement that makes the risk
visible rather than theoretical.

## Failure is silence, never noise

Every failure path -- no judge, a judge that raises, a sampled-out result, a
missing score -- records what happened and returns. A scorer that could break
a review would be a worse trade than no scorer, since the thing it produces is
a metric rather than a safeguard.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent_configs import AgentConfig
from ..run import Run, record_agent_invocation

#: The judge AI Config, created in LaunchDarkly in judge mode. LaunchDarkly
#: derives the key rather than taking one, so this follows the console.
JUDGE_KEY = "narration-scope-relevance"

#: Prior exchanges handed to the judge as context. Enough for a terse
#: follow-up to be recognisable as one -- "why?" is in scope only if the judge
#: can see what was asked -- and not the whole review, which would make every
#: score more expensive as the conversation grew.
HISTORY_TURNS = 4


@dataclass(frozen=True)
class Score:
    """One narration's score, and what the ledger records about it."""

    #: `in_scope` | `out_of_scope` | `error`. A fixed vocabulary, because it
    #: goes in the ledger and the ledger takes no model-authored text.
    outcome: str
    value: float | None = None
    #: Model-authored. Reaches LaunchDarkly and the log, never the ledger.
    reasoning: str = ""


#: Below this a narration is off-topic. Only a label on the ledger line -- it
#: gates nothing -- so it is a reporting convenience rather than control flow.
#: Live scores so far are 0.0 or 1.0 with nothing between, so it is nowhere
#: near a real boundary; that would change if the rubric ever became graded.
IN_SCOPE_AT = 0.5


class NarrationScorer:
    """Scores narrations with an injected `Judge`, and records the result.

    The judge is injected because `wiring.py` is where things that open
    sockets are built. `None` is the ordinary state rather than a failure: the
    judge config is disabled, not targeted at this run, or LaunchDarkly is
    unreachable. Scoring simply does not happen, and the review is unaffected
    either way.
    """

    def __init__(
        self, run: Run, judge: Any | None, *, ledger_root: Path | str
    ) -> None:
        self.run = run
        self.ledger_root = ledger_root
        self._judge = judge

    @property
    def active(self) -> bool:
        """Whether narrations will be scored.

        LaunchDarkly's answer rather than a flag's: `create_judge` returns None
        when its config is disabled or untargeted, so enabling and targeting
        the judge is the whole of the on switch. A flag beside it would be a
        second control over the same thing.
        """
        return self._judge is not None

    def score(self, history: str, reply: str) -> Score | None:
        """Score one narration. Never raises."""
        if not self.active:
            return None

        result = self._evaluate(history, reply)
        if result is None:
            return self._record(Score(outcome="error"))

        self._track(result)

        value = getattr(result, "score", None)
        if not getattr(result, "success", False) or value is None:
            # Sampled out, errored, or unparseable. The SDK logged it already.
            return self._record(
                Score(outcome="error", reasoning=_reasoning(result))
            )

        return self._record(
            Score(
                outcome="in_scope" if value >= IN_SCOPE_AT else "out_of_scope",
                value=float(value),
                reasoning=_reasoning(result),
            )
        )

    def _evaluate(self, history: str, reply: str) -> Any | None:
        """`Judge.evaluate` across the async boundary.

        `asyncio.run` is safe at all three call sites -- the CLI is
        synchronous, the UI worker is its own thread, and the SSE route runs
        the turn on a daemon thread -- and a caller that did hold a running
        loop would raise here, which is a reason to record nothing rather than
        to fail a review.
        """
        try:
            return asyncio.run(self._judge.evaluate(history, reply))
        except Exception:  # noqa: BLE001 - scoring never fails a review
            return None

    def _track(self, result: Any) -> None:
        """Send the score to LaunchDarkly. Best effort, like every metric.

        Programmatic judges do not report their own: the docs are explicit
        that `create_judge` "doesn't automatically emit monitoring metrics",
        and only judges attached to a completion-mode config are dispatched
        for you. Every config here is agent mode, so this is the only path.
        """
        with contextlib.suppress(Exception):  # telemetry never fails a run
            self._judge.get_ai_config().create_tracker().track_judge_result(result)

    def _record(self, score: Score) -> Score:
        """One ledger line per scored narration, carrying the number only.

        The reasoning is model-authored free text and the ledger is committed
        and append-only -- the same rule that sends every capability value
        through a `StrEnum`. A bounded float cannot carry an address.

        `tools_offered` / `tools_called` are omitted rather than empty: a judge
        has no tool loop, like B1 and D1, and design 7 makes an absent key mean
        "this append knows nothing about the field".
        """
        with contextlib.suppress(Exception):  # a ledger failure is not a score
            record_agent_invocation(
                self.ledger_root,
                self.run,
                JUDGE_KEY,
                outcome=score.outcome,
                judge_score=score.value,
                config=self._identity(),
            )
        return score

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
