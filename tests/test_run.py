import json
import textwrap

import pytest

from bbq_shipment_agent.capabilities import (
    AuthorityLevel,
    KillSwitchEngaged,
    PlannerMode,
)
from bbq_shipment_agent.agent_configs import AGENT_KEYS, AgentConfig
from bbq_shipment_agent.context import STAGE_MANIFEST_VERIFICATION
from bbq_shipment_agent.ledger import (
    AgentInvocationRecord,
    RunRecord,
    iter_records,
    rebuild,
    utc_now,
)
from bbq_shipment_agent.run import (
    CAPABILITY_FLAGS,
    FlagPayload,
    LaunchDarklyProvider,
    OfflineProvider,
    count_shadow_runs,
    initialize_run,
    launchdarkly_client,
    payload_hash,
    record_agent_invocation,
)

CONFIG = """
    profiles:
      baseline:
        planner: "off"
        memory: "off"
        verification: "off"
        authority: "propose_only"
      planner_trial:
        planner: "shadow"
        memory: "read"
        verification: "on"
        authority: "propose_only"
      full:
        planner: "on"
        memory: "read_write"
        verification: "on"
        authority: "purchase_labels"
    default_profile: "baseline"
    authority_ceiling: "propose_only"
    kill_switch: {kill}
    prerequisites:
      - id: planner_on_needs_shadow_history
        when: {{planner: "on"}}
        requires: {{shadow_runs_at_least: 5}}
        demote: {{planner: "shadow"}}
"""


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "capabilities.yaml"
    path.write_text(textwrap.dedent(CONFIG.format(kill="false")), encoding="utf-8")
    return path


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "ledger"


class ExplodingProvider:
    """Fails if consulted. Proves ordering, not behavior."""

    def fetch(self, context):
        raise AssertionError("provider was contacted")


class StubProvider:
    def __init__(self, **overrides):
        self.overrides = overrides
        self.seen = None

    def fetch(self, context):
        self.seen = context
        return FlagPayload(
            overrides=self.overrides,
            hash="payload-abc",
            source="launchdarkly",
            reason="TARGET_MATCH",
        )


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
    """Stands in for an LD client serving the capability flags."""

    def __init__(self, values=None, reason=None):
        self.values = values or {}
        self.reason = reason
        self.asked = []

    def variation_detail(self, key, context, default):
        self.asked.append(key)
        return FlagDetail(self.values.get(key, default), self.reason)


class TestKillSwitch:
    def test_it_aborts_the_run(self, tmp_path, ledger):
        path = tmp_path / "killed.yaml"
        path.write_text(textwrap.dedent(CONFIG.format(kill="true")), encoding="utf-8")
        with pytest.raises(KillSwitchEngaged):
            initialize_run(ledger_root=ledger, config_path=path)

    def test_it_is_read_before_any_provider_is_contacted(self, tmp_path, ledger):
        # An operator disabling the pipeline should not be racing a network call.
        path = tmp_path / "killed.yaml"
        path.write_text(textwrap.dedent(CONFIG.format(kill="true")), encoding="utf-8")
        with pytest.raises(KillSwitchEngaged):
            initialize_run(
                ledger_root=ledger, config_path=path, provider=ExplodingProvider()
            )

    def test_an_aborted_run_writes_nothing(self, tmp_path, ledger):
        path = tmp_path / "killed.yaml"
        path.write_text(textwrap.dedent(CONFIG.format(kill="true")), encoding="utf-8")
        with pytest.raises(KillSwitchEngaged):
            initialize_run(ledger_root=ledger, config_path=path)
        assert list(iter_records(ledger, RunRecord)) == []


class TestOfflinePath:
    def test_no_provider_falls_back_to_the_config_profile(self, ledger, config_path):
        run = initialize_run(ledger_root=ledger, config_path=config_path)
        assert run.capabilities.planner is PlannerMode.OFF
        assert run.capabilities.authority is AuthorityLevel.PROPOSE_ONLY

    def test_the_fallback_is_recorded_as_such(self, ledger, config_path):
        run = initialize_run(ledger_root=ledger, config_path=config_path)
        reasons = run.evaluation_reasons()
        assert reasons["flag_payload_source"] == "unavailable"
        assert reasons["flag_payload_reason"] == "NO_SDK_KEY"

    def test_offline_still_records_a_real_payload_hash(self, ledger, config_path):
        # A comparable value, not a magic string — `source` says where it came from.
        run = initialize_run(ledger_root=ledger, config_path=config_path)
        assert run.payload.hash == payload_hash({})

    def test_a_custom_offline_reason_is_carried_through(self, ledger, config_path):
        run = initialize_run(
            ledger_root=ledger,
            config_path=config_path,
            provider=OfflineProvider(reason="LD_UNREACHABLE"),
        )
        assert run.evaluation_reasons()["flag_payload_reason"] == "LD_UNREACHABLE"


class TestAuthorityIsNotNegotiable:
    def test_a_provider_cannot_raise_authority(self, ledger, config_path):
        provider = StubProvider(authority="purchase_labels")
        run = initialize_run(
            ledger_root=ledger, config_path=config_path, provider=provider
        )
        assert run.capabilities.authority is AuthorityLevel.PROPOSE_ONLY
        assert run.resolved.reasons["authority"].startswith("CLAMPED_TO_CEILING")

    def test_a_profile_cannot_raise_authority_either(self, ledger, config_path):
        run = initialize_run(
            ledger_root=ledger, config_path=config_path, profile="full"
        )
        assert run.capabilities.authority is AuthorityLevel.PROPOSE_ONLY


class TestContextConstruction:
    def test_the_provider_receives_a_multi_context(self, ledger, config_path):
        provider = StubProvider()
        initialize_run(
            ledger_root=ledger,
            config_path=config_path,
            provider=provider,
            campaign="aug-cook",
            packet_count=22,
        )
        assert provider.seen["kind"] == "multi"
        assert provider.seen["run"]["campaign"] == "aug-cook"
        assert provider.seen["run"]["packet_count"] == 22
        assert provider.seen["stage"]["key"] == "run_init"

    def test_later_stages_reuse_the_run_identity(self, ledger, config_path):
        run = initialize_run(
            ledger_root=ledger, config_path=config_path, packet_count=22
        )
        context = run.context_for_stage(STAGE_MANIFEST_VERIFICATION)
        assert context["run"]["key"] == run.run_id
        assert context["stage"]["key"] == "manifest_verification"
        assert "shipment" not in context


class TestLedgerRecording:
    def test_a_run_row_lands_with_its_capability_fingerprint(self, ledger, config_path):
        run = initialize_run(
            ledger_root=ledger, config_path=config_path, packet_count=22
        )
        connection = rebuild(ledger)
        row = connection.execute(
            "SELECT run_id, profile, cap_fingerprint, packet_count, "
            "flag_payload_hash, started_at IS NOT NULL FROM runs"
        ).fetchone()
        assert row == (
            run.run_id,
            "baseline",
            run.cap_fingerprint,
            22,
            run.payload.hash,
            True,
        )

    def test_the_snapshot_is_queryable(self, ledger, config_path):
        initialize_run(
            ledger_root=ledger, config_path=config_path, profile="planner_trial"
        )
        connection = rebuild(ledger)
        assert connection.execute(
            "SELECT json_extract_string(cap_snapshot, '$.capabilities.planner') FROM runs"
        ).fetchone() == ("shadow",)

    def test_evaluation_reasons_explain_the_run(self, ledger, config_path):
        initialize_run(
            ledger_root=ledger,
            config_path=config_path,
            provider=StubProvider(memory="read_write"),
        )
        connection = rebuild(ledger)
        assert connection.execute(
            "SELECT json_extract_string(evaluation_reasons, '$.capabilities.memory') "
            "FROM runs"
        ).fetchone() == ("FLAG_OVERRIDE",)

    def test_runs_get_distinct_ids(self, ledger, config_path):
        ids = {
            initialize_run(ledger_root=ledger, config_path=config_path).run_id
            for _ in range(5)
        }
        assert len(ids) == 5


class TestShadowRunHistory:
    def test_an_empty_ledger_counts_zero(self, ledger):
        assert count_shadow_runs(ledger) == 0

    def test_only_completed_shadow_runs_count(self, ledger, config_path):
        from bbq_shipment_agent.ledger import LedgerWriter

        run = initialize_run(
            ledger_root=ledger, config_path=config_path, profile="planner_trial"
        )
        assert count_shadow_runs(ledger) == 0, "not completed yet"

        LedgerWriter(ledger).append(
            RunRecord(run_id=run.run_id, completed_at=utc_now())
        )
        assert count_shadow_runs(ledger) == 1

    def test_non_shadow_runs_do_not_count(self, ledger, config_path):
        from bbq_shipment_agent.ledger import LedgerWriter

        run = initialize_run(ledger_root=ledger, config_path=config_path)
        LedgerWriter(ledger).append(
            RunRecord(run_id=run.run_id, completed_at=utc_now())
        )
        assert count_shadow_runs(ledger) == 0

    def test_history_gates_planner_promotion(self, ledger, config_path):
        from bbq_shipment_agent.ledger import LedgerWriter

        writer = LedgerWriter(ledger)
        for _ in range(5):
            run = initialize_run(
                ledger_root=ledger, config_path=config_path, profile="planner_trial"
            )
            writer.append(RunRecord(run_id=run.run_id, completed_at=utc_now()))
        assert count_shadow_runs(ledger) == 5

        promoted = initialize_run(
            ledger_root=ledger, config_path=config_path, profile="full"
        )
        assert promoted.capabilities.planner is PlannerMode.ON

    def test_without_history_planner_is_demoted_to_shadow(self, ledger, config_path):
        run = initialize_run(
            ledger_root=ledger, config_path=config_path, profile="full"
        )
        assert run.capabilities.planner is PlannerMode.SHADOW
        assert run.resolved.reasons["planner"].startswith("PREREQUISITE_UNMET")


class TestLaunchDarklyProvider:
    def _context(self, ledger, config_path):
        run = initialize_run(ledger_root=ledger, config_path=config_path)
        return run.context_for_stage("run_init")

    def test_flag_keys_map_onto_capability_names(self, ledger, config_path):
        client = FlagClient({
            "planner-mode": "shadow",
            "memory-mode": "read",
            "verification-enabled": "on",
        })
        payload = LaunchDarklyProvider(client).fetch(
            self._context(ledger, config_path)
        )
        assert payload.overrides == {
            "planner": "shadow", "memory": "read", "verification": "on"
        }
        assert payload.source == "launchdarkly"

    def test_it_never_asks_for_authority(self, ledger, config_path):
        # Design 6.5: authority lives in repo config so a change is a visible
        # commit. The provider is not offered the chance to propose one.
        client = FlagClient()
        LaunchDarklyProvider(client).fetch(self._context(ledger, config_path))
        assert "authority-level" not in client.asked
        assert "authority" not in CAPABILITY_FLAGS.values()

    def test_an_absent_flag_proposes_nothing(self, ledger, config_path):
        # An absent flag is not an instruction to change anything, so the
        # profile's value stands rather than being overwritten with a default.
        client = FlagClient({"planner-mode": "shadow"})
        payload = LaunchDarklyProvider(client).fetch(
            self._context(ledger, config_path)
        )
        assert payload.overrides == {"planner": "shadow"}

    def test_the_reason_names_every_flag_evaluated(self, ledger, config_path):
        client = FlagClient(
            {"planner-mode": "shadow"},
            reason={"kind": "RULE_MATCH", "ruleId": "r-9"},
        )
        payload = LaunchDarklyProvider(client).fetch(
            self._context(ledger, config_path)
        )
        assert "planner-mode:RULE_MATCH:r-9" in payload.reason
        assert "memory-mode:RULE_MATCH:r-9" in payload.reason

    def test_the_hash_covers_what_was_proposed(self, ledger, config_path):
        client = FlagClient({"memory-mode": "read"})
        payload = LaunchDarklyProvider(client).fetch(
            self._context(ledger, config_path)
        )
        assert payload.hash == payload_hash({"memory": "read"})

    def test_a_proposal_still_cannot_raise_authority(self, ledger, config_path):
        # Defense in depth: even if a provider proposed one, the clamp runs.
        run = initialize_run(
            ledger_root=ledger,
            config_path=config_path,
            provider=StubProvider(authority="purchase_labels"),
            profile="planner_trial",
        )
        assert run.capabilities.authority is AuthorityLevel.PROPOSE_ONLY


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
    def test_every_agent_is_retrieved_at_run_start(self, ledger, config_path, tmp_path):
        run = initialize_run(
            ledger_root=ledger,
            config_path=config_path,
            agent_source=StubAgentConfigs(),
            snapshot_path=tmp_path / "snap.json",
        )
        assert set(run.agent_configs) == set(AGENT_KEYS)

    def test_each_agent_gets_its_own_stage_context(self, ledger, config_path, tmp_path):
        source = StubAgentConfigs()
        run = initialize_run(
            ledger_root=ledger,
            config_path=config_path,
            agent_source=source,
            snapshot_path=tmp_path / "snap.json",
        )
        stages = {
            key: ctx["stage"]["key"] for key, ctx in source.seen.items()
        }
        assert stages["manifest-verification"] == "manifest_verification"
        assert len(set(stages.values())) == len(AGENT_KEYS)
        # And every one of them under this run's identity.
        assert {ctx["run"]["key"] for ctx in source.seen.values()} == {run.run_id}

    def test_the_offline_default_retrieves_nothing_usable(self, ledger, config_path):
        run = initialize_run(ledger_root=ledger, config_path=config_path)
        assert set(run.agent_configs) == set(AGENT_KEYS)
        assert not any(c.available for c in run.agent_configs.values())

    def test_the_snapshot_is_written_when_configs_arrive(
        self, ledger, config_path, tmp_path
    ):
        path = tmp_path / "snap.json"
        initialize_run(
            ledger_root=ledger,
            config_path=config_path,
            agent_source=StubAgentConfigs(),
            snapshot_path=path,
        )
        assert path.exists()
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert set(stored["agents"]) == set(AGENT_KEYS)

    def test_an_offline_run_writes_no_snapshot_at_all(
        self, ledger, config_path, tmp_path
    ):
        # Not merely "writes nothing usable": the file must not be created,
        # or an offline run would leave an empty cache where none existed.
        path = tmp_path / "snap.json"
        initialize_run(
            ledger_root=ledger,
            config_path=config_path,
            agent_source=StubAgentConfigs(available=False),
            snapshot_path=path,
        )
        assert not path.exists()


class TestAgentInvocationRecording:
    def _run(self, ledger, config_path, tmp_path):
        return initialize_run(
            ledger_root=ledger,
            config_path=config_path,
            agent_source=StubAgentConfigs(),
            snapshot_path=tmp_path / "snap.json",
        )

    def test_the_instruction_identity_lands_in_the_ledger(
        self, ledger, config_path, tmp_path
    ):
        run = self._run(ledger, config_path, tmp_path)
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

    def test_the_hash_describes_the_text_captured_at_run_start(
        self, ledger, config_path, tmp_path
    ):
        # Re-reading LD at write time would attribute the behavior to whatever
        # the console is serving now, which is the confusion the hash prevents.
        run = self._run(ledger, config_path, tmp_path)
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

    def test_two_invocations_are_two_facts(self, ledger, config_path, tmp_path):
        # No merge key: a second call is not a correction of the first.
        run = self._run(ledger, config_path, tmp_path)
        record_agent_invocation(ledger, run, "manifest-verification", outcome="fail")
        record_agent_invocation(ledger, run, "manifest-verification", outcome="pass")
        connection = rebuild(ledger)
        assert connection.execute(
            "SELECT count(*) FROM agent_invocations"
        ).fetchone() == (2,)

    def test_an_unretrieved_agent_is_refused(self, ledger, config_path, tmp_path):
        run = self._run(ledger, config_path, tmp_path)
        with pytest.raises(KeyError, match="no config was retrieved"):
            record_agent_invocation(ledger, run, "carrier-selection", outcome="pass")
        assert list(iter_records(ledger, AgentInvocationRecord)) == []
