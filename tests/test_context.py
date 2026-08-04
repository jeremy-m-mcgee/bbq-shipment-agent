import json
from pathlib import Path

import pytest

from bbq_shipment_agent.context import (
    STAGE_ADDRESS_REPAIR,
    STAGE_EXTRACTION,
    STAGE_MANIFEST_VERIFICATION,
    ContextBuilder,
    ContextError,
    ImageIdentity,
    _individual,
    to_ld_context,
)


def test_shape_matches_the_design_document():
    # docs/design.md section 6.6 shows this literal structure.
    context = ContextBuilder(
        run_id="run-1",
        profile="planner_trial",
        campaign="aug-cook",
        packet_count=22,
    ).for_stage(STAGE_ADDRESS_REPAIR)
    assert context == {
        "kind": "multi",
        "run": {
            "key": "run-1",
            "profile": "planner_trial",
            "campaign": "aug-cook",
            "packet_count": 22,
        },
        "stage": {"key": "address_repair"},
    }


def test_the_sdk_accepts_it():
    # An invalid context evaluates to the fallback for every flag without
    # raising, so a run would silently ignore all targeting.
    context = to_ld_context(
        ContextBuilder(run_id="run-1", profile="baseline").for_stage(
            STAGE_ADDRESS_REPAIR
        )
    )
    assert context.valid
    assert {
        context.get_individual_context(i).kind
        for i in range(context.individual_context_count)
    } == {"run", "stage"}


def test_optional_run_attributes_are_omitted_when_absent():
    context = ContextBuilder(run_id="run-1", profile="baseline").for_stage("run_init")
    assert context["run"] == {"key": "run-1", "profile": "baseline"}


def test_one_builder_serves_every_stage_with_the_same_run_identity():
    # The property the builder exists for: a percentage rollout is only
    # coherent, and an experiment only attributable, if two evaluations in one
    # run present identical attributes.
    builder = ContextBuilder(run_id="run-1", profile="full", campaign="aug-cook")
    first = builder.for_stage(STAGE_ADDRESS_REPAIR)
    second = builder.for_stage(STAGE_MANIFEST_VERIFICATION)
    assert first["run"] == second["run"]
    assert first["stage"] != second["stage"]


def test_a_run_carries_the_builder_it_was_evaluated_under(tmp_path):
    # Not a fresh one built from its own fields: A1 resolves capabilities
    # against this object before `Run` exists.
    from bbq_shipment_agent.run import initialize_run

    run = initialize_run(ledger_root=tmp_path, campaign="aug-cook", packet_count=22)
    assert run.context_for_stage(STAGE_ADDRESS_REPAIR) == run.contexts.for_stage(
        STAGE_ADDRESS_REPAIR
    )
    assert run.contexts.profile == run.resolved.profile


class TestTheImageKind:
    """The only unit that is stable across runs and appears many times within
    one, which is what a percentage rollout or an experiment needs. The run
    key is a fresh UUID, so bucketing on it re-rolls every run."""

    def shot(self, tmp_path, name, content=b"\x89PNG-one"):
        path = tmp_path / name
        path.write_bytes(content)
        return path

    def test_it_adds_an_image_to_the_stage_context(self, tmp_path):
        image = ImageIdentity.of(self.shot(tmp_path, "01-imessage.png"))
        builder = ContextBuilder(run_id="run-1", profile="baseline")
        context = builder.for_image(STAGE_EXTRACTION, image)
        assert context["image"] == {"key": image.key}
        assert context["run"] == builder.for_stage(STAGE_EXTRACTION)["run"]
        assert context["stage"] == {"key": "extraction"}

    def test_the_sdk_accepts_it(self, tmp_path):
        image = ImageIdentity.of(self.shot(tmp_path, "01-imessage.png"))
        context = to_ld_context(
            ContextBuilder(run_id="run-1", profile="baseline").for_image(
                STAGE_EXTRACTION, image
            )
        )
        assert context.valid
        assert context.get_individual_context("image").key == image.key

    def test_the_filename_never_reaches_launchdarkly(self, tmp_path):
        # These are screenshots of private message threads. The key is a
        # content hash and the kind carries no attributes, so there is nothing
        # in the context to leak -- the name lives on the run row instead.
        image = ImageIdentity.of(self.shot(tmp_path, "mum-and-dad-thread.png"))
        context = ContextBuilder(run_id="run-1", profile="baseline").for_image(
            STAGE_EXTRACTION, image
        )
        assert "mum-and-dad-thread" not in json.dumps(context)
        assert set(context["image"]) == {"key"}

    def test_the_same_bytes_are_the_same_unit_under_any_name(self, tmp_path):
        # A renamed or re-sorted directory is not a new population to bucket.
        first = ImageIdentity.of(self.shot(tmp_path, "01-shot.png"))
        second = ImageIdentity.of(self.shot(tmp_path, "07-renamed.png"))
        assert first.key == second.key
        assert first.name != second.name

    def test_different_bytes_are_different_units(self, tmp_path):
        first = ImageIdentity.of(self.shot(tmp_path, "a.png", b"\x89PNG-one"))
        second = ImageIdentity.of(self.shot(tmp_path, "b.png", b"\x89PNG-two"))
        assert first.key != second.key

    def test_the_key_is_stable_across_processes(self, tmp_path):
        # A bucketing key that changed between runs would silently re-roll
        # every rollout, which is the failure the run UUID already has.
        path = self.shot(tmp_path, "01-shot.png")
        assert ImageIdentity.of(path).key == ImageIdentity.of(path).key


def test_an_undeclared_attribute_is_refused():
    # The failure this guards is an edit that adds a field to ContextBuilder
    # and forgets to declare it, which would otherwise send it to LD.
    with pytest.raises(ContextError, match="may not carry"):
        _individual("run", "run-1", {"profile": "baseline", "operator_email": "x@y.z"})


def test_an_undeclared_kind_is_refused():
    with pytest.raises(ContextError, match="not a declared context kind"):
        _individual("shipment", "k1", {})


def test_declared_attributes_are_the_ones_the_builder_produces():
    # Keeps _ATTRIBUTES honest in the other direction: a declared attribute
    # nothing sends is a targeting rule that would never fire.
    from bbq_shipment_agent.context import _ATTRIBUTES

    produced = ContextBuilder(
        run_id="r", profile="p", campaign="c", packet_count=1
    ).for_stage("run_init")
    assert set(produced["run"]) - {"key"} == _ATTRIBUTES["run"]
    assert set(produced["stage"]) - {"key"} == _ATTRIBUTES["stage"]


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"run_id": "", "profile": "p"}, "run_id is required"),
        ({"run_id": "r", "profile": ""}, "profile is required"),
    ],
)
def test_missing_run_identity_is_refused(kwargs, match):
    with pytest.raises(ContextError, match=match):
        ContextBuilder(**kwargs)


def test_a_missing_stage_is_refused():
    with pytest.raises(ContextError, match="stage is required"):
        ContextBuilder(run_id="r", profile="p").for_stage("")


def test_an_invalid_context_is_caught_not_passed_along():
    with pytest.raises(ContextError, match="invalid evaluation context"):
        to_ld_context({"kind": "multi"})  # no individual contexts


def test_nothing_outside_context_py_builds_a_context():
    # The same shape as "no test can open a socket": the property is only
    # worth anything if it is enforced rather than intended. A second
    # construction site is how the run context and the stage context drift
    # apart, which is precisely what a rollout cannot survive.
    package = Path(__file__).resolve().parent.parent / "src" / "bbq_shipment_agent"
    offenders = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if path.name != "context.py" and "Context.from_dict" in path.read_text()
    ]
    assert offenders == []
