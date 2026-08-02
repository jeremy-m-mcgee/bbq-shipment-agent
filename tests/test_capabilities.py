import textwrap

import pytest
from enum import StrEnum

from bbq_shipment_agent.capabilities import (
    DEFAULT_CONFIG_PATH,
    AuthorityLevel,
    CAPABILITY_TYPES,
    CapabilityConfig,
    CapabilityConfigError,
    CapabilitySet,
    MemoryMode,
    PlannerMode,
    VerificationMode,
    resolve,
)


def write_config(tmp_path, body: str):
    path = tmp_path / "capabilities.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


BASE = """
    profiles:
      baseline:
        planner: "off"
        memory: "off"
        validation: "standard"
        verification: "off"
        authority: "propose_only"
      full:
        planner: "on"
        memory: "read_write"
        validation: "strict"
        verification: "on"
        authority: "purchase_labels"
    default_profile: "baseline"
    authority_ceiling: "propose_only"
    kill_switch: false
"""


class TestShippedConfig:
    """The committed config is a hard-rule surface, not just a fixture."""

    def test_it_loads(self):
        assert CapabilityConfig.load(DEFAULT_CONFIG_PATH).profiles

    def test_authority_ceiling_is_propose_only(self):
        # docs/design.md section 3: a hard constraint. If this fails, someone
        # has changed what the system is permitted to do in the world.
        config = CapabilityConfig.load(DEFAULT_CONFIG_PATH)
        assert config.authority_ceiling is AuthorityLevel.PROPOSE_ONLY

    def test_no_profile_exceeds_the_ceiling(self):
        config = CapabilityConfig.load(DEFAULT_CONFIG_PATH)
        for name, capabilities in config.profiles.items():
            assert not (config.authority_ceiling < capabilities.authority), name

    def test_kill_switch_is_off(self):
        assert CapabilityConfig.load(DEFAULT_CONFIG_PATH).kill_switch is False

    def test_default_profile_is_the_safe_one(self):
        config = CapabilityConfig.load(DEFAULT_CONFIG_PATH)
        baseline = config.profiles[config.default_profile]
        assert baseline.planner is PlannerMode.OFF
        assert baseline.memory is MemoryMode.OFF
        assert baseline.verification is VerificationMode.OFF


class TestYamlBooleanTrap:
    def test_unquoted_off_and_on_survive_as_modes(self, tmp_path):
        # YAML 1.1 parses bare off/on as booleans, which would make `planner`
        # a bool in two profiles and a string in the third.
        path = write_config(
            tmp_path,
            """
            profiles:
              baseline:
                planner: off
                memory: off
                validation: standard
                verification: on
                authority: "propose_only"
            default_profile: baseline
            authority_ceiling: "propose_only"
            kill_switch: false
            """,
        )
        baseline = CapabilityConfig.load(path).profiles["baseline"]
        assert baseline.planner is PlannerMode.OFF
        assert baseline.verification is VerificationMode.ON

    def test_planner_is_not_a_boolean(self):
        assert {m.value for m in PlannerMode} == {"off", "shadow", "on"}


class TestAuthorityClamp:
    def test_a_flag_cannot_raise_authority_past_the_ceiling(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(config, overrides={"authority": "purchase_labels"})
        assert resolved.capabilities.authority is AuthorityLevel.PROPOSE_ONLY
        assert resolved.reasons["authority"].startswith("CLAMPED_TO_CEILING")

    def test_a_profile_cannot_exceed_the_ceiling_either(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(config, profile="full", shadow_runs=99)
        assert resolved.capabilities.authority is AuthorityLevel.PROPOSE_ONLY

    def test_a_flag_may_still_lower_authority(self, tmp_path):
        # LD can lower authority, never raise it — lowering must keep working.
        config = CapabilityConfig.load(
            write_config(tmp_path, BASE.replace('ceiling: "propose_only"', 'ceiling: "purchase_labels"'))
        )
        resolved = resolve(config, profile="full", overrides={"authority": "propose_only"},
                           shadow_runs=99)
        assert resolved.capabilities.authority is AuthorityLevel.PROPOSE_ONLY
        assert resolved.reasons["authority"] == "FLAG_OVERRIDE"

    def test_authority_levels_order_by_rank_not_alphabetically(self):
        assert AuthorityLevel.PROPOSE_ONLY < AuthorityLevel.PURCHASE_LABELS
        assert not (AuthorityLevel.PURCHASE_LABELS < AuthorityLevel.PROPOSE_ONLY)


class TestFlagPayloadCannotCarryFreeText:
    """The guard behind recording `flag_payload` instead of hashing it.

    CLAUDE.md forbids raw LD payload in the committed, append-only ledger.
    Storing the *parsed* overrides is safe only because every capability value
    is enum-constrained -- structurally incapable of carrying a secret. That
    is a property of `CAPABILITY_TYPES`, so it is pinned here: adding a
    capability whose values are free text breaks this test rather than
    silently widening what reaches the ledger.
    """

    def test_every_capability_is_enum_constrained(self):
        for name, enum_type in CAPABILITY_TYPES.items():
            assert issubclass(enum_type, StrEnum), (
                f"{name} is not a StrEnum, so an override for it could be "
                "arbitrary text. Either constrain it or stop recording "
                "flag_payload verbatim."
            )

    def test_an_override_outside_the_enum_never_reaches_the_record(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(config, overrides={"planner": "sk-live-secret"})
        assert resolved.capabilities.planner is PlannerMode.OFF
        assert "sk-live-secret" in resolved.reasons["planner"]


class TestUnusableOverrides:
    """A proposal the repo cannot use is discarded, never fatal.

    Found live: `planner-mode` was serving the literal string "variation 2",
    LaunchDarkly's placeholder variation name, and it aborted the run.
    """

    def test_an_unparseable_value_leaves_the_profile_standing(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(config, overrides={"planner": "variation 2"})
        assert resolved.capabilities.planner is PlannerMode.OFF

    def test_the_rejected_value_is_named_in_the_reason(self, tmp_path):
        # So the console gets fixed rather than guessed at.
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(config, overrides={"planner": "variation 2"})
        assert resolved.reasons["planner"] == "FLAG_VALUE_INVALID:'variation 2'"

    def test_a_non_string_value_is_also_discarded(self, tmp_path):
        # LD will serve whatever type the variation holds, including a number.
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(config, overrides={"memory": 7})
        assert resolved.capabilities.memory is MemoryMode.OFF
        assert resolved.reasons["memory"].startswith("FLAG_VALUE_INVALID")

    def test_one_bad_value_does_not_discard_the_good_ones(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(
            config, overrides={"planner": "variation 2", "memory": "read"}
        )
        assert resolved.capabilities.memory is MemoryMode.READ
        assert resolved.reasons["memory"] == "FLAG_OVERRIDE"

    def test_an_unknown_capability_is_recorded_not_raised(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(config, overrides={"telepathy": "on"})
        assert resolved.reasons["telepathy"] == "UNKNOWN_CAPABILITY_IGNORED"

    def test_a_bad_value_cannot_smuggle_authority_past_the_ceiling(self, tmp_path):
        # Discarding must fall back to the profile, never to "leave it alone".
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(config, overrides={"authority": "root"})
        assert resolved.capabilities.authority is AuthorityLevel.PROPOSE_ONLY

    def test_the_committed_config_is_still_strict(self, tmp_path):
        # The degrade applies to values LD hands over at runtime. A bad value
        # in the repo is a repo problem and must still fail loudly.
        path = write_config(tmp_path, BASE.replace('planner: "off"', 'planner: "nope"'))
        with pytest.raises(CapabilityConfigError):
            CapabilityConfig.load(path)

    def test_an_unknown_profile_still_raises(self, tmp_path):
        # That comes from the caller, not from the network.
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        with pytest.raises(CapabilityConfigError):
            resolve(config, profile="does_not_exist")


class TestPrerequisites:
    CONFIG = BASE + """
    prerequisites:
      - id: authority_needs_verification
        when: {authority_above: "propose_only"}
        requires: {verification: "on"}
        demote: {authority: "propose_only"}
      - id: planner_on_needs_shadow_history
        when: {planner: "on"}
        requires: {shadow_runs_at_least: 5}
        demote: {planner: "shadow"}
    """

    def test_planner_on_is_demoted_without_shadow_history(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, self.CONFIG))
        resolved = resolve(config, profile="full", shadow_runs=0)
        assert resolved.capabilities.planner is PlannerMode.SHADOW
        assert resolved.reasons["planner"] == (
            "PREREQUISITE_UNMET:planner_on_needs_shadow_history"
        )

    def test_planner_on_survives_with_enough_shadow_history(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, self.CONFIG))
        resolved = resolve(config, profile="full", shadow_runs=5)
        assert resolved.capabilities.planner is PlannerMode.ON

    def test_authority_is_demoted_when_verification_is_off(self, tmp_path):
        raised = self.CONFIG.replace('ceiling: "propose_only"', 'ceiling: "purchase_labels"')
        config = CapabilityConfig.load(write_config(tmp_path, raised))
        resolved = resolve(
            config, profile="full", overrides={"verification": "off"}, shadow_runs=99
        )
        assert resolved.capabilities.authority is AuthorityLevel.PROPOSE_ONLY
        assert resolved.reasons["authority"] == (
            "PREREQUISITE_UNMET:authority_needs_verification"
        )

    def test_a_cycle_fails_loudly_rather_than_hanging(self, tmp_path):
        cyclic = BASE + """
    prerequisites:
      - id: a
        when: {planner: "off"}
        requires: {memory: "read"}
        demote: {planner: "shadow"}
      - id: b
        when: {planner: "shadow"}
        requires: {memory: "read"}
        demote: {planner: "off"}
    """
        config = CapabilityConfig.load(write_config(tmp_path, cyclic))
        with pytest.raises(CapabilityConfigError, match="converge"):
            resolve(config)


class TestFingerprint:
    def test_same_capabilities_same_fingerprint(self):
        a = CapabilitySet.from_mapping(
            {"planner": "off", "memory": "off", "validation": "standard", "verification": "off",
             "authority": "propose_only"}, source="a")
        b = CapabilitySet.from_mapping(
            {"authority": "propose_only", "verification": "off", "memory": "off",
             "validation": "standard", "planner": "off"}, source="b")
        assert a.fingerprint() == b.fingerprint()

    def test_any_difference_changes_it(self):
        base = {"planner": "off", "memory": "off", "validation": "standard", "verification": "off",
                "authority": "propose_only"}
        a = CapabilitySet.from_mapping(base, source="a")
        b = CapabilitySet.from_mapping({**base, "memory": "read"}, source="b")
        assert a.fingerprint() != b.fingerprint()


class TestConfigValidation:
    def test_missing_file_is_fatal(self, tmp_path):
        with pytest.raises(CapabilityConfigError, match="not found"):
            CapabilityConfig.load(tmp_path / "nope.yaml")

    def test_unknown_capability_is_rejected(self, tmp_path):
        path = write_config(tmp_path, BASE.replace(
            '        authority: "propose_only"\n      full:',
            '        authority: "propose_only"\n        telepathy: "on"\n      full:', 1))
        with pytest.raises(CapabilityConfigError, match="unknown capability"):
            CapabilityConfig.load(path)

    def test_invalid_mode_is_rejected(self, tmp_path):
        path = write_config(tmp_path, BASE.replace('planner: "off"', 'planner: "maybe"'))
        with pytest.raises(CapabilityConfigError, match="not one of"):
            CapabilityConfig.load(path)

    def test_default_profile_must_exist(self, tmp_path):
        path = write_config(tmp_path, BASE.replace('default_profile: "baseline"',
                                                   'default_profile: "ghost"'))
        with pytest.raises(CapabilityConfigError, match="not a defined profile"):
            CapabilityConfig.load(path)

    def test_unknown_profile_at_resolve_time_is_rejected(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        with pytest.raises(CapabilityConfigError, match="unknown profile"):
            resolve(config, profile="ghost")

    def test_non_boolean_kill_switch_is_rejected(self, tmp_path):
        path = write_config(tmp_path, BASE.replace("kill_switch: false",
                                                   'kill_switch: "maybe"'))
        with pytest.raises(CapabilityConfigError, match="true or false"):
            CapabilityConfig.load(path)


def test_snapshot_carries_values_and_reasons_only(tmp_path):
    # This lands in a committed, append-only file — no raw payload.
    config = CapabilityConfig.load(write_config(tmp_path, BASE))
    snapshot = resolve(config).to_snapshot()
    assert set(snapshot) == {"profile", "capabilities", "reasons"}
    assert snapshot["capabilities"]["authority"] == "propose_only"
