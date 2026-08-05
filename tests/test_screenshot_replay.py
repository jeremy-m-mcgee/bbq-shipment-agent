"""The screenshot path, replayed end to end with nothing live anywhere.

Every other spine test plans one recipient down one recorded lane, because for
a long time one lane was all there was: `shippo-quotes-sf-dc.json` held San
Francisco to Washington and the fixture screenshots hold twenty-odd other
destinations, so a replayed screenshot plan reached C2 and stopped. Design 10
recorded that as a limit and paired it with a second one, that B3's proposals
had no recording either, so the repair loop escalated offline where it repaired
live.

`tools/record_shippo.py` recorded both. This is what stops them silently
un-recording themselves: a fixture only makes a path offline if something
actually runs that path against it, and the failure mode of both recordings is
a raise from `RecordedQuoter` or `RecordedAddressValidator` that some caller
upstream is entitled to catch.

Nothing here is a shape assertion. The counts are the point -- design 10 says
so in as many words about the repair tests, which passed throughout the
regression they were meant to catch.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bbq_shipment_agent.capabilities import PlannerMode, ValidationMode
from bbq_shipment_agent.wiring import RunOptions, open_run, plan_with

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).parent.parent
SNAPSHOT = REPO / "config" / "ld-snapshot.json"

#: The `--offline` command in the README, as options. `--profile` is the only
#: thing the two classes below vary, because `validation-mode` is what decides
#: whether B2 hands an address to B3 or corrects it in place.
RECORDINGS = dict(
    recipients=FIXTURES / "roster-screenshots.yaml",
    screenshots=FIXTURES / "screenshots",
    extractions=FIXTURES / "b1-extractions.json",
    repairs=FIXTURES / "b3-repairs.json",
    quotes=FIXTURES / "shippo-quotes-screenshots.json",
    validations=FIXTURES / "shippo-addresses.json",
    completions=FIXTURES / "d1-completions.json",
)


@pytest.fixture
def options(tmp_path):
    # The committed snapshot is copied rather than pointed at: an offline run
    # reads it for instructions, and nothing a test does should be able to
    # write a file the repo tracks.
    (tmp_path / "snapshot.json").write_bytes(SNAPSHOT.read_bytes())
    return RunOptions(
        offline=True,
        ledger=tmp_path / "ledger",
        config=REPO / "config" / "capabilities.yaml",
        lanes=REPO / "config" / "lanes.yaml",
        snapshot=tmp_path / "snapshot.json",
        cache=tmp_path / "cache",
        **RECORDINGS,
    )


def plan(options, profile):
    chosen = replace(options, profile=profile)
    context = open_run(chosen)
    assert context.client is None, "an offline run built a live client"
    return plan_with(context, chosen).result


@pytest.fixture(scope="module")
def proposals():
    """What B3 proposed, out of the recorded conversation's last turn."""
    turns = json.loads((FIXTURES / "b3-repairs.json").read_text())["turns"]
    text = turns[-1]["text"]
    return json.loads(text[text.find("{") : text.rfind("}") + 1])["repairs"]


class TestTheRecordingsCoverTheRun:
    def test_every_recording_is_named_so_the_run_is_replaying(self, options):
        # `replaying` is what the UI's checkbox means. A screenshot run needs
        # all five, and this is the assertion that fails first if one is
        # dropped from the fixture set above.
        assert options.replaying

    def test_a_replayed_screenshot_run_reaches_a_manifest(self, options):
        result = plan(options, "planner_trial")
        assert result.manifest is not None, result.reason
        assert result.manifest.rows

    def test_the_manifest_covers_more_than_the_one_recorded_lane(self, options):
        # The limit this fixture removed, stated as a test: twenty-odd
        # destinations rather than San Francisco to Washington.
        result = plan(options, "planner_trial")
        assert len({row.address["zip"] for row in result.manifest.rows}) > 10

    def test_every_row_carries_a_recorded_price(self, options):
        result = plan(options, "planner_trial")
        assert all(row.cost > 0 for row in result.manifest.rows)


class TestTheRepairLoopBehavesAsItDidLive:
    """B3 on the strict set, which is the whole correctable-and-failed list.

    Under `standard` the validator's correction is applied and B3 sees only
    hard failures, so this is the profile that exercises the proposals -- and
    `full` is the profile the live run that produced `b3-repairs.json` used.
    """

    @pytest.fixture
    def repair(self, options):
        result = plan(options, "full")
        assert result.repair is not None, "B3 did not run"
        return result.repair

    def test_no_proposal_is_rejected_for_want_of_a_recording(self, repair):
        # The regression itself. `_adjudicate` catches whatever the validator
        # raises and calls the proposal rejected, so an unrecorded address is
        # indistinguishable from one the validator refused -- which is how
        # this went unnoticed while every test passed.
        assert repair.rejected == ()
        assert not [e for e in repair.escalated if "could not be validated" in e.reason]

    def test_it_repairs_what_the_live_run_repaired(self, repair, proposals):
        assert len(repair.repaired) == len(proposals)
        assert {r.key for r in repair.repaired} == {
            p["recipient_key"] for p in proposals
        }

    def test_a_repair_carries_the_validators_address_not_the_agents(self, repair, proposals):
        # Same rule as B2: the canonical form is what gets quoted, so the
        # proposal cannot be priced by accident.
        proposed = {p["recipient_key"]: p["proposed"] for p in proposals}
        assert any(
            r.address.zip != proposed[r.key]["zip"] for r in repair.repaired
        )

    def test_nobody_is_silently_dropped(self, repair, options):
        result = plan(options, "full")
        accounted = {r.key for r in result.repair.repaired} | {
            e.recipient_key for e in result.repair.escalated
        }
        assert accounted == {r.key for r in result.validation.for_repair}

    def test_shadow_keeps_the_escalations(self, options):
        # `full` asks for `planner: on` and the prerequisite demotes it,
        # because a fresh ledger has no completed shadow runs. So the repairs
        # are computed and not applied, which is design 6.7's mechanism rather
        # than a half-measure -- and it is why the repaired lanes are recorded
        # by a pass of their own rather than by this run.
        result = plan(options, "full")
        assert result.run.capabilities.planner is PlannerMode.SHADOW
        assert result.run.capabilities.validation is ValidationMode.STRICT
        assert not result.applied_repairs
