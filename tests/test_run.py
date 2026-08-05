import json

import pytest

from bbq_shipment_agent.capabilities import (
    CAPABILITY_FLAGS,
    KILL_SWITCH_FLAG,
    FlagEvaluation,
    KillSwitchEngaged,
    LaunchDarklyGate,
    PlannerMode,
    ValidationMode,
    VerificationMode,
)
from bbq_shipment_agent.agent_configs import LD_CONFIGURED_KEYS, AgentConfig
from bbq_shipment_agent.context import STAGE_MANIFEST_VERIFICATION
from bbq_shipment_agent.ledger import (
    AgentInvocationRecord,
    CapabilityEvaluationRecord,
    RunRecord,
    iter_records,
    rebuild,
)
from bbq_shipment_agent.run import (
    initialize_run,
    launchdarkly_client,
    record_agent_invocation,
)


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "ledger"


class RecordingGate:
    """Serves fixed values and records the context each flag was seen under.

    The single live seam now, so it stands in for both the kill switch (under
    `run_init`) and the capability flags (each under its own stage).
    """

    def __init__(self, values=None, reason="RULE_MATCH:r1"):
        self.values = values or {}
        self.reason = reason
        self.seen: dict[str, dict] = {}

    def evaluate(self, flag_key, context, default):
        self.seen[flag_key] = context
        return FlagEvaluation(
            value=self.values.get(flag_key, default),
            source="launchdarkly",
            reason=self.reason,
        )


class ExplodingGate:
    """Fails if consulted after the kill switch. Proves ordering."""

    def __init__(self, kill=True):
        self._kill = kill
        self.calls = 0

    def evaluate(self, flag_key, context, default):
        self.calls += 1
        if flag_key == KILL_SWITCH_FLAG:
            return FlagEvaluation(self._kill, "launchdarkly", "RULE_MATCH:kill")
        raise AssertionError(f"gate contacted for {flag_key} after the kill switch")


class StubAgentConfigs:
    """Serves one prepared config for every agent, recording the contexts."""

    def __init__(self, available=True, **fields):
        self.available = available
        self.fields = fields
        self.seen = {}

    def fetch(self, agent_key, context):
        self.seen[agent_key] = context
        if not self.available:
            return AgentConfig(agent_key=agent_key, source="unavailable")
        return AgentConfig(
            agent_key=agent_key,
            enabled=True,
            instructions=f"instructions for {agent_key}",
            variation_key="v3",
            version=7,
            model="claude-sonnet-5",
            source="launchdarkly",
            reason="RULE_MATCH:r-1",
            **self.fields,
        )


class FlagDetail:
    def __init__(self, value, reason=None):
        self.value = value
        self.reason = reason if reason is not None else {"kind": "FALLTHROUGH"}


class FlagClient:
    """Stands in for a real LD client, wrapped by `LaunchDarklyGate`."""

    def __init__(self, values=None, reason=None):
        self.values = values or {}
        self.reason = reason
        self.asked = []

    def variation_detail(self, key, context, default):
        self.asked.append(key)
        return FlagDetail(self.values.get(key, default), self.reason)


class TestKillSwitch:
    def test_it_aborts_the_run(self, ledger):
        gate = RecordingGate({KILL_SWITCH_FLAG: True})
        with pytest.raises(KillSwitchEngaged):
            initialize_run(ledger_root=ledger, gate=gate)

    def test_it_is_evaluated_before_anything_else(self, ledger):
        # An engaged kill switch stops the run before any agent config is
        # fetched or capability evaluated -- the gate is asked for the kill
        # switch and nothing after it.
        gate = ExplodingGate(kill=True)
        with pytest.raises(KillSwitchEngaged):
            initialize_run(ledger_root=ledger, gate=gate)
        assert gate.calls == 1

    def test_an_aborted_run_writes_nothing(self, ledger):
        gate = RecordingGate({KILL_SWITCH_FLAG: True})
        with pytest.raises(KillSwitchEngaged):
            initialize_run(ledger_root=ledger, gate=gate)
        assert list(iter_records(ledger, RunRecord)) == []

    def test_off_by_default_offline(self, ledger):
        # No gate injected -> OfflineGate -> kill switch defaults to off, so an
        # LD outage does not brick an offline run.
        run = initialize_run(ledger_root=ledger)
        assert run.kill_switch.value is False
        assert run.kill_switch.source == "offline"


class TestOfflinePath:
    def test_capabilities_fall_back_to_the_defaults(self, ledger):
        run = initialize_run(ledger_root=ledger)
        assert run.planner() is PlannerMode.OFF
        assert run.validation() is ValidationMode.STANDARD
        assert run.verification() is VerificationMode.OFF

    def test_an_evaluation_is_recorded_as_offline(self, ledger):
        run = initialize_run(ledger_root=ledger)
        run.validation()
        (record,) = list(iter_records(ledger, CapabilityEvaluationRecord))
        assert record.flag == "validation-mode"
        assert record.value == "standard"
        assert record.source == "offline"

    def test_a_custom_offline_reason_is_carried_through(self, ledger):
        from bbq_shipment_agent.capabilities import OfflineGate

        run = initialize_run(ledger_root=ledger, gate=OfflineGate(reason="LD_UNREACHABLE"))
        run.planner()
        assert run.cap_reasons()["planner"] == "offline:LD_UNREACHABLE"
        assert run.kill_switch.reason == "LD_UNREACHABLE"


class TestLiveEvaluation:
    def test_a_capability_takes_the_served_value(self, ledger):
        gate = RecordingGate({"planner-mode": "shadow"})
        run = initialize_run(ledger_root=ledger, gate=gate)
        assert run.planner() is PlannerMode.SHADOW
        assert run.cap_sources()["planner"] == "launchdarkly"

    def test_each_capability_is_evaluated_under_its_own_stage(self, ledger):
        gate = RecordingGate()
        run = initialize_run(ledger_root=ledger, gate=gate)
        run.planner()
        run.validation()
        run.verification()
        assert gate.seen["pipeline-kill-switch"]["stage"]["key"] == "run_init"
        assert gate.seen["planner-mode"]["stage"]["key"] == "address_repair"
        assert gate.seen["validation-mode"]["stage"]["key"] == "address_validation"
        assert gate.seen["verification-enabled"]["stage"]["key"] == "manifest_verification"

    def test_a_capability_is_evaluated_once_and_cached(self, ledger):
        # A second ask returns the cache: one value per capability per run, and
        # one evaluation record, not two.
        gate = RecordingGate({"planner-mode": "shadow"})
        run = initialize_run(ledger_root=ledger, gate=gate)
        assert run.planner() is run.planner()
        records = [
            r for r in iter_records(ledger, CapabilityEvaluationRecord)
            if r.flag == "planner-mode"
        ]
        assert len(records) == 1

    def test_only_the_known_flags_are_ever_asked(self, ledger):
        # authority-level and memory-mode are gone; a live run never asks for
        # them, so a console still holding them costs nothing.
        client = FlagClient()
        gate = LaunchDarklyGate(client)
        run = initialize_run(ledger_root=ledger, gate=gate)
        run.planner()
        run.validation()
        run.verification()
        assert set(client.asked) == {KILL_SWITCH_FLAG, *CAPABILITY_FLAGS}
        assert set(CAPABILITY_FLAGS.values()) == {"planner", "validation", "verification"}


class TestContextConstruction:
    def test_the_gate_receives_a_multi_context(self, ledger):
        gate = RecordingGate()
        initialize_run(
            ledger_root=ledger, gate=gate, campaign="aug-cook", packet_count=22
        )
        context = gate.seen[KILL_SWITCH_FLAG]
        assert context["kind"] == "multi"
        assert context["run"]["campaign"] == "aug-cook"
        assert context["run"]["packet_count"] == 22
        assert context["stage"]["key"] == "run_init"

    def test_later_stages_reuse_the_run_identity(self, ledger):
        run = initialize_run(ledger_root=ledger, packet_count=22)
        context = run.context_for_stage(STAGE_MANIFEST_VERIFICATION)
        assert context["run"]["key"] == run.run_id
        assert context["stage"]["key"] == "manifest_verification"
        assert "shipment" not in context


class TestLedgerRecording:
    def test_a1_opens_a_run_row_without_a_fingerprint_yet(self, ledger):
        # Capabilities are evaluated live per stage, so A1 does not know them.
        # The row opens with profile and started_at; the fingerprint lands at
        # planning.
        run = initialize_run(ledger_root=ledger, packet_count=22)
        connection = rebuild(ledger)
        row = connection.execute(
            "SELECT run_id, profile, cap_fingerprint, packet_count, "
            "started_at IS NOT NULL FROM runs"
        ).fetchone()
        assert row == (run.run_id, "baseline", None, 22, True)

    def test_the_fingerprint_folds_the_evaluated_set(self, ledger):
        run = initialize_run(ledger_root=ledger)
        assert run.cap_fingerprint is None, "nothing evaluated yet"
        run.planner()
        run.validation()
        assert run.cap_fingerprint is None, "still partial"
        run.verification()
        assert run.cap_fingerprint is not None

    def test_the_snapshot_is_the_folded_view(self, ledger):
        gate = RecordingGate({"planner-mode": "shadow"})
        run = initialize_run(ledger_root=ledger, gate=gate)
        run.planner()
        run.validation()
        run.verification()
        snapshot = run.cap_snapshot()
        assert snapshot["capabilities"]["planner"] == "shadow"
        assert snapshot["reasons"]["planner"] == "launchdarkly:RULE_MATCH:r1"

    def test_runs_get_distinct_ids(self, ledger):
        ids = {initialize_run(ledger_root=ledger).run_id for _ in range(5)}
        assert len(ids) == 5


class TestSdkBootstrap:
    def test_an_empty_key_is_unconfigured_not_a_client(self, monkeypatch):
        # UV_ENV_FILE seeds .env from .env.example, so an unset key arrives as
        # "" rather than absent. Handing that to the SDK blocks for the whole
        # timeout and then fails.
        monkeypatch.setenv("LD_SDK_KEY", "")
        assert launchdarkly_client() is None

    def test_an_absent_key_is_also_unconfigured(self, monkeypatch):
        monkeypatch.delenv("LD_SDK_KEY", raising=False)
        assert launchdarkly_client() is None

    def test_an_explicit_empty_key_does_not_reach_the_sdk(self):
        assert launchdarkly_client(sdk_key="") is None


class TestAgentConfigRetrieval:
    def test_every_agent_is_retrieved_at_run_start(self, ledger, tmp_path):
        run = initialize_run(
            ledger_root=ledger,
            agent_source=StubAgentConfigs(),
            snapshot_path=tmp_path / "snap.json",
        )
        assert set(run.agent_configs) == set(LD_CONFIGURED_KEYS)

    def test_each_agent_gets_its_own_stage_context(self, ledger, tmp_path):
        source = StubAgentConfigs()
        run = initialize_run(
            ledger_root=ledger,
            agent_source=source,
            snapshot_path=tmp_path / "snap.json",
        )
        stages = {key: ctx["stage"]["key"] for key, ctx in source.seen.items()}
        assert stages["manifest-verification"] == "manifest_verification"
        assert len(set(stages.values())) == len(LD_CONFIGURED_KEYS)
        # And every one of them under this run's identity.
        assert {ctx["run"]["key"] for ctx in source.seen.values()} == {run.run_id}

    def test_the_offline_default_retrieves_nothing_usable(self, ledger):
        run = initialize_run(ledger_root=ledger)
        assert set(run.agent_configs) == set(LD_CONFIGURED_KEYS)
        assert not any(c.available for c in run.agent_configs.values())

    def test_the_snapshot_is_written_when_configs_arrive(self, ledger, tmp_path):
        path = tmp_path / "snap.json"
        initialize_run(
            ledger_root=ledger,
            agent_source=StubAgentConfigs(),
            snapshot_path=path,
        )
        assert path.exists()
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert set(stored["agents"]) == set(LD_CONFIGURED_KEYS)

    def test_an_offline_run_writes_no_snapshot_at_all(self, ledger, tmp_path):
        # Not merely "writes nothing usable": the file must not be created,
        # or an offline run would leave an empty cache where none existed.
        path = tmp_path / "snap.json"
        initialize_run(
            ledger_root=ledger,
            agent_source=StubAgentConfigs(available=False),
            snapshot_path=path,
        )
        assert not path.exists()


class TestAgentInvocationRecording:
    def _run(self, ledger, tmp_path):
        return initialize_run(
            ledger_root=ledger,
            agent_source=StubAgentConfigs(),
            snapshot_path=tmp_path / "snap.json",
        )

    def test_the_instruction_identity_lands_in_the_ledger(self, ledger, tmp_path):
        run = self._run(ledger, tmp_path)
        record_agent_invocation(
            ledger, run, "manifest-verification", outcome="pass", iterations=2
        )
        connection = rebuild(ledger)
        assert connection.execute(
            "SELECT agent_key, instruction_variation_key, instruction_version, "
            "instruction_hash, model, iterations, outcome FROM agent_invocations"
        ).fetchone() == (
            "manifest-verification",
            "v3",
            7,
            run.agent_configs["manifest-verification"].instruction_hash,
            "claude-sonnet-5",
            2,
            "pass",
        )

    def test_the_hash_describes_the_text_captured_at_run_start(self, ledger, tmp_path):
        # Re-reading LD at write time would attribute the behavior to whatever
        # the console is serving now, which is the confusion the hash prevents.
        run = self._run(ledger, tmp_path)
        config = run.agent_configs["manifest-verification"]
        run.agent_configs["manifest-verification"] = AgentConfig(
            agent_key="manifest-verification",
            enabled=True,
            instructions="edited after the run began",
        )
        record = record_agent_invocation(
            ledger, run, "manifest-verification", outcome="pass"
        )
        assert record.instruction_hash != config.instruction_hash

    def test_two_invocations_are_two_facts(self, ledger, tmp_path):
        # No merge key: a second call is not a correction of the first.
        run = self._run(ledger, tmp_path)
        record_agent_invocation(ledger, run, "manifest-verification", outcome="fail")
        record_agent_invocation(ledger, run, "manifest-verification", outcome="pass")
        connection = rebuild(ledger)
        assert connection.execute(
            "SELECT count(*) FROM agent_invocations"
        ).fetchone() == (2,)

    def test_the_tool_trace_lands_in_the_ledger(self, ledger, tmp_path):
        # Call order and repeats are kept: "validated twice" is the fact worth
        # having, and a set would throw it away.
        run = self._run(ledger, tmp_path)
        record_agent_invocation(
            ledger,
            run,
            "manifest-verification",
            outcome="pass",
            tools_offered=["validate_address", "read_image_region"],
            tools_called=["read_image_region", "validate_address", "validate_address"],
        )
        connection = rebuild(ledger)
        assert connection.execute(
            "SELECT tools_offered, tools_called FROM agent_invocations"
        ).fetchone() == (
            ["validate_address", "read_image_region"],
            ["read_image_region", "validate_address", "validate_address"],
        )

    def test_offered_nothing_and_no_tool_loop_are_different_lines(self, ledger, tmp_path):
        # The ledger's usual distinction: an absent key says this append knows
        # nothing about the field, an empty list is a measurement.
        run = self._run(ledger, tmp_path)
        offered_nothing = record_agent_invocation(
            ledger, run, "manifest-verification", outcome="pass",
            tools_offered=[], tools_called=[],
        )
        no_loop = record_agent_invocation(
            ledger, run, "manifest-verification", outcome="pass"
        )
        assert offered_nothing.tools_offered == []
        assert no_loop.tools_offered is None
        assert "tools_offered" in offered_nothing.to_dict()
        assert "tools_offered" not in no_loop.to_dict()

    def test_an_unretrieved_agent_is_refused(self, ledger, tmp_path):
        run = self._run(ledger, tmp_path)
        with pytest.raises(KeyError, match="no config was retrieved"):
            record_agent_invocation(ledger, run, "carrier-selection", outcome="pass")
        assert list(iter_records(ledger, AgentInvocationRecord)) == []
