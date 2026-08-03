"""D2's voice: the tool loop, and what it reports.

The conversation itself is exercised live; these pin the mechanics that a
live run cannot assert on — how many times the model is called, how many
times metrics are reported, and what happens when a tool refuses.
"""

import json
import textwrap
from pathlib import Path

import pytest

from bbq_shipment_agent.agent_configs import SnapshotAgentConfigs
from bbq_shipment_agent.agents.model import Completion, ToolCall
from bbq_shipment_agent.agents.narrator import (
    MAX_TOOL_ITERATIONS,
    Narrator,
    NarratorUnavailable,
)
from bbq_shipment_agent.agents.model import ModelUnavailable
from bbq_shipment_agent.ledger import AgentInvocationRecord, iter_records
from bbq_shipment_agent.planning import RecordedQuoter
from bbq_shipment_agent.recipients import load_roster, to_shipments
from bbq_shipment_agent.review import ReviewSession
from bbq_shipment_agent.run import initialize_run

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = FIXTURES / "shippo-quotes-sf-dc.json"
ROSTER = FIXTURES / "roster-sf-dc.yaml"
SNAPSHOT = Path(__file__).parent.parent / "config" / "ld-snapshot.json"

CONFIG = """
    profiles:
      baseline:
        planner: "off"
        memory: "off"
        validation: "off"
        verification: "off"
        authority: "propose_only"
    default_profile: "baseline"
    authority_ceiling: "propose_only"
    kill_switch: false
"""


class ScriptedModel:
    """Replays a list of completions, recording every call."""

    def __init__(self, *completions):
        self.completions = list(completions)
        self.calls = 0

    def converse(self, invocation, messages, tools=()):
        self.calls += 1
        self.tools_offered = tools
        nxt = self.completions.pop(0) if self.completions else Completion(text="done")
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class SpyMetrics:
    def __init__(self):
        self.begins = 0
        self.events = []

    def begin(self):
        self.begins += 1
        return self

    def track_success(self):
        self.events.append("success")

    def track_error(self):
        self.events.append("error")

    def track_tokens(self, input_tokens, output_tokens):
        self.events.append(("tokens", input_tokens, output_tokens))


@pytest.fixture
def session(tmp_path):
    (tmp_path / "capabilities.yaml").write_text(
        textwrap.dedent(CONFIG), encoding="utf-8"
    )
    roster = load_roster(ROSTER)
    run = initialize_run(
        ledger_root=tmp_path / "ledger",
        config_path=tmp_path / "capabilities.yaml",
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


def narrator(session, tmp_path, model, metrics=None):
    run, review = session
    return Narrator(
        run, review, ledger_root=tmp_path / "ledger", model=model, metrics=metrics
    )


class TestTheToolLoop:
    def test_a_reply_with_no_tool_calls_ends_the_turn(self, session, tmp_path):
        model = ScriptedModel(Completion(text="here is the plan"))
        turn = narrator(session, tmp_path, model).say("explain")
        assert turn.reply == "here is the plan"
        assert model.calls == 1
        assert turn.tools_called == []

    def test_a_tool_call_is_run_and_the_loop_continues(self, session, tmp_path):
        model = ScriptedModel(
            Completion(
                text="",
                tool_calls=(ToolCall(id="t1", name="read_manifest", arguments={}),),
                raw_content=[{"type": "tool_use", "id": "t1"}],
            ),
            Completion(text="the run costs a lot"),
        )
        turn = narrator(session, tmp_path, model).say("what does it cost?")
        assert turn.tools_called == ["read_manifest"]
        assert turn.reply == "the run costs a lot"
        assert model.calls == 2

    def test_the_tools_offered_are_the_ones_the_contract_names(self, session, tmp_path):
        from bbq_shipment_agent.agents.tools import TOOL_NAMES

        model = ScriptedModel(Completion(text="ok"))
        narrator(session, tmp_path, model).say("hello")
        assert {t.name for t in model.tools_offered} == TOOL_NAMES["review-narrator"]

    def test_a_runaway_loop_ends_the_turn_not_the_budget(self, session, tmp_path):
        forever = [
            Completion(
                text="",
                tool_calls=(ToolCall(id=f"t{i}", name="read_manifest", arguments={}),),
                raw_content=[{"type": "tool_use"}],
            )
            for i in range(MAX_TOOL_ITERATIONS + 3)
        ]
        turn = narrator(session, tmp_path, ScriptedModel(*forever)).say("loop")
        assert "kept calling tools" in turn.reply
        assert turn.iterations == MAX_TOOL_ITERATIONS


class TestToolFailuresStayInTheConversation:
    def test_a_bad_argument_comes_back_as_a_tool_result(self, session, tmp_path):
        # An operator asking to pin someone to a date that is not a candidate
        # should be told, in the conversation, not watch the review die.
        model = ScriptedModel(
            Completion(
                text="",
                tool_calls=(
                    ToolCall(
                        id="t1",
                        name="propose_edit",
                        arguments={"kind": "ship_date", "recipient_key": "ana",
                                   "ship_date": "2026-12-25"},
                    ),
                ),
                raw_content=[{"type": "tool_use"}],
            ),
            Completion(text="that date is not available"),
        )
        turn = narrator(session, tmp_path, model).say("pin ana to christmas")
        assert turn.reply == "that date is not available"
        assert turn.iterations == 2

    def test_an_unknown_tool_is_reported_not_raised(self, session, tmp_path):
        # Design 6.1: an agent asking for a tool it does not have should
        # produce a caught error, not an expanded capability or a dead review.
        model = ScriptedModel(
            Completion(
                text="",
                tool_calls=(ToolCall(id="t1", name="buy_labels", arguments={}),),
                raw_content=[{"type": "tool_use"}],
            ),
            Completion(text="I cannot do that"),
        )
        turn = narrator(session, tmp_path, model).say("buy the labels")
        assert turn.reply == "I cannot do that"

    def test_an_unreachable_model_ends_the_narrator(self, session, tmp_path):
        model = ScriptedModel(ModelUnavailable("no key"))
        with pytest.raises(NarratorUnavailable):
            narrator(session, tmp_path, model).say("hello")


class TestMetricsPerTurn:
    def test_tokens_are_reported_once_for_the_whole_turn(self, session, tmp_path):
        # An LD tracker records tokens exactly once. A turn with three tool
        # round trips makes three model calls, and reporting each one meant
        # the first was recorded and the rest dropped with a warning -- seen
        # live before this was fixed.
        spy = SpyMetrics()
        model = ScriptedModel(
            Completion(
                text="",
                input_tokens=100,
                output_tokens=10,
                tool_calls=(ToolCall(id="t1", name="read_manifest", arguments={}),),
                raw_content=[{"type": "tool_use"}],
            ),
            Completion(text="done", input_tokens=200, output_tokens=20),
        )
        turn = narrator(session, tmp_path, model, spy).say("explain")
        token_events = [e for e in spy.events if isinstance(e, tuple)]
        assert token_events == [("tokens", 300, 30)]
        # The same total the ledger line's iterations describe.
        assert (turn.input_tokens, turn.output_tokens) == (300, 30)

    def test_each_turn_gets_its_own_reporter(self, session, tmp_path):
        spy = SpyMetrics()
        voice = narrator(session, tmp_path, ScriptedModel(
            Completion(text="a"), Completion(text="b")
        ), spy)
        voice.say("one")
        voice.say("two")
        assert spy.begins == 2
        assert spy.events.count("success") == 2


class TestTheLedger:
    def test_every_turn_is_one_invocation_record(self, session, tmp_path):
        # Design 7: invocations are events, not entities, so a ten-turn review
        # is ten facts -- which is what design 8's operator edit count needs.
        voice = narrator(session, tmp_path, ScriptedModel(
            Completion(text="a"), Completion(text="b"), Completion(text="c")
        ))
        voice.say("one")
        voice.say("two")
        voice.say("three")
        records = list(iter_records(tmp_path / "ledger", AgentInvocationRecord))
        assert len(records) == 3
        assert {r.agent_key for r in records} == {"review-narrator"}
