"""D2's voice: `review-narrator`. Design section 4, Phase D.

The conversational half of the review. `review.ReviewSession` is the other
half and owns every decision; this drives the model, runs whatever tools it
calls, and hands the answers back.

## Why this agent is cheap despite being a model in the loop

Design 2 makes the case, and it is the only place in the system where a model
gets this much rope:

> A model that explains a tradeoff carries almost none of the risk of a model
> that makes a decision, because it sits strictly downstream of a computed
> result and cannot alter it.

That holds here only because of what the tools do *not* let it do. It can read
the manifest, ask for a re-solve, and apply an edit the operator has confirmed.
It cannot approve, reject, exclude without a re-solve, or touch the thermal
gate. Every number it reports was computed upstream; design 6.3 requires every
claim to trace to a field, and the payload it reads is that field set.

## The tool loop, and why it is bounded

The model may call tools several times per turn — read the manifest, propose
an edit, read the manifest again to see what changed. `MAX_TOOL_ITERATIONS`
stops that being unbounded. It is not a safety property (the tools are the
safety property) but a cost and liveness one: a model that loops on
`read_manifest` forever should end the turn rather than the budget.

A tool that raises `ToolError` returns its message to the model as a tool
result rather than ending the turn. An operator asking to pin someone to a
date that is not a candidate should be told, in the conversation, not watch
the review die.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..context import STAGE_REVIEW_NARRATOR
from ..run import Run, record_agent_invocation
from .guard import REFUSAL, ScopeGuard
from .metrics import Outcome, metrics_for
from .model import ConversingModel, Invocation, ModelUnavailable
from .tools import Tool, ToolError, build_tools
from .verification import render_instructions

CONFIG_KEY = "review-narrator"

#: Tool round trips allowed inside one operator turn. See the module
#: docstring: a liveness bound, not a safety one.
MAX_TOOL_ITERATIONS = 8

#: Prior exchanges handed to the scope guard as context. Enough for a terse
#: follow-up to be recognisable as one; not the whole review, which would make
#: every judgement more expensive as the conversation grew.
HISTORY_TURNS = 4

#: What the narrator is handed to open with. Not instruction text -- that is
#: LaunchDarkly's -- but the turn that starts the conversation.
OPENING_PROMPT = (
    "Read the manifest and open the review. Narrate the tradeoff the solve "
    "produced, per your instructions. Do not propose any edit yet."
)


class NarratorUnavailable(Exception):
    """The narrator cannot run. D2 falls back to the plain rendered manifest."""


@dataclass
class Turn:
    """One exchange: what the operator said, what came back, what it ran."""

    prompt: str
    reply: str = ""
    tools_called: list[str] = field(default_factory=list)
    iterations: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    #: The scope guard withheld this narration and `reply` is the canned
    #: refusal rather than the model's answer. Carried so the review pane can
    #: say so instead of attributing Python's words to the narrator.
    refused: bool = False


class Narrator:
    """A live conversation over one `ReviewSession`.

    Holds the message history, which is why it is an object rather than a
    function: design 6.2 asks for "conversational, longer context", and a
    review is a sequence of turns over one manifest that keeps changing
    underneath it.
    """

    def __init__(
        self,
        run: Run,
        session: Any,
        *,
        ledger_root: Path | str,
        model: ConversingModel,
        guard: ScopeGuard | None = None,
    ) -> None:
        config = run.agent_configs.get(CONFIG_KEY)
        if config is None or not config.available:
            reason = (
                "no config was retrieved at run start"
                if config is None
                else f"{config.source}/{config.reason}"
            )
            raise NarratorUnavailable(f"{CONFIG_KEY}: {reason}")

        context = run.context_for_stage(STAGE_REVIEW_NARRATOR)
        self.invocation = Invocation.from_config(
            config, render_instructions(config, context)
        )
        self.run = run
        self.config = config
        self.session = session
        self.ledger_root = ledger_root
        self._model = model
        #: None is the ordinary state: the flag is off, LaunchDarkly is
        #: unreachable, or no judge config is served. All of them mean
        #: narrations pass.
        self._guard = guard
        self._tools: tuple[Tool, ...] = build_tools(CONFIG_KEY, session=session)
        self._by_name = {tool.name: tool for tool in self._tools}
        self.messages: list[dict[str, Any]] = []
        self.turns: list[Turn] = []

    @property
    def enforcing(self) -> bool:
        """Whether a narration will be withheld unless the judge clears it.

        Read by the front-ends, because enforcement and streaming cannot both
        happen: a reply that must be judged before the operator sees it cannot
        already be painted on their screen.
        """
        return self._guard is not None and self._guard.enforcing

    def open(self) -> Turn:
        """The opening narration. Design 4 requires it before any table.

        Deliberately not scope-guarded. `OPENING_PROMPT` is Python\'s text, not
        an operator\'s, and it asks for exactly the manifest narration the
        guard exists to protect -- so it is in scope by construction, and
        withholding it would leave the review with no opening at all.
        """
        turn = self._say(OPENING_PROMPT)
        self._record("findings" if turn.tools_called else "clean", turn)
        self.turns.append(turn)
        return turn

    def say(self, text: str, on_delta: Any = None) -> Turn:
        """One operator turn, scored by the scope guard before it is shown.

        `on_delta`, when given, is called with each text fragment as the reply
        streams -- the D2 review pane\'s live rendering. It is **not** forwarded
        while the guard is enforcing, for the reason above: the operator cannot
        be shown a reply that is still being judged.
        """
        # Captured before the turn appends anything, so a suppressed turn can
        # be removed whole. Both halves go: with no input-side gate the prompt
        # may itself be the off-topic question, and keeping it would re-prime
        # the model on the next turn into a loop the operator cannot see.
        before = len(self.messages)
        turn = self._say(text, None if self.enforcing else on_delta)

        verdict = (
            self._guard.check(self._recent(), turn.reply)
            if self._guard is not None
            else None
        )
        if verdict is not None and verdict.suppress:
            del self.messages[before:]
            turn.reply = REFUSAL
            turn.refused = True
            # The streaming route renders deltas and swaps in the pane at the
            # end; without this it would show an empty bubble until the swap.
            if on_delta is not None:
                on_delta(REFUSAL)
            self._record("suppressed", turn)
        else:
            self._record("findings" if turn.tools_called else "clean", turn)

        self.turns.append(turn)
        return turn

    def _recent(self, limit: int = HISTORY_TURNS) -> str:
        """Prior exchanges, so the judge scores a reply in context.

        The highest-value defence against a false positive. "Why?" and "what
        about ana?" are in scope only if the judge can see what was asked, and
        a judge handed a bare terse answer has no way to tell a follow-up from
        a non sequitur.
        """
        return "\n\n".join(
            f"operator: {t.prompt}\nnarrator: {t.reply}" for t in self.turns[-limit:]
        )

    def _say(self, text: str, on_delta: Any = None) -> Turn:
        """One turn against the model. Unjudged, and does not record.

        Split from `say` so `open` can use it: recording and the guard belong
        to the caller, because what a turn is *called* in the ledger depends on
        whether it survived scoring.
        """
        turn = Turn(prompt=text)
        self.messages.append({"role": "user", "content": text})
        # One tracker per operator turn. A turn may span several model calls
        # when tools are involved; a tracker records once, so the turn is the
        # invocation -- the same unit `iterations` counts on the ledger line.
        metrics = metrics_for(self.config)

        def _loop() -> Turn:
            for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
                turn.iterations = iteration
                # `on_delta` is passed only when a caller asked to stream, so a
                # non-streaming turn's call is byte-for-byte the one it always
                # was -- a `ConversingModel` that predates the delta channel is
                # never handed a keyword it does not accept.
                completion = (
                    self._model.converse(
                        self.invocation, self.messages, self._tools, on_delta=on_delta
                    )
                    if on_delta is not None
                    else self._model.converse(
                        self.invocation, self.messages, self._tools
                    )
                )

                # The turn's first token, which is the one the operator waited
                # on. Later iterations are the tool loop and report nothing.
                metrics.track_time_to_first_token(completion.time_to_first_token_ms)
                turn.input_tokens += completion.input_tokens
                turn.output_tokens += completion.output_tokens
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": completion.raw_content
                        if completion.raw_content is not None
                        else completion.text,
                    }
                )

                if not completion.tool_calls:
                    turn.reply = completion.text
                    break

                results = []
                for call in completion.tool_calls:
                    turn.tools_called.append(call.name)
                    results.append(self._run(call))
                self.messages.append({"role": "user", "content": results})
            else:
                # Ran out of iterations with the model still calling tools. The
                # turn ends rather than the budget; the operator can ask again.
                turn.reply = (
                    "(the narrator kept calling tools without answering; "
                    "ask again, or read the manifest directly)"
                )
            return turn

        # The whole loop is the invocation, so the duration the SDK records
        # covers what the operator waited for: a narration that re-solved twice
        # took as long as it took, tool execution included. `tools_called` is
        # the same list the ledger line carries, so a variation's tool use is
        # answerable from the console as well as from the committed JSONL.
        try:
            metrics.record(
                _loop,
                lambda t: Outcome(
                    success=True,
                    input_tokens=t.input_tokens,
                    output_tokens=t.output_tokens,
                    tools_called=t.tools_called,
                ),
            )
        except ModelUnavailable as exc:
            self._record("unavailable", turn)
            raise NarratorUnavailable(str(exc)) from exc

        return turn

    def _run(self, call: Any) -> dict[str, Any]:
        """Run one tool call and shape its result for the model."""
        tool = self._by_name.get(call.name)
        if tool is None:
            # Should be unreachable: A1's contract assertion and the fact that
            # we only advertise `self._tools` both prevent it. Reported to the
            # model rather than raised, because the correct outcome of an
            # agent asking for a tool it does not have is a caught error
            # (design 6.1), not a dead review.
            return self._result(call.id, {"error": f"no such tool: {call.name}"}, True)
        try:
            return self._result(call.id, tool.run(**call.arguments), False)
        except ToolError as exc:
            return self._result(call.id, {"error": str(exc)}, True)
        except TypeError as exc:
            return self._result(
                call.id, {"error": f"bad arguments for {call.name}: {exc}"}, True
            )

    @staticmethod
    def _result(call_id: str, payload: Any, is_error: bool) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": call_id,
            "content": json.dumps(payload, default=str),
            "is_error": is_error,
        }

    def _record(self, outcome: str, turn: Turn) -> None:
        """One ledger line per operator turn.

        Invocations have no merge key -- design 7 calls them events rather
        than entities -- so a ten-turn review is ten facts, which is exactly
        what design 8's "operator edit count" metric needs to be countable.

        The tool trace is per turn for the same reason. A review where the
        operator asked one question that moved the carrier set and nine that
        did not is a different review from one where every turn re-solved,
        and only a per-turn line can say which happened.
        """
        record_agent_invocation(
            self.ledger_root,
            self.run,
            CONFIG_KEY,
            outcome=outcome,
            iterations=turn.iterations,
            tools_offered=[tool.name for tool in self._tools],
            tools_called=turn.tools_called,
        )
