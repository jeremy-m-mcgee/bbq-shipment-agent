"""Scoring D2's narrations with a LaunchDarkly judge.

The scorer is a metric, not a safeguard, so the properties worth pinning are
about what it *records* and what it refuses to disturb. Two matter most and
both fail silently:

- **It never changes the review.** No failure path -- an absent judge, one that
  raises, a sampled-out result -- may alter what the operator reads, and none
  may raise. A scorer that could break a review would be a worse trade than no
  scorer at all.
- **Model-authored text never reaches the ledger.** The reasoning goes to
  LaunchDarkly and the log; the ledger takes the number. Same rule that sends
  every capability value through a `StrEnum`.
"""

from pathlib import Path

import pytest
from conftest import CapabilityGate

from bbq_shipment_agent.agent_configs import SnapshotAgentConfigs
from bbq_shipment_agent.agents.model import Completion, ToolCall
from bbq_shipment_agent.agents.narrator import Narrator
from bbq_shipment_agent.agents.scoring import (
    IN_SCOPE_AT,
    JUDGE_KEY,
    NarrationScorer,
)
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

    The real `evaluate` is async and never raises -- it catches everything and
    reports through the result -- so a scripted failure returns a failed result
    rather than throwing, except where a test is about an exception escaping.
    """

    def __init__(self, *results, explode=False):
        self.results = list(results)
        self.calls = []
        self.tracker = FakeTracker()
        self._explode = explode

    async def evaluate(self, history, reply):
        self.calls.append((history, reply))
        if self._explode:
            raise RuntimeError("the judge fell over")
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
        scorer=NarrationScorer(run, judge, ledger_root=tmp_path / "ledger"),
    )


def scored(tmp_path):
    return [
        r
        for r in iter_records(tmp_path / "ledger", AgentInvocationRecord)
        if r.agent_key == JUDGE_KEY
    ]


class TestItRecordsTheScore:
    def test_one_line_per_narration(self, session, tmp_path):
        judge = ScriptedJudge(_result(1.0), _result(0.0))
        model = ScriptedModel(Completion(text="on topic"), Completion(text="off topic"))
        voice = narrator(session, tmp_path, model, judge)

        voice.say("why did USPS win?")
        voice.say("reverse a linked list")

        assert [(r.outcome, r.judge_score) for r in scored(tmp_path)] == [
            ("in_scope", 1.0),
            ("out_of_scope", 0.0),
        ]

    def test_the_threshold_is_inclusive(self, session, tmp_path):
        judge = ScriptedJudge(_result(IN_SCOPE_AT))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="x")), judge)

        voice.say("hmm")

        assert scored(tmp_path)[0].outcome == "in_scope"

    def test_it_claims_no_tools(self, session, tmp_path):
        # Absent, not empty: design 7 makes an absent key mean "this append
        # knows nothing", and an empty list a measurement. A judge has no tool
        # loop, like B1 and D1.
        judge = ScriptedJudge(_result(1.0))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="x")), judge)

        voice.say("why?")

        line = scored(tmp_path)[0]
        assert line.tools_offered is None
        assert line.tools_called is None
        assert line.model == "claude-haiku-4-5-20251001"

    def test_the_reasoning_never_reaches_the_ledger(self, session, tmp_path):
        # Model-authored free text, and the ledger is committed and
        # append-only. The score is a bounded float and is safe; prose is not.
        secret = "the operator lives at 42 Wallaby Way"
        judge = ScriptedJudge(_result(0.0, reasoning=secret))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="x")), judge)

        voice.say("why?")

        assert secret not in (tmp_path / "ledger" / "agent_invocations.jsonl").read_text()


class TestItNeverChangesTheReview:
    """The property that makes this safe to run on every turn."""

    def test_a_low_score_is_still_shown(self, session, tmp_path):
        # Measured, not withheld. Five live turns produced no case where the
        # narrator answered off-topic at all -- it declines by itself -- so
        # suppression guarded a failure with a rate of zero and was removed.
        judge = ScriptedJudge(_result(0.0))
        voice = narrator(
            session, tmp_path, ScriptedModel(Completion(text="leetcode")), judge
        )

        turn = voice.say("reverse a linked list")

        assert turn.reply == "leetcode"

    def test_the_conversation_is_untouched(self, session, tmp_path):
        judge = ScriptedJudge(_result(0.0))
        voice = narrator(
            session, tmp_path, ScriptedModel(Completion(text="leetcode")), judge
        )

        voice.say("reverse a linked list")

        assert [m["role"] for m in voice.messages] == ["user", "assistant"]

    def test_the_narrator_line_is_unaffected(self, session, tmp_path):
        judge = ScriptedJudge(_result(0.0))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="x")), judge)

        voice.say("why?")

        narrations = [
            r
            for r in iter_records(tmp_path / "ledger", AgentInvocationRecord)
            if r.agent_key == "review-narrator"
        ]
        assert [r.outcome for r in narrations] == ["clean"]

    def test_streaming_is_untouched(self, session, tmp_path):
        judge = ScriptedJudge(_result(0.0))
        model = ScriptedModel(Completion(text="x"))
        voice = narrator(session, tmp_path, model, judge)

        voice.say("why?", on_delta=lambda _: None)

        assert model.streamed == [True]


class TestTheOpeningIsNotScored:
    def test_open_does_not_consult_the_judge(self, session, tmp_path):
        # `OPENING_PROMPT` is Python's text asking for exactly the manifest
        # narration the judge measures. Scoring it spends a call to learn
        # nothing.
        judge = ScriptedJudge(_result(1.0))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="plan")), judge)

        voice.open()

        assert judge.calls == []
        assert scored(tmp_path) == []


class TestItFailsQuietly:
    def test_no_judge_records_nothing_and_still_answers(self, session, tmp_path):
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="x")), None)

        assert voice.say("why?").reply == "x"
        assert scored(tmp_path) == []

    def test_a_judge_that_raises(self, session, tmp_path):
        judge = ScriptedJudge(explode=True)
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="x")), judge)

        assert voice.say("why?").reply == "x"
        assert [r.outcome for r in scored(tmp_path)] == ["error"]

    def test_a_result_with_no_score(self, session, tmp_path):
        judge = ScriptedJudge(_result(None, success=False))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="x")), judge)

        assert voice.say("why?").reply == "x"
        assert [r.outcome for r in scored(tmp_path)] == ["error"]

    def test_a_sampled_out_result(self, session, tmp_path):
        judge = ScriptedJudge(_result(None, success=False, sampled=False))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="x")), judge)

        assert voice.say("why?").reply == "x"


class TestItReachesLaunchDarkly:
    def test_the_score_is_tracked(self, session, tmp_path):
        # Programmatic judges do not report their own metric -- only judges
        # attached to a completion-mode config are dispatched for you, and
        # every config here is agent mode.
        judge = ScriptedJudge(_result(0.9))
        voice = narrator(session, tmp_path, ScriptedModel(Completion(text="x")), judge)

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
        # Without this a terse reply is unscoreable: the first live rubric
        # scored a correct one-line narration 0.3 for reading like a fragment.
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

    def test_a_tool_using_turn_is_scored_on_its_final_reply(self, session, tmp_path):
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
