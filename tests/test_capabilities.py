import textwrap

import pytest
from enum import StrEnum

from bbq_shipment_agent.capabilities import (
    DEFAULT_CONFIG_PATH,
    CAPABILITY_TYPES,
    CapabilityConfig,
    CapabilityConfigError,
    CapabilitySet,
    PlannerMode,
    ValidationMode,
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
        validation: "standard"
        verification: "off"
      full:
        planner: "on"
        validation: "strict"
        verification: "on"
    default_profile: "baseline"
    kill_switch: false
"""


class TestShippedConfig:
    """The committed config is a hard-rule surface, not just a fixture."""

    def test_it_loads(self):
        assert CapabilityConfig.load(DEFAULT_CONFIG_PATH).profiles

    def test_every_capability_is_one_a_stage_reads(self):
        # `authority` and `memory` were both resolved, clamped, fingerprinted
        # and recorded while no stage consulted either, which is the
        # decorative-permission failure design 6.5 warns about and 6.6 removed
        # the `shipment` context kind for. This pins the set so a capability
        # cannot come back without a consumer arriving with it:
        #   planner     -> plan.py, gating B3 and whether repairs are applied
        #   validation  -> plan.py, gating B2
        #   verification-> agents/verification.py, gating D1
        assert set(CAPABILITY_TYPES) == {"planner", "validation", "verification"}

    def test_kill_switch_is_off(self):
        assert CapabilityConfig.load(DEFAULT_CONFIG_PATH).kill_switch is False

    def test_default_profile_is_the_safe_one(self):
        config = CapabilityConfig.load(DEFAULT_CONFIG_PATH)
        baseline = config.profiles[config.default_profile]
        assert baseline.planner is PlannerMode.OFF
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
                validation: standard
                verification: on
            default_profile: baseline
            kill_switch: false
            """,
        )
        baseline = CapabilityConfig.load(path).profiles["baseline"]
        assert baseline.planner is PlannerMode.OFF
        assert baseline.verification is VerificationMode.ON

    def test_planner_is_not_a_boolean(self):
        assert {m.value for m in PlannerMode} == {"off", "shadow", "on"}


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
        # Falls back to the profile, never to "leave whatever was there".
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
        resolved = resolve(config, overrides={"validation": 7})
        assert resolved.capabilities.validation is ValidationMode.STANDARD
        assert resolved.reasons["validation"].startswith("FLAG_VALUE_INVALID")

    def test_one_bad_value_does_not_discard_the_good_ones(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(
            config, overrides={"planner": "variation 2", "verification": "on"}
        )
        assert resolved.capabilities.verification is VerificationMode.ON
        assert resolved.reasons["verification"] == "FLAG_OVERRIDE"

    def test_an_unknown_capability_is_recorded_not_raised(self, tmp_path):
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(config, overrides={"telepathy": "on"})
        assert resolved.reasons["telepathy"] == "UNKNOWN_CAPABILITY_IGNORED"

    def test_a_removed_capability_is_treated_as_unknown(self, tmp_path):
        # `authority-level` and `memory-mode` no longer map to a field. A
        # console still serving them must not fail a run, and must leave a
        # trace saying the repo ignored it.
        config = CapabilityConfig.load(write_config(tmp_path, BASE))
        resolved = resolve(
            config, overrides={"authority": "purchase_labels", "memory": "read_write"}
        )
        assert resolved.reasons["authority"] == "UNKNOWN_CAPABILITY_IGNORED"
        assert resolved.reasons["memory"] == "UNKNOWN_CAPABILITY_IGNORED"
        assert not hasattr(resolved.capabilities, "authority")

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

    def test_a_prerequisite_on_a_removed_capability_is_rejected(self, tmp_path):
        # The `when` and `requires` keys are checked against CAPABILITY_TYPES,
        # so a rule left over from the authority ceiling fails loudly at
        # resolve time rather than silently never applying.
        stale = BASE + """
    prerequisites:
      - id: authority_needs_verification
        when: {authority: "purchase_labels"}
        requires: {verification: "on"}
        demote: {planner: "off"}
    """
        config = CapabilityConfig.load(write_config(tmp_path, stale))
        with pytest.raises(CapabilityConfigError, match="unknown `when` key"):
            resolve(config)

    def test_a_cycle_fails_loudly_rather_than_hanging(self, tmp_path):
        cyclic = BASE + """
    prerequisites:
      - id: a
        when: {planner: "off"}
        requires: {verification: "on"}
        demote: {planner: "shadow"}
      - id: b
        when: {planner: "shadow"}
        requires: {verification: "on"}
        demote: {planner: "off"}
    """
        config = CapabilityConfig.load(write_config(tmp_path, cyclic))
        with pytest.raises(CapabilityConfigError, match="converge"):
            resolve(config)


class TestFingerprint:
    def test_same_capabilities_same_fingerprint(self):
        a = CapabilitySet.from_mapping(
            {"planner": "off", "validation": "standard", "verification": "off"},
            source="a",
        )
        b = CapabilitySet.from_mapping(
            {"verification": "off", "validation": "standard", "planner": "off"},
            source="b",
        )
        assert a.fingerprint() == b.fingerprint()

    def test_any_difference_changes_it(self):
        base = {"planner": "off", "validation": "standard", "verification": "off"}
        a = CapabilitySet.from_mapping(base, source="a")
        b = CapabilitySet.from_mapping({**base, "validation": "strict"}, source="b")
        assert a.fingerprint() != b.fingerprint()


class TestConfigValidation:
    def test_missing_file_is_fatal(self, tmp_path):
        with pytest.raises(CapabilityConfigError, match="not found"):
            CapabilityConfig.load(tmp_path / "nope.yaml")

    def test_unknown_capability_is_rejected(self, tmp_path):
        path = write_config(tmp_path, BASE.replace(
            '        verification: "off"\n      full:',
            '        verification: "off"\n        telepathy: "on"\n      full:', 1))
        with pytest.raises(CapabilityConfigError, match="unknown capability"):
            CapabilityConfig.load(path)

    def test_a_profile_still_carrying_authority_is_rejected(self, tmp_path):
        # The removal has to be visible in the config too. A profile left
        # holding `authority: propose_only` would otherwise read as though the
        # ceiling were still enforced.
        path = write_config(tmp_path, BASE.replace(
            '        verification: "off"\n      full:',
            '        verification: "off"\n        authority: "propose_only"\n      full:',
            1))
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
    assert snapshot["capabilities"] == {
        "planner": "off",
        "validation": "standard",
        "verification": "off",
    }
