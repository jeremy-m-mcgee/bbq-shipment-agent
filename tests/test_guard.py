"""D2's scope guard: what reaches the operator, and what is withheld.

The guard scores the narration rather than the question, so most of these
drive a real `Narrator` turn with a scripted model and a scripted judge and
then ask what the operator would have seen.

Two properties matter more than the rest and are worth naming here, because
both fail silently:

- **Everything that is not a clear refusal passes.** A guard that blanks a
  legitimate narration is worse than the leetcode answer it exists to prevent,
  since the operator has no override. Each failure mode gets its own test.
- **A suppressed turn leaves no trace in the conversation.** With no input-side
  gate the prompt may itself be the off-topic question, so keeping it would
  re-prime the model on the next turn into a loop nobody can see from outside.
"""

from pathlib import Path

import pytest
from conftest import CapabilityGate

from bbq_shipment_agent.agent_configs import SnapshotAgentConfigs
from bbq_shipment_agent.agents.guard import (
    JUDGE_KEY,
    REFUSAL,
    THRESHOLD,
    ScopeGuard,
)
from bbq_shipment_agent.agents.model import Completion, ToolCall
from bbq_shipment_agent.agents.narrator import Narrator
from bbq_shipment_agent.ledger import AgentInvocationRecord, iter_records
from bbq_shipment_agent.planning import RecordedQuoter
from bbq_shipment_agent.recipients import load_roster, to_shipments
from bbq_shipment_agent.review import ReviewSession
from bbq_shipment_agent.run import initialize_run

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = FIXTURES / "shippo-quotes-sf-dc.json"
ROSTER = FIXTURES / "roster-sf-dc.yaml"
SNAPSHOT = Path(__file__).parent.parent / "config" / "ld-snapshot.json"


class ScriptedModel:
    """Replays completions and records whether it was asked to stream."""

    def __init__(self, *completions):
        self.completions = list(completions)
        self.calls = 0
        self.streamed = []

    def converse(self, invocation, messages, tools=(), on_delta=None):
        self.calls += 1
        self.streamed.append(on_delta is not None)
        return self.completions.pop(0) if self.completions else Completion(text="done")


class FakeTracker:
    def __init__(self):
        self.judged = []

    def track_judge_result(self, result):
        self.judged.append(result)


class FakeJudgeConfig:
    def __init__(self, tracker):
        self._tracker = tracker
        self.model = type("M", (), {"name": "claude-haiku-4-5-20251001"})()
        self.messages = [type("Msg", (), {"content": "score the narration"})()]

    def create_tracker(self):
        return self._tracker


class ScriptedJudge:
    """The SDK's `Judge`, scripted.

    `evaluate` is async and never raises in the real one -- it catches
    everything and reports through the result -- so a scripted failure returns
    a failed result rather than throwing, except where a test is specifically
    about an exception escaping.
    """

    def __init__(self, *results, explode=False):
        self.results = list(results)
        self.calls = []
        self.tracker = FakeTracker()
        self._explode = explode

    async def evaluate(self, history, reply):
        self.calls.append((history, reply))
        if self._explode:
            raise RuntimeError("the grader fell over")
        return self.results.pop(0) if self.results else _result(1.0)

    def get_ai_config(self):
        return FakeJudgeConfig(self.tracker)


def _result(score, *, success=True, sampled=True, reasoning="because"):
    from ldai.providers.types import JudgeResult

    return JudgeResult(
        judge_config_key=JUDGE_KEY,
        success=success,
        sampled=sampled,
        metric_key=f"$ld:ai:judge:{JUDGE_KEY}",
        score=score,
        reasoning=reasoning,
    )


@pytest.fixture
def session(tmp_path):
    roster = load_roster(ROSTER)
    run = initialize_run(
        ledger_root=tmp_path / "ledger",
        gate=CapabilityGate(),
        agent_source=SnapshotAgentConfigs(SNAPSHOT),
        snapshot_path=tmp_path / "snap.json",
    )
    return run, ReviewSession(
        run,
        to_shipments(roster.recipients),
        roster.origin,
        roster.ship_dates,
        ledger_root=tmp_path / "ledger",
        quoter=RecordedQuoter.from_file(QUOTES),
    )


def narrator(session, tmp_path, model, judge):
    run, review = session
    return Narrator(
        run,
        review,
        ledger_root=tmp_path / "ledger",
        model=model,
        guard=ScopeGuard(run, judge, ledger_root=tmp_path / "ledger"),
    )


def invocations(tmp_path, agent_key=None):
    records = list(iter_records(tmp_path / "ledger", AgentInvocationRecord))
    return [r for r in records if agent_key is None or r.agent_key == agent_key]


class TestTheOpeningIsNeverJudged:
    def test_open_does_not_consult_the_judge(self, session, tmp_path):
        # `OPENING_PROMPT` is Python's text asking for exactly the manifest
        # narration the guard protects. Judging it risks withholding the
        # opening, which would leave the review with nothing to read.
        judge = ScriptedJudge(_result(0.0))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="the plan")), judge)

        turn = voice.open()

        assert judge.calls == []
        assert turn.reply == "the plan"
        assert turn.refused is False


class TestEnforce:
    def test_a_low_score_is_withheld(self, session, tmp_path):
        judge = ScriptedJudge(_result(0.0))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="leetcode")), judge)

        turn = voice.say("reverse a linked list")

        assert turn.reply == REFUSAL
        assert turn.refused is True

    def test_the_whole_turn_is_rolled_back(self, session, tmp_path):
        # Both halves go. With no input-side gate the prompt may itself be the
        # off-topic question, and keeping it would re-prime the model into
        # suppressing again on the next turn, forever.
        judge = ScriptedJudge(_result(0.0))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="leetcode")), judge)

        voice.say("reverse a linked list")

        assert voice.messages == []

    def test_a_later_turn_carries_no_trace_of_it(self, session, tmp_path):
        judge = ScriptedJudge(_result(0.0), _result(1.0))
        model = ScriptedModel(
            Completion(text="leetcode"), Completion(text="USPS won on cost")
        )
        voice = narrator(session, tmp_path, model, judge)

        voice.say("reverse a linked list")
        voice.say("why did USPS win?")

        assert [m["content"] for m in voice.messages if m["role"] == "user"] == [
            "why did USPS win?"
        ]

    def test_a_high_score_passes(self, session, tmp_path):
        judge = ScriptedJudge(_result(1.0))
        voice = narrator(
            session, tmp_path, ScriptedModel(Completion(text="USPS won on cost")), judge
        )

        turn = voice.say("why did USPS win?")

        assert turn.reply == "USPS won on cost"
        assert turn.refused is False

    def test_the_threshold_is_inclusive(self, session, tmp_path):
        judge = ScriptedJudge(_result(THRESHOLD))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="borderline")), judge)

        assert voice.say("hmm").refused is False


class TestTheLedger:
    def test_a_suppressed_turn_records_both_invocations(self, session, tmp_path):
        # The narrator *ran*, so it is recorded -- CLAUDE.md's rule is that an
        # invocation that ran is always recorded. The judge is its own line,
        # because the judge is its own invocation.
        judge = ScriptedJudge(_result(0.0))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="leetcode")), judge)

        voice.say("reverse a linked list")

        by_key = {r.agent_key: r for r in invocations(tmp_path)}
        assert by_key["review-narrator"].outcome == "suppressed"
        assert by_key[JUDGE_KEY].outcome == "out_of_scope"
        assert by_key[JUDGE_KEY].judge_score == 0.0

    def test_the_judge_line_claims_no_tools(self, session, tmp_path):
        # Absent, not empty: design 7 makes an absent key mean "this append
        # knows nothing", and an empty list a measurement. A judge has no tool
        # loop, like B1 and D1.
        judge = ScriptedJudge(_result(1.0))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="ok")), judge)

        voice.say("why?")

        line = invocations(tmp_path, JUDGE_KEY)[0]
        assert line.tools_offered is None
        assert line.tools_called is None

    def test_the_reasoning_never_reaches_the_ledger(self, session, tmp_path):
        # Model-authored free text, and the ledger is committed and
        # append-only -- the same rule that makes every capability value pass
        # through a StrEnum. The score is a bounded float and is safe.
        secret = "the operator lives at 42 Wallaby Way"
        judge = ScriptedJudge(_result(0.0, reasoning=secret))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="leetcode")), judge)

        voice.say("reverse a linked list")

        written = (tmp_path / "ledger" / "agent_invocations.jsonl").read_text()
        assert secret not in written


class TestItFailsOpen:
    """Nothing but a clear refusal withholds a narration."""

    def test_no_judge_at_all(self, session, tmp_path):
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="anything")), None)

        turn = voice.say("why?")

        assert turn.reply == "anything"
        assert invocations(tmp_path, JUDGE_KEY) == []

    def test_a_judge_that_raises(self, session, tmp_path):
        judge = ScriptedJudge(explode=True)
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="anything")), judge)

        turn = voice.say("why?")

        assert turn.reply == "anything"
        assert [r.outcome for r in invocations(tmp_path, JUDGE_KEY)] == ["error"]

    def test_a_result_with_no_score(self, session, tmp_path):
        judge = ScriptedJudge(_result(None, success=False))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="anything")), judge)

        turn = voice.say("why?")

        assert turn.reply == "anything"
        assert [r.outcome for r in invocations(tmp_path, JUDGE_KEY)] == ["error"]

    def test_a_sampled_out_result(self, session, tmp_path):
        # `sampled=False` carries no score. A guard at 100% sampling should
        # never see this, but a console edit can change that without a deploy.
        judge = ScriptedJudge(_result(None, success=False, sampled=False))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="anything")), judge)

        assert voice.say("why?").refused is False


class TestTheScoreReachesLaunchDarkly:
    def test_it_is_tracked_on_the_judges_own_tracker(self, session, tmp_path):
        # Programmatic judges do not report their own metric -- only attached
        # ones are dispatched by `ManagedAgent` -- so the guard tracks it.
        judge = ScriptedJudge(_result(0.9))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="ok")), judge)

        voice.say("why?")

        assert [r.score for r in judge.tracker.judged] == [0.9]
        assert judge.tracker.judged[0].sampled is True


class TestWhatTheJudgeIsGiven:
    def test_it_scores_the_reply_not_the_question(self, session, tmp_path):
        judge = ScriptedJudge(_result(1.0))
        voice = narrator(
            session, tmp_path, ScriptedModel(Completion(text="the narration")), judge
        )

        voice.say("why did USPS win?")

        _history, reply = judge.calls[0]
        assert reply == "the narration"

    def test_prior_turns_travel_as_context(self, session, tmp_path):
        # The false-positive mitigation that matters. "why?" is in scope only
        # if the judge can see what was asked before it.
        judge = ScriptedJudge(_result(1.0), _result(1.0))
        model = ScriptedModel(
            Completion(text="USPS won on cost"), Completion(text="because Saturday")
        )
        voice = narrator(session, tmp_path, model, judge)

        voice.say("which carrier won?")
        voice.say("why?")

        history, _reply = judge.calls[1]
        assert "which carrier won?" in history
        assert "USPS won on cost" in history


class TestStreaming:
    def test_enforcing_does_not_stream(self, session, tmp_path):
        # A reply that must be judged before the operator sees it cannot
        # already be painted on their screen.
        judge = ScriptedJudge(_result(1.0))
        model = ScriptedModel(Completion(text="ok"))
        voice = narrator(session, tmp_path, model, judge)

        voice.say("why?", on_delta=lambda _: None)

        assert model.streamed == [False]


    def test_a_refusal_is_pushed_through_the_delta_channel(self, session, tmp_path):
        # The SSE route renders deltas and swaps the pane at the end; without
        # this the operator watches an empty bubble until the swap.
        judge = ScriptedJudge(_result(0.0))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="leetcode")), judge)
        seen = []

        voice.say("reverse a linked list", on_delta=seen.append)

        assert seen == [REFUSAL]


class TestToolCallsStillWork:
    def test_a_tool_using_turn_is_judged_on_its_final_reply(self, session, tmp_path):
        judge = ScriptedJudge(_result(1.0))
        model = ScriptedModel(
            Completion(
                text="",
                tool_calls=(ToolCall(id="t1", name="read_manifest", arguments={}),),
                raw_content=[{"type": "tool_use"}],
            ),
            Completion(text="here is the tradeoff"),
        )
        voice = narrator(session, tmp_path, model, judge)

        turn = voice.say("explain")

        _history, reply = judge.calls[0]
        assert reply == "here is the tradeoff"
        assert turn.tools_called == ["read_manifest"]
