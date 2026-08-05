"""What reaches LaunchDarkly for one invocation, and what deliberately does not.

The tracker's own contract is at-most-once for duration, tokens, first-token
time and the success/error pair, and it logs a warning when that is violated.
These pin the two things this repo adds on top: a repeat is dropped here so a
legitimate tool loop does not print a warning per iteration, and an unmeasured
latency reports nothing rather than reporting zero.

The integration half matters more than the unit half. A metric that is
plumbed through the protocol and never called by a stage is the decorative
kind design 6.5 removed capabilities for, so each stage's real path is driven
here with a fake tracker attached to the config it was served.
"""

from dataclasses import replace
from pathlib import Path
import json
import time

import pytest
from conftest import CapabilityGate

from bbq_shipment_agent.agent_configs import AgentConfig, SnapshotAgentConfigs
from bbq_shipment_agent.agents.metrics import (
    InvocationMetrics,
    NoMetrics,
    SdkMetrics,
    metrics_for,
)
from bbq_shipment_agent.agents.model import Completion, ToolCall
from bbq_shipment_agent.agents.narrator import Narrator
from bbq_shipment_agent.planning import RecordedQuoter
from bbq_shipment_agent.recipients import load_roster, to_shipments
from bbq_shipment_agent.review import ReviewSession
from bbq_shipment_agent.run import initialize_run

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = FIXTURES / "shippo-quotes-sf-dc.json"
ROSTER = FIXTURES / "roster-sf-dc.yaml"
SNAPSHOT = Path(__file__).parent.parent / "config" / "ld-snapshot.json"

class FakeTracker:
    """Records what the SDK tracker would have been asked to send.

    Deliberately *not* at-most-once: the point of most of these tests is that
    the repeat never arrives here in the first place.
    """

    def __init__(self):
        self.durations = []
        self.first_token = []
        self.tokens = []
        self.tool_calls = []
        self.successes = 0
        self.errors = 0

    def track_duration(self, milliseconds):
        self.durations.append(milliseconds)

    def track_time_to_first_token(self, milliseconds):
        self.first_token.append(milliseconds)

    def track_tokens(self, usage):
        self.tokens.append(usage)

    def track_tool_calls(self, names):
        self.tool_calls.extend(names)

    def track_success(self):
        self.successes += 1

    def track_error(self):
        self.errors += 1


class FakeEvent:
    """One streamed event, optionally arriving after a measurable pause."""

    def __init__(self, type_, delay=0.0):
        self.type = type_
        self._delay = delay

    def arrive(self):
        if self._delay:
            time.sleep(self._delay)


class FakeBlock:
    def __init__(self, type_, **fields):
        self.type = type_
        for name, value in fields.items():
            setattr(self, name, value)


class FakeMessage:
    """What `get_final_message` assembles, shaped like the SDK's `Message`."""

    def __init__(
        self,
        text="ok",
        input_tokens=0,
        output_tokens=0,
        stop_reason="end_turn",
        model="claude-sonnet-5",
        tool_calls=(),
    ):
        self.content = [FakeBlock("text", text=text)] + [
            FakeBlock("tool_use", id=i, name=n, input=a) for i, n, a in tool_calls
        ]
        self.usage = FakeBlock(
            "usage", input_tokens=input_tokens, output_tokens=output_tokens
        )
        self.stop_reason = stop_reason
        self.model = model


class FakeStream:
    def __init__(self, events, message):
        self._events = events
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for event in self._events:
            event.arrive()
            yield event

    def get_final_message(self):
        return self._message


class FakeAnthropic:
    """Stands in for the SDK client. Opens nothing.

    `refuse` names a parameter the fake API rejects on the first attempt, the
    way a real provider rejects a deprecated one -- with `always`, on every
    attempt, which is the non-droppable case.
    """

    def __init__(self, events, message=None, refuse=None, always=False):
        self._events = events
        self._message = message or FakeMessage()
        self._refuse = refuse
        self._always = always
        self.requests = []
        self.attempts = 0
        self.messages = self

    def stream(self, **request):
        import httpx
        from anthropic import APIError

        self.attempts += 1
        self.requests.append(dict(request))
        refused = self._refuse is not None and (
            self._always or self.attempts == 1
        )
        if refused:
            raise APIError(
                f"invalid request: `{self._refuse}` is not supported",
                httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
                body=None,
            )
        return FakeStream(self._events, self._message)


class ScriptedModel:
    """Replays a list of completions, recording every call."""

    def __init__(self, *completions):
        self.completions = list(completions)
        self.calls = 0

    def converse(self, invocation, messages, tools=()):
        self.calls += 1
        return self.completions.pop(0) if self.completions else Completion(text="done")


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


def wire_tracker(run, agent_key):
    """Attach a fake tracker to the config a stage will be served.

    The same tracker for every invocation, which a live run would never do --
    `create_tracker` mints a fresh run id per call. Fine here because each
    test drives one invocation, and it is what lets the assertion be "this
    metric arrived once" rather than "some tracker somewhere saw it".
    """
    tracker = FakeTracker()
    run.agent_configs[agent_key] = replace(
        run.agent_configs[agent_key], create_tracker=lambda: tracker
    )
    return tracker


class TestTheReportersAgree:
    def test_no_metrics_answers_everything_the_protocol_declares(self):
        # A stage calls the protocol, not a class. A method on `SdkMetrics`
        # that `NoMetrics` lacks would be an AttributeError on every offline
        # run -- which is design 6.10's *normal* path, so it would be an
        # AttributeError in the common case and not the rare one.
        declared = {
            name
            for name in dir(InvocationMetrics)
            if not name.startswith("_")
        }
        assert declared
        for name in declared:
            assert callable(getattr(NoMetrics(), name))
            assert callable(getattr(SdkMetrics(FakeTracker()), name))

    def test_a_config_with_no_tracker_factory_reports_nowhere(self):
        assert isinstance(metrics_for(AgentConfig(agent_key="x")), NoMetrics)

    def test_a_tracker_factory_that_raises_is_not_a_failed_run(self):
        def explode():
            raise RuntimeError("LaunchDarkly is unreachable")

        config = AgentConfig(agent_key="x", create_tracker=explode)
        assert isinstance(metrics_for(config), NoMetrics)


class TestDuration:
    def test_it_is_reported_once_in_milliseconds(self):
        tracker = FakeTracker()
        SdkMetrics(tracker).track_duration()

        assert len(tracker.durations) == 1
        assert isinstance(tracker.durations[0], int)
        assert tracker.durations[0] >= 0

    def test_a_second_call_never_reaches_the_tracker(self):
        # Dropped here rather than at the tracker, which would log a warning.
        tracker = FakeTracker()
        metrics = SdkMetrics(tracker)
        metrics.track_duration()
        metrics.track_duration()

        assert len(tracker.durations) == 1

    def test_a_tracker_that_raises_does_not_propagate(self):
        class Broken(FakeTracker):
            def track_duration(self, milliseconds):
                raise RuntimeError("event delivery failed")

        SdkMetrics(Broken()).track_duration()  # no exception


class TestTimeToFirstToken:
    def test_an_unmeasured_latency_reports_nothing(self):
        # Every replayed completion answers `None`. Zero would put a fictional
        # latency in the same chart as the real ones.
        tracker = FakeTracker()
        SdkMetrics(tracker).track_time_to_first_token(None)

        assert tracker.first_token == []

    def test_the_first_measurement_wins(self):
        tracker = FakeTracker()
        metrics = SdkMetrics(tracker)
        metrics.track_time_to_first_token(12.4)
        metrics.track_time_to_first_token(880.0)

        assert tracker.first_token == [12]

    def test_a_none_before_a_measurement_does_not_consume_the_slot(self):
        tracker = FakeTracker()
        metrics = SdkMetrics(tracker)
        metrics.track_time_to_first_token(None)
        metrics.track_time_to_first_token(31.0)

        assert tracker.first_token == [31]


class TestToolCalls:
    def test_order_and_repeats_are_kept(self):
        # The ledger keeps both -- "validated four addresses" is the fact
        # worth having -- and the console should not disagree with it.
        tracker = FakeTracker()
        SdkMetrics(tracker).track_tools_called(
            ["validate_address", "read_image_region", "validate_address"]
        )

        assert tracker.tool_calls == [
            "validate_address",
            "read_image_region",
            "validate_address",
        ]

    def test_an_agent_that_called_nothing_reports_nothing(self):
        # Not an empty event: B1 and D1 have no tool loop at all, and an
        # empty report would make "was offered none" look like "declined".
        tracker = FakeTracker()
        SdkMetrics(tracker).track_tools_called([])

        assert tracker.tool_calls == []


class TestWhereTheNumberComesFrom:
    """`AnthropicModel` streams, and this is the only reason it does.

    Here rather than in a model test file because the streaming is not a
    feature of the model seam -- no caller sees a stream, and the assembled
    `Completion` is identical either way. It exists so that time to first
    token is measurable at all, and the thing worth pinning is that the
    measurement is real and that assembling from a stream did not quietly
    change what the rest of the pipeline receives.

    A fake client is injected onto `_client`, so this opens no socket.
    """

    @staticmethod
    def model_with(client):
        from bbq_shipment_agent.agents.model import AnthropicModel

        model = AnthropicModel(api_key="test-key-not-real")
        model._client = client
        return model

    @staticmethod
    def invocation():
        from bbq_shipment_agent.agents.model import Invocation

        return Invocation(
            agent_key="manifest-verification",
            model="claude-sonnet-5",
            instructions="check it",
            parameters={"temperature": 0},
        )

    def test_the_clock_stops_at_the_first_content_not_at_the_acknowledgement(self):
        # `message_start` carries no content: timing to it would report the
        # connection rather than the model.
        client = FakeAnthropic(
            events=[
                FakeEvent("message_start", delay=0.02),
                FakeEvent("content_block_start"),
                FakeEvent("content_block_delta", delay=0.02),
                FakeEvent("content_block_delta"),
            ]
        )
        completion = self.model_with(client).complete(self.invocation(), "hi")

        assert completion.time_to_first_token_ms is not None
        # Both delays elapse before the first delta; neither has elapsed at
        # `message_start`. A clock stopped there would come in under 20ms.
        assert completion.time_to_first_token_ms >= 20

    def test_a_reply_with_no_content_reports_no_latency_rather_than_zero(self):
        client = FakeAnthropic(events=[FakeEvent("message_start")])
        completion = self.model_with(client).complete(self.invocation(), "hi")

        assert completion.time_to_first_token_ms is None

    def test_the_assembled_completion_is_what_it_always_was(self):
        client = FakeAnthropic(
            events=[FakeEvent("content_block_delta")],
            message=FakeMessage(
                text="the answer",
                input_tokens=11,
                output_tokens=7,
                stop_reason="end_turn",
                model="claude-sonnet-5-20260101",
                tool_calls=[("t1", "read_manifest", {})],
            ),
        )
        completion = self.model_with(client).complete(self.invocation(), "hi")

        assert completion.text == "the answer"
        assert (completion.input_tokens, completion.output_tokens) == (11, 7)
        assert completion.total_tokens == 18
        assert completion.stop_reason == "end_turn"
        assert completion.model == "claude-sonnet-5-20260101"
        assert [c.name for c in completion.tool_calls] == ["read_manifest"]
        assert completion.raw_content is not None

    def test_a_refused_model_parameter_is_still_dropped_and_retried(self):
        # Design 10's `temperature` case. The retry loop is unchanged, but
        # the request now leaves on `__enter__` rather than from `create`, so
        # where the `APIError` surfaces moved and this is what pins it.
        client = FakeAnthropic(
            events=[FakeEvent("content_block_delta")], refuse="temperature"
        )
        completion = self.model_with(client).complete(self.invocation(), "hi")

        assert completion.dropped_parameters == ("temperature",)
        assert "temperature" not in client.requests[-1]
        assert client.attempts == 2

    def test_an_error_that_names_nothing_droppable_is_raised(self):
        from bbq_shipment_agent.agents.model import ModelUnavailable

        client = FakeAnthropic(events=[], refuse="model", always=True)
        with pytest.raises(ModelUnavailable):
            self.model_with(client).complete(self.invocation(), "hi")


SSE_EVENTS = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "claude-sonnet-5-20260101", "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "the "}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": "answer"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 7}}),
    ("message_stop", {"type": "message_stop"}),
]


class TestAgainstTheRealSdk:
    """The streaming assumption, checked against the SDK rather than a fake.

    The fakes above pin this repo's logic and would keep passing through an
    SDK upgrade that renamed an event or moved the usage totals -- which is
    the failure that would silently turn every first-token measurement into
    `None`, or worse, drop the token counts. So this one drives the real
    `Anthropic` client over an `httpx.MockTransport`.

    **It opens no socket.** The transport answers in-process, which is what
    lets the SDK's own SSE parsing and message accumulation run for real
    without contradicting the rule that no test reaches the network.
    """

    def test_the_sdk_streams_and_assembles_what_the_pipeline_expects(self):
        import httpx
        from anthropic import Anthropic

        from bbq_shipment_agent.agents.model import AnthropicModel, Invocation

        body = "".join(
            f"event: {name}\ndata: {json.dumps(payload)}\n\n"
            for name, payload in SSE_EVENTS
        )
        sent = {}

        def handler(request):
            sent["body"] = json.loads(request.content)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=body
            )

        model = AnthropicModel(api_key="sk-ant-not-real")
        model._client = Anthropic(
            api_key="sk-ant-not-real",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        completion = model.complete(
            Invocation(
                agent_key="manifest-verification",
                model="claude-sonnet-5",
                instructions="check the manifest",
                parameters={"max_tokens": 256},
            ),
            "here is the manifest",
        )

        # The request really was a streaming one -- the whole reason TTFT is
        # measurable, and the one thing a fake client cannot demonstrate.
        assert sent["body"]["stream"] is True
        assert completion.time_to_first_token_ms is not None

        # ...and streaming changed nothing the pipeline reads.
        assert completion.text == "the answer"
        assert (completion.input_tokens, completion.output_tokens) == (11, 7)
        assert completion.stop_reason == "end_turn"
        assert completion.model == "claude-sonnet-5-20260101"


class TestWhatD2ActuallyReports:
    """The narrator's real path, since it is the one stage with both loops."""

    def test_one_turn_reports_duration_and_first_token_once(
        self, session, tmp_path
    ):
        run, review = session
        tracker = wire_tracker(run, "review-narrator")
        model = ScriptedModel(
            Completion(text="here is the plan", time_to_first_token_ms=42.0)
        )

        Narrator(run, review, ledger_root=tmp_path / "ledger", model=model).say("hi")

        assert tracker.first_token == [42]
        assert len(tracker.durations) == 1
        assert tracker.successes == 1

    def test_the_tool_loop_reports_the_first_turn_and_every_tool(
        self, session, tmp_path
    ):
        run, review = session
        tracker = wire_tracker(run, "review-narrator")
        model = ScriptedModel(
            Completion(
                text="",
                time_to_first_token_ms=10.0,
                tool_calls=(ToolCall(id="t1", name="read_manifest", arguments={}),),
            ),
            Completion(
                text="",
                time_to_first_token_ms=999.0,
                tool_calls=(ToolCall(id="t2", name="read_manifest", arguments={}),),
            ),
            Completion(text="and here it is", time_to_first_token_ms=999.0),
        )

        Narrator(run, review, ledger_root=tmp_path / "ledger", model=model).say("hi")

        # The operator waited from the start of the turn, not from the last
        # model call in it.
        assert tracker.first_token == [10]
        assert tracker.tool_calls == ["read_manifest", "read_manifest"]
        # One duration for the whole turn, tool round trips included.
        assert len(tracker.durations) == 1

    def test_a_replayed_turn_reports_no_first_token_but_still_a_duration(
        self, session, tmp_path
    ):
        run, review = session
        tracker = wire_tracker(run, "review-narrator")
        model = ScriptedModel(Completion(text="from a fixture"))

        Narrator(run, review, ledger_root=tmp_path / "ledger", model=model).say("hi")

        assert tracker.first_token == []
        assert len(tracker.durations) == 1
