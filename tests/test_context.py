import pytest

from bbq_shipment_agent.context import (
    STAGE_ADDRESS_REPAIR,
    STAGE_MANIFEST_VERIFICATION,
    ContextError,
    build_context,
    to_ld_context,
)


def test_shape_matches_the_design_document():
    # docs/design.md section 6.6 shows this literal structure.
    context = build_context(
        run_id="run-1",
        stage=STAGE_ADDRESS_REPAIR,
        profile="planner_trial",
        campaign="aug-cook",
        packet_count=22,
        recipient_key="k1",
        shipment_attributes={"variant": "large", "zone": 6, "prior_failures": 3},
    )
    assert context == {
        "kind": "multi",
        "run": {
            "key": "run-1",
            "profile": "planner_trial",
            "campaign": "aug-cook",
            "packet_count": 22,
        },
        "stage": {"key": "address_repair"},
        "shipment": {
            "key": "k1",
            "variant": "large",
            "zone": 6,
            "prior_failures": 3,
        },
    }


def test_the_sdk_accepts_it():
    # An invalid context evaluates to the fallback for every flag without
    # raising, so a run would silently ignore all targeting.
    context = to_ld_context(
        build_context(
            run_id="run-1",
            stage=STAGE_ADDRESS_REPAIR,
            profile="baseline",
            recipient_key="k1",
        )
    )
    assert context.valid
    assert {
        context.get_individual_context(i).kind
        for i in range(context.individual_context_count)
    } == {"run", "stage", "shipment"}


def test_shipment_attributes_survive_the_sdk_round_trip():
    context = to_ld_context(
        build_context(
            run_id="run-1",
            stage=STAGE_ADDRESS_REPAIR,
            profile="baseline",
            recipient_key="k1",
            shipment_attributes={"prior_failures": 3},
        )
    )
    # Targeting a record that has failed before is the point of this kind.
    assert context.get_individual_context("shipment").get("prior_failures") == 3


def test_run_level_stages_carry_no_shipment():
    # manifest-verification has no shipment; a placeholder key would create a
    # targetable context corresponding to nothing real.
    context = build_context(
        run_id="run-1", stage=STAGE_MANIFEST_VERIFICATION, profile="baseline"
    )
    assert "shipment" not in context
    assert to_ld_context(context).valid


def test_optional_run_attributes_are_omitted_when_absent():
    context = build_context(run_id="run-1", stage="run_init", profile="baseline")
    assert context["run"] == {"key": "run-1", "profile": "baseline"}


def test_shipment_attributes_without_a_recipient_are_refused():
    with pytest.raises(ContextError, match="without a recipient_key"):
        build_context(
            run_id="run-1",
            stage=STAGE_ADDRESS_REPAIR,
            profile="baseline",
            shipment_attributes={"zone": 6},
        )


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"run_id": "", "stage": "s", "profile": "p"}, "run_id is required"),
        ({"run_id": "r", "stage": "", "profile": "p"}, "stage is required"),
    ],
)
def test_missing_context_keys_are_refused(kwargs, match):
    with pytest.raises(ContextError, match=match):
        build_context(**kwargs)


def test_an_invalid_context_is_caught_not_passed_along():
    with pytest.raises(ContextError, match="invalid evaluation context"):
        to_ld_context({"kind": "multi"})  # no individual contexts
