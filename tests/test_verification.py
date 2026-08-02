"""D1, against a real recorded reply from the configured model."""

import json
import textwrap
from pathlib import Path

import pytest

from bbq_shipment_agent.agent_configs import AgentConfig, SnapshotAgentConfigs
from bbq_shipment_agent.agents import (
    AGENT_KEY,
    Completion,
    Invocation,
    ModelUnavailable,
    RecordedModel,
    manifest_payload,
    verify_manifest,
)
from bbq_shipment_agent.ledger import AgentInvocationRecord, iter_records
from bbq_shipment_agent.plan import plan_run
from bbq_shipment_agent.planning import RecordedQuoter
from bbq_shipment_agent.recipients import RecordedAddressValidator, load_roster
from bbq_shipment_agent.run import initialize_run

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = FIXTURES / "shippo-quotes-sf-dc.json"
VALIDATIONS = FIXTURES / "shippo-addresses.json"
COMPLETIONS = FIXTURES / "d1-completions.json"
ROSTER = FIXTURES / "roster-sf-dc.yaml"
SNAPSHOT = Path(__file__).parent.parent / "config" / "ld-snapshot.json"

CONFIG = """
    profiles:
      baseline:
        planner: "off"
        memory: "off"
        validation: "standard"
        verification: "{verification}"
        authority: "propose_only"
    default_profile: "baseline"
    authority_ceiling: "propose_only"
    kill_switch: false
"""


class StubModel:
    """Returns whatever it was given, and remembers what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, invocation, prompt):
        self.calls.append((invocation, prompt))
        reply = self.replies.pop(0) if self.replies else "{}"
        if isinstance(reply, Exception):
            raise reply
        return Completion(text=reply, input_tokens=10, output_tokens=5)


class StubAgentConfigs:
    """Serves one config for `manifest-verification`, nothing for the rest."""

    def __init__(self, config: AgentConfig | None):
        self._config = config

    def fetch(self, agent_key, context):
        if agent_key == AGENT_KEY and self._config is not None:
            return self._config
        return AgentConfig(agent_key=agent_key, source="unavailable", reason="STUB")


def make_run(tmp_path, *, verification="on", agent_source=None):
    (tmp_path / "capabilities.yaml").write_text(
        textwrap.dedent(CONFIG).format(verification=verification), encoding="utf-8"
    )
    return initialize_run(
        ledger_root=tmp_path / "ledger",
        config_path=tmp_path / "capabilities.yaml",
        agent_source=agent_source or SnapshotAgentConfigs(SNAPSHOT),
        snapshot_path=tmp_path / "snap.json",
    )


def make_manifest(tmp_path, run):
    roster = load_roster(ROSTER)
    result = plan_run(
        run,
        roster,
        ledger_root=tmp_path / "ledger",
        quoter=RecordedQuoter.from_file(QUOTES),
        validator=RecordedAddressValidator.from_file(VALIDATIONS),
    )
    return result.manifest, roster


@pytest.fixture
def planned(tmp_path):
    """A run with verification off, so planning does not invoke D1 itself."""
    run = make_run(tmp_path, verification="off")
    manifest, roster = make_manifest(tmp_path, run)
    return run, manifest, roster


class TestTheGate:
    def test_verification_off_skips_without_calling_anything(self, planned, tmp_path):
        run, manifest, _ = planned
        model = StubModel()
        result = verify_manifest(
            run, manifest, ledger_root=tmp_path / "ledger", model=model
        )
        assert result.outcome == "skipped"
        assert model.calls == []

    def test_a_skipped_run_writes_no_invocation_record(self, planned, tmp_path):
        run, manifest, _ = planned
        verify_manifest(run, manifest, ledger_root=tmp_path / "ledger", model=StubModel())
        assert list(iter_records(tmp_path / "ledger", AgentInvocationRecord)) == []

    def test_an_unavailable_config_is_reported_not_raised(self, tmp_path):
        run = make_run(tmp_path, agent_source=StubAgentConfigs(None))
        manifest, _ = make_manifest(tmp_path, run)
        result = verify_manifest(
            run, manifest, ledger_root=tmp_path / "ledger", model=StubModel()
        )
        assert result.outcome == "unavailable"
        assert not result.ran

    def test_a_config_declaring_tools_refuses_to_run(self, tmp_path):
        # Design 6.4 mitigation 1: an instruction referencing a tool Python
        # does not offer must not fail mid-run. This agent is offered none.
        config = AgentConfig(
            agent_key=AGENT_KEY,
            enabled=True,
            instructions="check the manifest",
            model="claude-haiku-4-5-20251001",
            declared_tools=("shippo_validate",),
            source="launchdarkly",
            reason="FALLTHROUGH",
        )
        run = make_run(tmp_path, agent_source=StubAgentConfigs(config))
        manifest, _ = make_manifest(tmp_path, run)
        result = verify_manifest(
            run, manifest, ledger_root=tmp_path / "ledger", model=StubModel()
        )
        assert result.outcome == "unavailable"
        assert "shippo_validate" in result.reason


class TestLaunchDarklySuppliesTheAgent:
    def test_the_instructions_and_model_come_from_the_config(self, tmp_path):
        config = AgentConfig(
            agent_key=AGENT_KEY,
            enabled=True,
            instructions="INSTRUCTIONS FROM LAUNCHDARKLY",
            model="claude-sonnet-5",
            model_parameters={"temperature": 0.2, "max_tokens": 512},
            variation_key="experimental",
            version=7,
            source="launchdarkly",
            reason="FALLTHROUGH",
        )
        run = make_run(tmp_path, agent_source=StubAgentConfigs(config))
        manifest, _ = make_manifest(tmp_path, run)
        model = StubModel('{"findings": []}')
        verify_manifest(run, manifest, ledger_root=tmp_path / "ledger", model=model)

        invocation, _ = model.calls[0]
        assert invocation.instructions == "INSTRUCTIONS FROM LAUNCHDARKLY"
        assert invocation.model == "claude-sonnet-5"
        assert invocation.request_parameters() == {"temperature": 0.2, "max_tokens": 512}

    def test_an_unknown_model_parameter_is_dropped_not_forwarded(self, tmp_path):
        # LD is a delivery layer for values. A typo in the console should not
        # become a TypeError inside a shipping run.
        config = AgentConfig(
            agent_key=AGENT_KEY,
            enabled=True,
            instructions="x",
            model="claude-haiku-4-5-20251001",
            model_parameters={"temprature": 9, "temperature": 0.1},
            source="launchdarkly",
            reason="FALLTHROUGH",
        )
        invocation = Invocation.from_config(config, "x")
        assert "temprature" not in invocation.request_parameters()
        assert invocation.request_parameters()["temperature"] == 0.1

    def test_a_config_naming_no_model_refuses_rather_than_defaulting(self):
        config = AgentConfig(
            agent_key=AGENT_KEY, enabled=True, instructions="x", source="cache"
        )
        with pytest.raises(ModelUnavailable, match="names no model"):
            Invocation.from_config(config, "x")

    def test_the_invocation_record_names_the_config_a1_captured(self, tmp_path):
        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        result = verify_manifest(
            run,
            manifest,
            ledger_root=tmp_path / "ledger",
            model=RecordedModel.from_file(COMPLETIONS),
        )
        config = run.agent_configs[AGENT_KEY]
        assert result.record.instruction_hash == config.instruction_hash
        assert result.record.instruction_variation_key == config.variation_key
        assert result.record.instruction_version == config.version
        assert result.record.model == config.model


class TestAgainstTheRealReply:
    def test_the_recorded_reply_parses_into_findings(self, tmp_path):
        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        result = verify_manifest(
            run,
            manifest,
            ledger_root=tmp_path / "ledger",
            model=RecordedModel.from_file(COMPLETIONS),
        )
        assert result.outcome == "findings"
        assert result.findings
        assert result.clean_checks

    def test_the_real_reply_arrived_fenced_and_still_parsed(self, tmp_path):
        # The model wrapped its JSON in a ```json fence. Captured rather than
        # cleaned up, because that is what the configured model actually does.
        recorded = json.loads(COMPLETIONS.read_text(encoding="utf-8"))
        assert any("```" in row["text"] for row in recorded.values())

    def test_findings_are_grounded_in_manifest_fields(self, tmp_path):
        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        result = verify_manifest(
            run,
            manifest,
            ledger_root=tmp_path / "ledger",
            model=RecordedModel.from_file(COMPLETIONS),
        )
        assert all(f.evidence for f in result.findings)
        assert all(f.severity in {"blocker", "warning", "note"} for f in result.findings)


class TestTheBoundedBudget:
    def test_an_unparseable_reply_is_retried_once_with_the_error(self, planned, tmp_path):
        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        model = StubModel("not json at all", '{"findings": []}')
        result = verify_manifest(
            run, manifest, ledger_root=tmp_path / "ledger", model=model
        )
        assert result.outcome == "clean"
        assert result.iterations == 2
        # The nudge carries the parse error rather than repeating the ask.
        assert "could not be parsed" in model.calls[1][1]

    def test_two_bad_replies_end_as_unparseable_with_the_reply_kept(self, tmp_path):
        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        model = StubModel("nope", "still nope")
        result = verify_manifest(
            run, manifest, ledger_root=tmp_path / "ledger", model=model
        )
        assert result.outcome == "unparseable"
        assert result.raw == "still nope"
        assert len(model.calls) == 2

    def test_an_invocation_that_ran_is_recorded_even_when_unparseable(self, tmp_path):
        # An invocation that happened is a fact. A ledger recording only the
        # successful ones cannot answer what design 8 asks of this flag.
        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        verify_manifest(
            run,
            manifest,
            ledger_root=tmp_path / "ledger",
            model=StubModel("nope", "still nope"),
        )
        records = list(iter_records(tmp_path / "ledger", AgentInvocationRecord))
        assert [r.outcome for r in records] == ["unparseable"]
        assert records[0].iterations == 2

    def test_an_unreachable_model_is_not_retried(self, tmp_path):
        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        model = StubModel(ModelUnavailable("no key"))
        result = verify_manifest(
            run, manifest, ledger_root=tmp_path / "ledger", model=model
        )
        assert result.outcome == "unavailable"
        assert len(model.calls) == 1


class TestThePayload:
    def test_the_input_recipients_are_sent_so_accounting_can_be_checked(self, planned):
        _, manifest, roster = planned
        payload = manifest_payload(manifest, ("ana", "someone-dropped"))
        assert payload["input_recipients"] == ["ana", "someone-dropped"]

    def test_every_row_carries_the_fields_the_checks_name(self, planned):
        _, manifest, _ = planned
        row = manifest_payload(manifest)["eligible"][0]
        for field in (
            "recipient_key",
            "validated_address",
            "cost",
            "ship_date",
            "expected_arrival",
            "thermal_margin",
            "box_size",
            "carrier",
        ):
            assert field in row

    def test_the_payload_is_json_serializable(self, planned):
        _, manifest, _ = planned
        assert json.loads(json.dumps(manifest_payload(manifest)))


class TestFindingCoercion:
    def test_a_finding_with_no_claim_is_dropped(self, tmp_path):
        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        reply = json.dumps(
            {"findings": [{"check": "Cost outliers", "severity": "note", "problem": ""}]}
        )
        result = verify_manifest(
            run, manifest, ledger_root=tmp_path / "ledger", model=StubModel(reply)
        )
        assert result.outcome == "clean"

    def test_an_unknown_severity_becomes_a_note_rather_than_a_blocker(self, tmp_path):
        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        reply = json.dumps(
            {"findings": [{"check": "x", "severity": "catastrophe", "problem": "y"}]}
        )
        result = verify_manifest(
            run, manifest, ledger_root=tmp_path / "ledger", model=StubModel(reply)
        )
        assert result.findings[0].severity == "note"
        assert result.blockers == ()

    def test_a_blocker_is_surfaced_as_one(self, tmp_path):
        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        reply = json.dumps(
            {
                "findings": [
                    {"check": "Carrier count", "severity": "blocker", "problem": "3 carriers"}
                ]
            }
        )
        result = verify_manifest(
            run, manifest, ledger_root=tmp_path / "ledger", model=StubModel(reply)
        )
        assert len(result.blockers) == 1


class TestMetrics:
    def test_tokens_and_success_are_reported(self, tmp_path):
        class Spy:
            def __init__(self):
                self.events = []

            def track_success(self):
                self.events.append("success")

            def track_error(self):
                self.events.append("error")

            def track_tokens(self, input_tokens, output_tokens):
                self.events.append(("tokens", input_tokens, output_tokens))

        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        spy = Spy()
        verify_manifest(
            run,
            manifest,
            ledger_root=tmp_path / "ledger",
            model=StubModel('{"findings": []}'),
            metrics=spy,
        )
        assert ("tokens", 10, 5) in spy.events
        assert "success" in spy.events

    def test_findings_still_count_as_a_successful_invocation(self, tmp_path):
        # Success is about the invocation, not the manifest. An agent that
        # correctly reports six blockers did its job; conflating the two would
        # make the metric reward silence.
        class Spy:
            def __init__(self):
                self.events = []

            def track_success(self):
                self.events.append("success")

            def track_error(self):
                self.events.append("error")

            def track_tokens(self, input_tokens, output_tokens):
                pass

        run = make_run(tmp_path)
        manifest, _ = make_manifest(tmp_path, run)
        spy = Spy()
        reply = json.dumps(
            {"findings": [{"check": "x", "severity": "blocker", "problem": "bad"}]}
        )
        verify_manifest(
            run,
            manifest,
            ledger_root=tmp_path / "ledger",
            model=StubModel(reply),
            metrics=spy,
        )
        assert spy.events == ["success"]
