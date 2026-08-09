import json

import pytest

from bbq_shipment_agent.agent_configs import (
    EXTRACTION_KEY,
    LD_CONFIGURED_KEYS,
    SNAPSHOT_SCHEMA_VERSION,
    AgentConfig,
    AgentConfigError,
    ChainedAgentConfigs,
    LaunchDarklyAgentConfigs,
    OfflineAgentConfigs,
    SnapshotAgentConfigs,
    archived_variations,
    fetch_agent_configs,
    fetch_extraction_configs,
    snapshot_document,
    write_snapshot,
)
from bbq_shipment_agent.context import ContextBuilder, ImageIdentity

AGENT = "manifest-verification"


def variation(instructions="Check the manifest.", enabled=True, **overrides):
    """An AI Config value shaped the way LaunchDarkly serves one."""
    value = {
        "_ldMeta": {"enabled": enabled, "variationKey": "v3", "version": 7},
        "model": {"name": "claude-sonnet-5", "parameters": {"temperature": 0.2}},
        "instructions": instructions,
        "tools": {"manifest_read": {}, "cost_table_read": {}},
    }
    value.update(overrides)
    return value


class FakeDetail:
    def __init__(self, value, reason=None, is_default=False):
        self.value = value
        self.reason = reason if reason is not None else {"kind": "FALLTHROUGH"}
        self._is_default = is_default

    def is_default_value(self):
        return self._is_default


class FakeClient:
    """Stands in for an LD client and records what it was asked."""

    def __init__(self, values=None, reason=None):
        self.values = values or {}
        self.reason = reason
        self.seen = []

    def variation_detail(self, key, context, default):
        self.seen.append((key, context))
        if key not in self.values:
            return FakeDetail(default, self.reason, is_default=True)
        return FakeDetail(self.values[key], self.reason)


@pytest.fixture
def context():
    return ContextBuilder(run_id="r1", profile="baseline").for_stage(
        "manifest_verification"
    )


class TestInstructionHash:
    def test_no_instructions_means_no_hash(self):
        assert AgentConfig(agent_key=AGENT).instruction_hash is None

    def test_identical_text_hashes_identically(self):
        a = AgentConfig(agent_key=AGENT, instructions="same")
        b = AgentConfig(agent_key="review-narrator", instructions="same")
        # Keyed on the text, not on which agent holds it: the same instruction
        # served to two agents is the same text and must not look like two.
        assert a.instruction_hash == b.instruction_hash

    def test_edited_text_changes_the_hash(self):
        a = AgentConfig(agent_key=AGENT, instructions="before")
        b = AgentConfig(agent_key=AGENT, instructions="before ")
        assert a.instruction_hash != b.instruction_hash

    def test_enabled_but_textless_is_not_available(self):
        # Running an agent on an empty prompt is worse than not running it.
        config = AgentConfig(agent_key=AGENT, enabled=True, instructions=None)
        assert config.enabled and not config.available

    def test_text_without_enabled_is_not_available(self):
        config = AgentConfig(agent_key=AGENT, enabled=False, instructions="text")
        assert not config.available


class TestSnapshotRoundTrip:
    def test_it_preserves_what_drives_an_agent(self):
        original = AgentConfig(
            agent_key=AGENT,
            enabled=True,
            variation_key="v3",
            version=7,
            model="claude-sonnet-5",
            model_parameters={"temperature": 0.2},
            instructions="Check the manifest.",
            declared_tools=("manifest_read",),
        )
        restored = AgentConfig.from_snapshot(AGENT, original.to_snapshot())
        assert restored.instructions == original.instructions
        assert restored.instruction_hash == original.instruction_hash
        assert restored.variation_key == "v3"
        assert restored.version == 7
        assert restored.declared_tools == ("manifest_read",)

    def test_it_records_that_the_text_came_from_cache(self):
        config = AgentConfig(agent_key=AGENT, enabled=True, instructions="text")
        restored = AgentConfig.from_snapshot(AGENT, config.to_snapshot())
        assert (restored.source, restored.reason) == ("cache", "SNAPSHOT")

    def test_per_run_facts_stay_out_of_the_committed_file(self):
        # `source` and `reason` describe how *this run* got the config. In the
        # snapshot they would churn the file on every run and drown the signal.
        config = AgentConfig(
            agent_key=AGENT, instructions="text", source="launchdarkly", reason="RULE"
        )
        assert "source" not in config.to_snapshot()
        assert "reason" not in config.to_snapshot()

    def test_a_hand_edited_snapshot_is_rejected(self):
        # The file is committed, so a mismatch means someone edited the text
        # and left the hash. Loud is right: this is a repo problem.
        entry = AgentConfig(agent_key=AGENT, instructions="original").to_snapshot()
        entry["instructions"] = "quietly rewritten"
        with pytest.raises(AgentConfigError, match="edited by hand"):
            AgentConfig.from_snapshot(AGENT, entry)

    def test_a_snapshot_without_a_stored_hash_is_accepted(self):
        entry = AgentConfig(agent_key=AGENT, instructions="text").to_snapshot()
        del entry["instruction_hash"]
        assert AgentConfig.from_snapshot(AGENT, entry).instructions == "text"


class TestReadingLaunchDarkly:
    def test_it_parses_a_served_config(self, context):
        source = LaunchDarklyAgentConfigs(FakeClient({AGENT: variation()}))
        config = source.fetch(AGENT, context)
        assert config.enabled
        assert config.variation_key == "v3"
        assert config.version == 7
        assert config.model == "claude-sonnet-5"
        assert config.model_parameters == {"temperature": 0.2}
        assert config.instructions == "Check the manifest."
        assert config.source == "launchdarkly"

    def test_declared_tools_are_sorted(self):
        # A declaration to assert against Python's registry, and stable order
        # keeps the committed snapshot from churning on dict iteration order.
        source = LaunchDarklyAgentConfigs(FakeClient({AGENT: variation()}))
        config = source.fetch(
            AGENT,
            ContextBuilder(run_id="r1", profile="baseline").for_stage(
                "manifest_verification"
            ),
        )
        assert config.declared_tools == ("cost_table_read", "manifest_read")

    def test_a_missing_flag_is_unavailable_not_an_error(self, context):
        # Section 6.10 makes a missing config a normal path that falls back.
        config = LaunchDarklyAgentConfigs(FakeClient()).fetch(AGENT, context)
        assert not config.available
        assert config.source == "unavailable"

    def test_a_non_config_value_is_unavailable(self, context):
        source = LaunchDarklyAgentConfigs(FakeClient({AGENT: "not a config"}))
        assert not source.fetch(AGENT, context).available

    def test_a_served_but_disabled_config_is_still_from_launchdarkly(self, context):
        # Distinct from "no answer": LD really did answer, and the answer was
        # off. Chaining depends on being able to tell those apart.
        source = LaunchDarklyAgentConfigs(FakeClient({AGENT: variation(enabled=False)}))
        config = source.fetch(AGENT, context)
        assert config.source == "launchdarkly"
        assert not config.available

    def test_a_rule_match_reason_carries_the_rule_id(self, context):
        client = FakeClient(
            {AGENT: variation()}, reason={"kind": "RULE_MATCH", "ruleId": "r-77"}
        )
        config = LaunchDarklyAgentConfigs(client).fetch(AGENT, context)
        assert config.reason == "RULE_MATCH:r-77"

    def test_an_error_reason_carries_the_error_kind(self, context):
        client = FakeClient(
            {AGENT: variation()},
            reason={"kind": "ERROR", "errorKind": "FLAG_NOT_FOUND"},
        )
        config = LaunchDarklyAgentConfigs(client).fetch(AGENT, context)
        assert config.reason == "ERROR:FLAG_NOT_FOUND"


class TestSnapshotSource:
    def _write(self, path, agents, schema_version=SNAPSHOT_SCHEMA_VERSION):
        path.write_text(
            json.dumps({"schema_version": schema_version, "agents": agents}),
            encoding="utf-8",
        )

    def test_it_reads_a_cached_config(self, tmp_path, context):
        path = tmp_path / "snap.json"
        entry = AgentConfig(
            agent_key=AGENT, enabled=True, instructions="cached text"
        ).to_snapshot()
        self._write(path, {AGENT: entry})
        config = SnapshotAgentConfigs(path).fetch(AGENT, context)
        assert config.available
        assert config.instructions == "cached text"

    def test_a_missing_file_degrades_rather_than_raising(self, tmp_path, context):
        config = SnapshotAgentConfigs(tmp_path / "absent.json").fetch(AGENT, context)
        assert config.reason == "NO_SNAPSHOT"
        assert not config.available

    def test_unreadable_json_degrades(self, tmp_path, context):
        path = tmp_path / "snap.json"
        path.write_text("{ not json", encoding="utf-8")
        assert SnapshotAgentConfigs(path).fetch(AGENT, context).reason == (
            "SNAPSHOT_UNREADABLE"
        )

    def test_a_schema_bump_degrades_rather_than_misreading(self, tmp_path, context):
        path = tmp_path / "snap.json"
        self._write(path, {AGENT: {}}, schema_version=SNAPSHOT_SCHEMA_VERSION + 1)
        assert SnapshotAgentConfigs(path).fetch(AGENT, context).reason == (
            "SNAPSHOT_SCHEMA_MISMATCH"
        )

    def test_an_agent_absent_from_a_good_snapshot_says_so(self, tmp_path, context):
        path = tmp_path / "snap.json"
        self._write(path, {"review-narrator": {}})
        assert SnapshotAgentConfigs(path).fetch(AGENT, context).reason == (
            "NOT_IN_SNAPSHOT"
        )

    def test_the_file_is_read_once_not_per_agent(self, tmp_path, context):
        # A run pulls four agents; re-reading would let the file change
        # underneath a single run.
        path = tmp_path / "snap.json"
        entry = AgentConfig(
            agent_key=AGENT, enabled=True, instructions="first"
        ).to_snapshot()
        self._write(path, {AGENT: entry})
        source = SnapshotAgentConfigs(path)
        path.unlink()
        assert source.fetch(AGENT, context).instructions == "first"


class TestChaining:
    def test_launchdarkly_wins_when_it_answers(self, tmp_path, context):
        path = tmp_path / "snap.json"
        path.write_text(
            json.dumps({
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "agents": {AGENT: AgentConfig(
                    agent_key=AGENT, enabled=True, instructions="stale"
                ).to_snapshot()},
            }),
            encoding="utf-8",
        )
        chained = ChainedAgentConfigs(
            LaunchDarklyAgentConfigs(FakeClient({AGENT: variation("fresh")})),
            SnapshotAgentConfigs(path),
        )
        assert chained.fetch(AGENT, context).instructions == "fresh"

    def test_it_falls_back_to_the_snapshot_when_ld_is_silent(self, tmp_path, context):
        path = tmp_path / "snap.json"
        path.write_text(
            json.dumps({
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "agents": {AGENT: AgentConfig(
                    agent_key=AGENT, enabled=True, instructions="cached"
                ).to_snapshot()},
            }),
            encoding="utf-8",
        )
        chained = ChainedAgentConfigs(
            LaunchDarklyAgentConfigs(FakeClient()), SnapshotAgentConfigs(path)
        )
        assert chained.fetch(AGENT, context).instructions == "cached"

    def test_a_deliberate_off_is_not_overridden_by_stale_cache(self, tmp_path, context):
        # Turning an agent off in the console must actually turn it off, not
        # silently fall through to instructions cached from when it was on.
        path = tmp_path / "snap.json"
        path.write_text(
            json.dumps({
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "agents": {AGENT: AgentConfig(
                    agent_key=AGENT, enabled=True, instructions="cached"
                ).to_snapshot()},
            }),
            encoding="utf-8",
        )
        chained = ChainedAgentConfigs(
            LaunchDarklyAgentConfigs(
                FakeClient({AGENT: variation(enabled=False)})
            ),
            SnapshotAgentConfigs(path),
        )
        config = chained.fetch(AGENT, context)
        assert config.source == "launchdarkly"
        assert not config.available

    def test_it_needs_at_least_one_source(self):
        with pytest.raises(ValueError):
            ChainedAgentConfigs()

    def test_the_last_answer_is_returned_when_nothing_is_available(self, context):
        chained = ChainedAgentConfigs(
            OfflineAgentConfigs("NO_SDK_KEY"), OfflineAgentConfigs("NO_SNAPSHOT")
        )
        assert chained.fetch(AGENT, context).reason == "NO_SNAPSHOT"


class TestFetchingEveryAgent:
    def test_each_agent_is_evaluated_under_its_own_stage(self):
        # Evaluating all four under one context would make the per-agent
        # targeting of 6.6 silently ineffective.
        client = FakeClient({key: variation() for key in LD_CONFIGURED_KEYS})
        configs = fetch_agent_configs(
            LaunchDarklyAgentConfigs(client),
            ContextBuilder(run_id="r1", profile="baseline").for_stage,
        )
        assert set(configs) == set(LD_CONFIGURED_KEYS)
        # The client is handed an SDK Context, so read the stage back the way
        # LaunchDarkly's targeting would.
        stages = {
            key: ctx.get_individual_context("stage").key for key, ctx in client.seen
        }
        assert stages["manifest-verification"] == "manifest_verification"
        assert stages["address-repair"] == "address_repair"
        assert len(set(stages.values())) == len(LD_CONFIGURED_KEYS)


class TestFetchingB1PerImage:
    """Design 6.2: B1 is the only stage with a ground-truth answer key, so it
    is the only one whose config is retrieved per unit rather than per run."""

    def images(self, count=3):
        return tuple(ImageIdentity(key=f"img{i}", name=f"{i:02d}.png") for i in range(count))

    def test_one_fetch_per_image(self):
        client = FakeClient({EXTRACTION_KEY: variation()})
        configs = fetch_extraction_configs(
            LaunchDarklyAgentConfigs(client),
            ContextBuilder(run_id="r1", profile="baseline"),
            self.images(),
        )
        assert set(configs) == {"img0", "img1", "img2"}
        assert len(client.seen) == 3

    def test_each_fetch_carries_its_own_image_key(self):
        # Retrieving once and reusing the answer would leave the context
        # correct and the behaviour uniform, which is a rollout that does not
        # roll out.
        client = FakeClient({EXTRACTION_KEY: variation()})
        fetch_extraction_configs(
            LaunchDarklyAgentConfigs(client),
            ContextBuilder(run_id="r1", profile="baseline"),
            self.images(),
        )
        keys = {ctx.get_individual_context("image").key for _, ctx in client.seen}
        assert keys == {"img0", "img1", "img2"}

    def test_no_images_means_no_fetches(self):
        client = FakeClient({EXTRACTION_KEY: variation()})
        configs = fetch_extraction_configs(
            LaunchDarklyAgentConfigs(client),
            ContextBuilder(run_id="r1", profile="baseline"),
            (),
        )
        assert configs == {}
        assert client.seen == []


class TestArchivingASecondVariation:
    """Design 6.4 mitigation 2: an instruction hash in the ledger has to join
    to text committed in the repo. A rollout serving two variations of B1 in
    one run would otherwise leave the losing one's text nowhere."""

    def config(self, variation_key, instructions="text"):
        return AgentConfig(
            agent_key=EXTRACTION_KEY,
            enabled=True,
            variation_key=variation_key,
            instructions=instructions,
        )

    def test_a_single_variation_archives_nothing(self):
        canonical = {EXTRACTION_KEY: self.config("sonnet-baseline")}
        per_image = {"img0": self.config("sonnet-baseline")}
        assert archived_variations(canonical, per_image) == {}

    def test_a_second_variation_is_archived_under_its_own_key(self):
        canonical = {EXTRACTION_KEY: self.config("sonnet-baseline")}
        per_image = {
            "img0": self.config("sonnet-baseline"),
            "img1": self.config("haiku-trial", "different text"),
        }
        archived = archived_variations(canonical, per_image)
        assert set(archived) == {f"{EXTRACTION_KEY}#haiku-trial"}
        assert archived[f"{EXTRACTION_KEY}#haiku-trial"].instructions == "different text"

    def test_an_unavailable_config_is_not_archived(self):
        canonical = {EXTRACTION_KEY: self.config("sonnet-baseline")}
        per_image = {"img0": AgentConfig(agent_key=EXTRACTION_KEY, enabled=False)}
        assert archived_variations(canonical, per_image) == {}

    def test_the_archived_key_is_not_reachable_by_the_offline_reader(self, tmp_path):
        # The archive is an audit trail, never a cache. `SnapshotAgentConfigs`
        # looks up bare agent keys, so a `#`-suffixed entry can never be
        # served back as if LD had chosen it.
        path = tmp_path / "snap.json"
        write_snapshot(
            {
                EXTRACTION_KEY: self.config("sonnet-baseline", "canonical"),
                f"{EXTRACTION_KEY}#haiku-trial": self.config("haiku-trial", "other"),
            },
            path,
        )
        served = SnapshotAgentConfigs(path).fetch(EXTRACTION_KEY, {})
        assert served.instructions == "canonical"
        assert "haiku-trial" in json.loads(path.read_text())["agents"][
            f"{EXTRACTION_KEY}#haiku-trial"
        ]["variation_key"]


class TestWritingTheSnapshot:
    def _config(self, instructions="text", key=AGENT):
        return AgentConfig(agent_key=key, enabled=True, instructions=instructions)

    def test_it_writes_usable_configs(self, tmp_path):
        path = tmp_path / "snap.json"
        assert write_snapshot({AGENT: self._config()}, path) is True
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["agents"][AGENT]["instructions"] == "text"

    def test_unchanged_content_is_not_rewritten(self, tmp_path):
        # A run that found nothing new leaves a clean working tree.
        path = tmp_path / "snap.json"
        write_snapshot({AGENT: self._config()}, path)
        assert write_snapshot({AGENT: self._config()}, path) is False

    def test_an_edit_is_written(self, tmp_path):
        path = tmp_path / "snap.json"
        write_snapshot({AGENT: self._config("before")}, path)
        assert write_snapshot({AGENT: self._config("after")}, path) is True

    def test_an_unreachable_run_does_not_destroy_the_cache(self, tmp_path):
        # Overwriting good instructions with nothing, on the one run where LD
        # was unreachable, would break the fallback exactly when it is needed.
        path = tmp_path / "snap.json"
        write_snapshot({AGENT: self._config("good")}, path)
        unavailable = AgentConfig(agent_key=AGENT, source="unavailable")
        assert write_snapshot({AGENT: unavailable}, path) is False
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["agents"][AGENT]["instructions"] == "good"

    def test_other_agents_survive_a_partial_write(self, tmp_path):
        path = tmp_path / "snap.json"
        write_snapshot({"review-narrator": self._config(key="review-narrator")}, path)
        write_snapshot({AGENT: self._config()}, path)
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert set(stored["agents"]) == {AGENT, "review-narrator"}

    def test_the_document_carries_no_run_id_or_timestamp(self):
        # The file must change if and only if the LD configs changed, or
        # `git log` on it stops being a history of instruction edits.
        document = snapshot_document({AGENT: self._config()})
        assert set(document) == {"schema_version", "agents"}
        assert set(document["agents"][AGENT]) == {
            "enabled", "variation_key", "version", "model",
            "model_parameters", "instructions", "instruction_hash",
            "declared_tools",
        }


class TestTheRegistryMeansLdConfigured:
    """`LD_CONFIGURED_STAGES` is not a list of agents. Design 6.1 vs 6.3."""

    def test_b1_is_in_it(self):
        from bbq_shipment_agent.agent_configs import LD_CONFIGURED_STAGES

        assert "screenshot-extraction" in LD_CONFIGURED_STAGES

    def test_b1_is_offered_no_tools_and_never_will_be(self):
        # Design 6.3: a single-shot vision call with no loop and no tool
        # access. Being LD-configured buys it a model and a prompt, not
        # agency.
        from bbq_shipment_agent.agents.tools import TOOL_NAMES

        assert TOOL_NAMES["screenshot-extraction"] == frozenset()

    def test_every_key_has_its_own_stage(self):
        # The stage kind is what makes per-stage targeting work at all; two
        # entries sharing one would silently merge their targeting rules.
        from bbq_shipment_agent.agent_configs import LD_CONFIGURED_STAGES

        stages = list(LD_CONFIGURED_STAGES.values())
        assert len(set(stages)) == len(stages)

    def test_the_tool_contract_covers_every_key(self):
        from bbq_shipment_agent.agent_configs import LD_CONFIGURED_KEYS
        from bbq_shipment_agent.agents.tools import TOOL_NAMES

        assert set(TOOL_NAMES) == set(LD_CONFIGURED_KEYS)
