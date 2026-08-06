from enum import StrEnum

import pytest

from bbq_shipment_agent.capabilities import (
    CAPABILITIES,
    CAPABILITY_FLAGS,
    CAPABILITY_TYPES,
    DEFAULT_PROFILE,
    KILL_SWITCH_DEFAULT,
    KILL_SWITCH_FLAG,
    CapabilityConfigError,
    CapabilitySet,
    FlagEvaluation,
    LaunchDarklyGate,
    OfflineGate,
    PlannerMode,
    ValidationMode,
    VerificationMode,
    evaluate_capability,
    evaluate_kill_switch,
)
from bbq_shipment_agent.context import (
    STAGE_ADDRESS_REPAIR,
    STAGE_ADDRESS_VALIDATION,
    STAGE_MANIFEST_VERIFICATION,
    ContextBuilder,
)


class StubGate:
    """A gate that serves a fixed value, for exercising coercion in isolation.

    Records the `(flag_key, default)` it was asked with, so a test can assert
    the code default was handed down as the SDK fallback.
    """

    def __init__(self, value, *, source="launchdarkly", reason="RULE_MATCH:r1"):
        self._evaluation = FlagEvaluation(value=value, source=source, reason=reason)
        self.calls: list[tuple[str, object]] = []

    def evaluate(self, flag_key, context, default):
        self.calls.append((flag_key, default))
        return self._evaluation


class FakeDetail:
    def __init__(self, value, reason):
        self.value = value
        self.reason = reason


class FakeClient:
    """Stands in for an LDClient. Returns one canned detail for every flag."""

    def __init__(self, value, reason):
        self._value = value
        self._reason = reason
        self.seen: list[tuple[str, object]] = []

    def variation_detail(self, flag, context, default):
        self.seen.append((flag, default))
        return FakeDetail(self._value, self._reason)


def a_context(stage=STAGE_ADDRESS_REPAIR):
    return ContextBuilder(run_id="run-x", profile="baseline").for_stage(stage)


class TestCapabilityRegistry:
    """Every capability is one a stage reads, and it says which stage."""

    def test_the_set_is_the_three_a_stage_reads(self):
        # `authority` and `memory` were both resolved and recorded while no
        # stage consulted either, which is the decorative-permission failure
        # design 6.5 warns about. This pins the set so a capability cannot come
        # back without a consumer arriving with it.
        assert set(CAPABILITY_TYPES) == {"planner", "validation", "verification"}

    def test_flag_keys_map_back_onto_capability_names(self):
        assert CAPABILITY_FLAGS == {
            "planner-mode": "planner",
            "validation-mode": "validation",
            "verification-enabled": "verification",
        }

    def test_each_capability_is_evaluated_under_the_stage_that_reads_it(self):
        # The whole point of per-stage evaluation: a rule written against the
        # stage fires because the flag is evaluated under it.
        assert CAPABILITIES["planner"].stage == STAGE_ADDRESS_REPAIR
        assert CAPABILITIES["validation"].stage == STAGE_ADDRESS_VALIDATION
        assert CAPABILITIES["verification"].stage == STAGE_MANIFEST_VERIFICATION

    def test_the_defaults_are_the_safe_offline_set(self):
        # No profile file any more: the offline fallback is these defaults,
        # and they are `baseline` (planner off, verification off).
        assert CAPABILITIES["planner"].default is PlannerMode.OFF
        assert CAPABILITIES["validation"].default is ValidationMode.STANDARD
        assert CAPABILITIES["verification"].default is VerificationMode.OFF
        assert DEFAULT_PROFILE == "baseline"

    def test_each_capability_declares_a_flag_key_and_an_enum(self):
        for name, cap in CAPABILITIES.items():
            assert cap.name == name
            assert cap.flag_key
            assert isinstance(cap.default, cap.enum)


class TestEnumConstrained:
    """The secrets guard: a served value can only ever be an enum member.

    CLAUDE.md forbids raw LD payload in the committed, append-only ledger.
    Recording the coerced value is safe only because every value is
    enum-constrained -- structurally incapable of carrying a secret.
    """

    def test_every_capability_is_enum_constrained(self):
        for name, enum_type in CAPABILITY_TYPES.items():
            assert issubclass(enum_type, StrEnum), (
                f"{name} is not a StrEnum, so a served value for it could be "
                "arbitrary text. Constrain it or stop recording it verbatim."
            )

    def test_planner_is_not_a_boolean(self):
        assert {m.value for m in PlannerMode} == {"off", "shadow", "on"}


class TestLiveCoercion:
    """`evaluate_capability` coerces whatever LD serves through the enum."""

    def test_a_served_value_is_used(self):
        gate = StubGate("shadow")
        value, source, reason = evaluate_capability(
            CAPABILITIES["planner"], gate, a_context()
        )
        assert value is PlannerMode.SHADOW
        assert source == "launchdarkly"
        assert reason == "launchdarkly:RULE_MATCH:r1"

    def test_the_default_is_handed_down_as_the_fallback(self):
        gate = StubGate("shadow")
        evaluate_capability(CAPABILITIES["planner"], gate, a_context())
        assert gate.calls == [("planner-mode", "off")]

    def test_an_invalid_value_falls_back_to_the_default(self):
        # Found live: `planner-mode` served the placeholder "variation 2" and
        # aborted the run. It must degrade, not raise.
        gate = StubGate("variation 2")
        value, _, reason = evaluate_capability(
            CAPABILITIES["planner"], gate, a_context()
        )
        assert value is PlannerMode.OFF
        assert reason == "FLAG_VALUE_INVALID:'variation 2'"

    def test_a_secret_never_becomes_the_value(self):
        gate = StubGate("sk-live-secret")
        value, _, reason = evaluate_capability(
            CAPABILITIES["planner"], gate, a_context()
        )
        assert value is PlannerMode.OFF
        # The rejected value is named in the reason so the console gets fixed,
        # but it is never what the ledger records as the value.
        assert "sk-live-secret" in reason
        assert value.value != "sk-live-secret"

    def test_a_non_string_value_is_discarded(self):
        gate = StubGate(7)
        value, _, reason = evaluate_capability(
            CAPABILITIES["validation"], gate, a_context(STAGE_ADDRESS_VALIDATION)
        )
        assert value is ValidationMode.STANDARD
        assert reason.startswith("FLAG_VALUE_INVALID")

    def test_a_boolean_maps_onto_on_off(self):
        # LD can serve a two-state flag as a JSON boolean.
        gate = StubGate(True)
        value, _, _ = evaluate_capability(
            CAPABILITIES["verification"], gate, a_context(STAGE_MANIFEST_VERIFICATION)
        )
        assert value is VerificationMode.ON

    def test_the_offline_gate_serves_the_default(self):
        value, source, reason = evaluate_capability(
            CAPABILITIES["planner"], OfflineGate(reason="NO_SDK_KEY"), a_context()
        )
        assert value is PlannerMode.OFF
        assert source == "offline"
        assert reason == "offline:NO_SDK_KEY"


class TestKillSwitch:
    def test_it_defaults_to_off_when_unreachable(self):
        kill = evaluate_kill_switch(OfflineGate(), a_context())
        assert kill.value is False
        assert KILL_SWITCH_DEFAULT is False

    def test_a_true_variation_engages_it(self):
        kill = evaluate_kill_switch(StubGate(True), a_context())
        assert kill.value is True

    def test_it_reads_the_named_flag(self):
        gate = StubGate(False)
        evaluate_kill_switch(gate, a_context())
        assert gate.calls == [(KILL_SWITCH_FLAG, KILL_SWITCH_DEFAULT)]


class TestLaunchDarklyGate:
    """The live gate, against a fake client and a real context."""

    def test_it_returns_the_value_and_a_flattened_reason(self):
        client = FakeClient("strict", {"kind": "RULE_MATCH", "ruleId": "r7"})
        gate = LaunchDarklyGate(client)
        result = gate.evaluate("validation-mode", a_context(STAGE_ADDRESS_VALIDATION), "standard")
        assert result.value == "strict"
        assert result.source == "launchdarkly"
        assert result.reason == "RULE_MATCH:r7"

    def test_it_passes_the_default_through_to_the_sdk(self):
        client = FakeClient("off", {"kind": "FALLTHROUGH"})
        LaunchDarklyGate(client).evaluate("planner-mode", a_context(), "off")
        assert client.seen == [("planner-mode", "off")]


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


class TestCapabilitySetStrict:
    """`from_mapping` is the strict reader for a stored full set."""

    def test_a_missing_capability_is_fatal(self):
        with pytest.raises(CapabilityConfigError, match="missing capability"):
            CapabilitySet.from_mapping(
                {"planner": "off", "validation": "standard"}, source="s"
            )

    def test_an_unknown_capability_is_fatal(self):
        with pytest.raises(CapabilityConfigError, match="unknown capability"):
            CapabilitySet.from_mapping(
                {
                    "planner": "off",
                    "validation": "standard",
                    "verification": "off",
                    "authority": "propose_only",
                },
                source="s",
            )

    def test_an_invalid_value_is_fatal(self):
        with pytest.raises(CapabilityConfigError, match="not one of"):
            CapabilitySet.from_mapping(
                {"planner": "maybe", "validation": "standard", "verification": "off"},
                source="s",
            )

    def test_a_boolean_coerces(self):
        resolved = CapabilitySet.from_mapping(
            {"planner": "off", "validation": "standard", "verification": True},
            source="s",
        )
        assert resolved.verification is VerificationMode.ON

    def test_to_mapping_is_names_to_values(self):
        resolved = CapabilitySet(
            planner=PlannerMode.OFF,
            validation=ValidationMode.STANDARD,
            verification=VerificationMode.OFF,
        )
        assert resolved.to_mapping() == {
            "planner": "off",
            "validation": "standard",
            "verification": "off",
        }
