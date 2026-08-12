"""The spine end to end: A1 -> B2 -> C1..C6, against real recorded answers.

Build order step 3's claim is that the system is useful on its own at this
point. That is a claim about the whole, not the parts, so these tests run the
whole: a roster file in, a manifest out, no model calls and no invented
numbers anywhere in between.
"""

from datetime import date
from pathlib import Path

import pytest
from conftest import CapabilityGate

from bbq_shipment_agent.agent_configs import SnapshotAgentConfigs
from bbq_shipment_agent.agents.model import ModelUnavailable
from bbq_shipment_agent.capabilities import ValidationMode
from bbq_shipment_agent.ledger import RunRecord, iter_records, rebuild
from bbq_shipment_agent.plan import plan_run
from bbq_shipment_agent.planning import RecordedQuoter
from bbq_shipment_agent.recipients import RecordedAddressValidator, load_roster
from bbq_shipment_agent.run import initialize_run

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = FIXTURES / "shippo-quotes-sf-dc.json"
VALIDATIONS = FIXTURES / "shippo-addresses.json"
COMPLETIONS = FIXTURES / "d1-completions.json"
SNAPSHOT = Path(__file__).parent.parent / "config" / "ld-snapshot.json"

# One recipient, because the recorded lane is one lane. The point of these
# tests is that the stages compose, and a second copy of the same lane would
# test the loop rather than the composition.
ROSTER = """
origin:
  name: BBQ HQ
  street1: 64 Divisadero St
  city: San Francisco
  state: CA
  zip: "94117"
ship_dates:
  - 2026-08-17
recipients:
  - key: ana
    name: Ana Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500"
"""


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "recipients.yaml").write_text(ROSTER, encoding="utf-8")
    return tmp_path


def _gate(*, planner=None, validation=None, verification=None):
    caps = {
        name: value
        for name, value in {
            "planner": planner,
            "validation": validation,
            "verification": verification,
        }.items()
        if value is not None
    }
    return CapabilityGate(**caps)


def run_plan(
    workspace,
    *,
    planner=None,
    validation=None,
    verification=None,
    roster_text=None,
    verifier=None,
    agent_source=None,
    extraction_escalations=(),
):
    if roster_text is not None:
        (workspace / "recipients.yaml").write_text(roster_text, encoding="utf-8")

    roster = load_roster(workspace / "recipients.yaml")
    run = initialize_run(
        ledger_root=workspace / "ledger",
        gate=_gate(planner=planner, validation=validation, verification=verification),
        agent_source=agent_source,
        snapshot_path=workspace / "snapshot.json",
        packet_count=roster.packet_count,
    )
    return plan_run(
        run,
        roster,
        ledger_root=workspace / "ledger",
        quoter=RecordedQuoter.from_file(QUOTES),
        validator=RecordedAddressValidator.from_file(VALIDATIONS),
        verifier=verifier,
        extraction_escalations=extraction_escalations,
    )


class TestTheWholeSpine:
    def test_a_roster_file_becomes_a_manifest(self, workspace):
        result = run_plan(workspace)
        assert result.manifest is not None
        assert [r.recipient_key for r in result.manifest.rows] == ["ana"]

    def test_every_row_carries_a_real_carrier_and_a_real_price(self, workspace):
        row = run_plan(workspace).manifest.rows[0]
        assert row.carrier in {"UPS", "USPS"}
        assert row.cost > 0
        assert row.service

    def test_the_thermal_gate_actually_ran(self, workspace):
        # Not a smoke test: the arrival temperature has to clear 4.4C, which
        # is the one constraint the design will not let anything override.
        row = run_plan(workspace).manifest.rows[0]
        assert row.predicted_arrival_temp_c <= 4.4
        assert row.thermal_margin_c >= 0

    def test_the_manifest_carries_the_run_identity(self, workspace):
        result = run_plan(workspace)
        assert result.manifest.run_id == result.run.run_id
        assert result.manifest.cap_fingerprint == result.run.cap_fingerprint

    def test_no_more_carriers_than_the_hard_limit(self, workspace):
        assert len(run_plan(workspace).manifest.carriers) <= 2


class TestValidationIsInThePath:
    def test_the_validated_address_is_the_one_that_was_quoted(self, workspace):
        # B2 runs before C2 precisely so this is true. The recorded quotes for
        # the enriched ZIP+4 destination were captured separately from the
        # submitted-form ones, so a manifest built without validation would
        # have priced a different key.
        row = run_plan(workspace).manifest.rows[0]
        assert row.address["zip"] == "20500-0005"

    def test_off_ships_the_address_exactly_as_supplied(self, workspace):
        row = run_plan(workspace, validation="off").manifest.rows[0]
        assert row.address["zip"] == "20500"

    def test_the_mode_comes_off_the_evaluated_capability(self, workspace):
        result = run_plan(workspace, validation="strict")
        assert result.validation.mode is ValidationMode.STRICT
        # Cached: validation was evaluated live during planning, under the
        # address_validation stage context.
        assert result.run.validation() is ValidationMode.STRICT

    def test_a_failed_address_is_escalated_and_never_reaches_planning(self, workspace):
        roster = ROSTER.replace(
            """  - key: ana
    name: Ana Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500"
""",
            """  - key: nobody
    name: Nobody Home
    street1: 99999 Nowhere Blvd
    city: Fargo
    state: ND
    zip: "58102"
""",
        )
        result = run_plan(workspace, roster_text=roster)
        assert [e.recipient_key for e in result.escalated] == ["nobody"]
        # Nothing was quoted, so there is nothing to build a manifest from.
        assert result.manifest is None
        assert "no carrier subset" in result.reason


class TestTheLedger:
    def test_planning_appends_to_the_run_row_opened_at_a1(self, workspace):
        result = run_plan(workspace)
        appends = [
            r
            for r in iter_records(workspace / "ledger", RunRecord)
            if r.run_id == result.run.run_id
        ]
        assert len(appends) == 2
        assert appends[0].started_at and appends[0].total_cost is None
        assert appends[1].started_at is None and appends[1].total_cost is not None

    def test_the_folded_row_carries_both_halves(self, workspace):
        # Design 7: a line is a partial update, and the derived table folds
        # them. The run row has to end up with A1's identity *and* planning's
        # numbers, or neither append was worth writing.
        result = run_plan(workspace)
        connection = rebuild(workspace / "ledger")
        try:
            row = connection.execute(
                "SELECT profile, cap_fingerprint, packet_count, total_cost, "
                "carrier_pair, escalated_count, completed_at FROM runs "
                "WHERE run_id = ?",
                [result.run.run_id],
            ).fetchone()
        finally:
            connection.close()
        profile, fingerprint, packets, cost, carriers, escalated, completed = row
        assert profile == "baseline"
        assert fingerprint == result.run.cap_fingerprint
        assert packets == 1
        assert cost == result.manifest.total_cost
        assert list(carriers) == list(result.manifest.carriers)
        assert escalated == 0
        # Planning is not a terminal state. Dispatch is, and it has not run.
        assert completed is None

    def test_the_available_carriers_are_recorded(self, workspace):
        # A carrier silently missing from a run is indistinguishable from one
        # with no service on the lane unless this is written down.
        run_plan(workspace)
        appends = list(iter_records(workspace / "ledger", RunRecord))
        reasons = appends[-1].evaluation_reasons
        assert set(reasons["available_carriers"]) == {"UPS", "USPS"}
        assert reasons["validation_mode"] == "standard"


class TestB1sUnresolvedPeopleAreAccountedFor:
    """Someone a screenshot named who gave no usable address.

    They never reach B2, so before this they existed only in a panel on the
    results page: absent from the manifest, from D1's payload, from the
    narrator's, and from the run row's count. Asked directly where two of them
    had gone, `review-narrator` answered — correctly — that it had no record of
    them, while D1 reported every completeness check clean.
    """

    def _escalation(self):
        from bbq_shipment_agent.planning import Excluded

        return Excluded(
            recipient_key="jules_g",
            name="jules_g",
            reason="no usable address in the source image: said 'my place'",
        )

    def test_they_reach_the_manifest_as_escalations(self, workspace):
        result = run_plan(workspace, extraction_escalations=(self._escalation(),))
        assert "jules_g" in [e.recipient_key for e in result.manifest.escalated]

    def test_they_are_in_the_runs_one_escalation_list(self, workspace):
        result = run_plan(workspace, extraction_escalations=(self._escalation(),))
        assert "jules_g" in [e.recipient_key for e in result.escalated]

    def test_the_completeness_check_can_see_them(self, workspace):
        """D1's first check is that every input recipient lands in exactly one
        bucket. With B1's people missing from both sides it compared the
        survivors against themselves and reported clean."""
        from bbq_shipment_agent.agents.verification import manifest_payload

        result = run_plan(workspace, extraction_escalations=(self._escalation(),))
        payload = manifest_payload(
            result.manifest,
            tuple(r.key for r in result.roster.recipients) + ("jules_g",),
        )
        assert "jules_g" in payload["input_recipients"]
        assert "jules_g" in [e["recipient_key"] for e in payload["escalated"]]

    def test_the_run_row_counts_them(self, workspace):
        result = run_plan(workspace, extraction_escalations=(self._escalation(),))
        appends = [
            r
            for r in iter_records(workspace / "ledger", RunRecord)
            if r.run_id == result.run.run_id and r.escalated_count is not None
        ]
        assert appends[-1].escalated_count == 1

    def test_a_run_without_any_is_unchanged(self, workspace):
        result = run_plan(workspace)
        assert result.escalated == ()
        assert result.manifest.escalated == ()


class TestTheAgentBoundary:
    def test_the_deterministic_spine_invokes_no_agents(self, workspace):
        # Design 11 step 3: "This should produce a complete manifest with zero
        # model calls." The ledger is where that claim is checkable, and it
        # stays true with verification off however many agents exist.
        from bbq_shipment_agent.ledger import AgentInvocationRecord

        run_plan(workspace)
        invocations = list(
            iter_records(workspace / "ledger", AgentInvocationRecord)
        )
        assert invocations == []

    def test_d1_runs_and_is_recorded_when_verification_is_on(self, workspace):
        from bbq_shipment_agent.agents import RecordedModel
        from bbq_shipment_agent.agents.verification import CONFIG_KEY as AGENT_KEY
        from bbq_shipment_agent.ledger import AgentInvocationRecord

        result = run_plan(
            workspace,
            verification="on",
            agent_source=SnapshotAgentConfigs(SNAPSHOT),
            verifier=RecordedModel.from_file(COMPLETIONS),
        )
        assert result.verification.ran
        records = list(iter_records(workspace / "ledger", AgentInvocationRecord))
        assert [r.agent_key for r in records] == [AGENT_KEY]

    def test_d1_cannot_change_the_manifest_it_reviews(self, workspace):
        # Design 10 settles the read-only contradiction this way: the agent
        # reports, the spine acts. The same plan with and without the critique
        # pass must be byte-identical.
        from bbq_shipment_agent.agent_configs import SnapshotAgentConfigs
        from bbq_shipment_agent.agents import RecordedModel

        without = run_plan(workspace)
        with_d1 = run_plan(
            workspace,
            verification="on",
            agent_source=SnapshotAgentConfigs(SNAPSHOT),
            verifier=RecordedModel.from_file(COMPLETIONS),
        )
        assert with_d1.verification.findings
        assert with_d1.manifest.rows == without.manifest.rows
        assert with_d1.manifest.carriers == without.manifest.carriers

    def test_a_manifest_that_does_not_exist_is_not_sent_to_the_agent(self, workspace):
        # A missing manifest is not a manifest with problems. Asking a
        # read-only critic to review nothing burns a call to learn what the
        # caller already knows.
        roster = ROSTER.replace(
            '    street1: 1600 Pennsylvania Ave NW\n    city: Washington\n'
            '    state: DC\n    zip: "20500"\n',
            '    street1: 99999 Nowhere Blvd\n    city: Fargo\n'
            '    state: ND\n    zip: "58102"\n',
        )
        result = run_plan(workspace, verification="on", roster_text=roster)
        assert result.manifest is None
        assert result.verification is None


class TestShipDates:
    def test_only_the_declared_dates_are_planned_against(self, workspace):
        row = run_plan(workspace).manifest.rows[0]
        assert row.ship_date == date(2026, 8, 17)

    def test_an_operator_pin_survives_into_the_manifest(self, workspace):
        # Design 3's Saturday interaction enters the plan only this way.
        roster = ROSTER.replace(
            "ship_dates:\n  - 2026-08-17\n",
            "ship_dates:\n  - 2026-08-17\n  - 2026-08-15\n",
        ).replace('    zip: "20500"\n', '    zip: "20500"\n    ship_date: 2026-08-15\n')
        result = run_plan(workspace, roster_text=roster)
        assert result.manifest.rows[0].ship_date == date(2026, 8, 15)
        # Saturday is USPS-only for perishables, so the pin decides the carrier.
        assert result.manifest.carriers == ("USPS",)
        assert result.manifest.forced_by_saturday


class TestDedupeIsInTheSpine:
    def test_two_recipients_at_one_doorstep_get_one_parcel(self, workspace):
        roster = ROSTER + """  - key: bea
    name: Bea Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500"
"""
        result = run_plan(workspace, roster_text=roster)
        assert [r.recipient_key for r in result.manifest.rows] == ["ana"]
        assert result.suppression.consolidated == {"ana": ("bea",)}

    def test_the_suppression_reaches_the_manifest(self, workspace):
        roster = ROSTER + """  - key: bea
    name: Bea Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500"
"""
        result = run_plan(workspace, roster_text=roster)
        assert [e.recipient_key for e in result.manifest.suppressed] == ["bea"]
        assert "same address as Ana Ruiz" in result.manifest.suppressed[0].reason

    def test_it_runs_after_validation_not_before(self, workspace):
        # The order is load-bearing, not cosmetic. These two submit different
        # ZIPs for one doorstep; only the validator's canonical form makes them
        # equal, so deduping the submitted addresses would ship twice.
        roster = ROSTER.replace('    zip: "20500"', '    zip: "20500-0005"') + """  - key: bea
    name: Bea Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500"
"""
        result = run_plan(workspace, roster_text=roster)
        assert len(result.manifest.rows) == 1

    def test_nobody_is_silently_dropped(self, workspace):
        # D1's first check, made answerable: every input recipient lands in
        # exactly one of eligible, suppressed or escalated.
        roster = ROSTER + """  - key: bea
    name: Bea Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500"
"""
        result = run_plan(workspace, roster_text=roster)
        assert set(result.manifest.accounted_for()) == {
            r.key for r in result.roster.recipients
        }

    def test_the_suppression_count_reaches_the_ledger(self, workspace):
        roster = ROSTER + """  - key: bea
    name: Bea Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500"
"""
        run_plan(workspace, roster_text=roster)
        appends = list(iter_records(workspace / "ledger", RunRecord))
        assert appends[-1].suppressed_count == 1
        # The packer needs it the other way round: this parcel covers these
        # people, so a card with two names goes in the box.
        assert appends[-1].evaluation_reasons["consolidated"] == {"ana": ["bea"]}

    def test_a_run_with_no_duplicates_suppresses_nothing(self, workspace):
        result = run_plan(workspace)
        assert result.suppression.suppressed == ()
        assert result.manifest.suppressed == ()


class TestRepairIsInTheSpine:
    """B3 between B2 and B4, gated by `planner-mode`."""

    def test_planner_off_never_calls_the_repairer(self, workspace):
        called = []

        class Spy:
            def converse(self, *a, **k):
                called.append(1)
                raise AssertionError("planner is off; B3 must not run")

        run_plan(workspace, validation="strict", verifier=None)
        result = run_plan(workspace, validation="strict")
        assert result.repair is None
        assert called == []

    def test_shadow_runs_the_loop_and_does_not_apply_it(self, workspace):
        # A second recipient whose address is *correctable*, so strict mode
        # actually gives B3 something to do. Ana alone validates clean --
        # ZIP+4 enrichment is not a material change.
        # Design 6.7: shadow runs the path and logs its output without acting
        # on it, which is how a capability earns promotion. A shadow run that
        # quietly applied repairs would be `on` wearing a different name.
        import json as _json

        from bbq_shipment_agent.agents.model import Completion

        (workspace / "recipients.yaml").write_text(
            ROSTER + """  - key: sloppy
    name: Sloppy Sam
    street1: 64 divisadero st
    city: san francisco
    state: CA
    zip: "94110"
""",
            encoding="utf-8",
        )
        roster = load_roster(workspace / "recipients.yaml")
        run = initialize_run(
            ledger_root=workspace / "ledger",
            gate=_gate(planner="shadow", validation="strict"),
            agent_source=SnapshotAgentConfigs(SNAPSHOT),
            snapshot_path=workspace / "snapshot.json",
        )

        class Repairs:
            def converse(self, invocation, messages, tools=()):
                return Completion(text=_json.dumps({"repairs": [], "escalations": []}))

        result = plan_run(
            run,
            roster,
            ledger_root=workspace / "ledger",
            quoter=RecordedQuoter.from_file(QUOTES),
            validator=RecordedAddressValidator.from_file(VALIDATIONS),
            repairer=Repairs(),
        )
        assert result.repair is not None, "shadow still runs the loop"
        assert not result.applied_repairs
        # The escalations are B2's, untouched by what B3 would have done.
        assert result.escalated == result.validation.escalated
        assert result.repair_unavailable is None, "it ran; nothing to explain"

    def test_a_failed_repair_loop_is_recorded_not_swallowed(self, workspace):
        """B3 asked-and-failed must not look like B3 never-asked.

        `repair` is `None` for both, so without a reason the two are the same
        run from outside. A live strict run lost ten recipients this way: the
        loop died, the escalations stayed, eight packets left the manifest,
        and the exit code was 0. Design 2 says a surprising run is diagnosable
        from the committed JSONL alone, and that one was not.
        """
        (workspace / "recipients.yaml").write_text(
            ROSTER + """  - key: sloppy
    name: Sloppy Sam
    street1: 64 divisadero st
    city: san francisco
    state: CA
    zip: "94110"
""",
            encoding="utf-8",
        )
        roster = load_roster(workspace / "recipients.yaml")
        run = initialize_run(
            ledger_root=workspace / "ledger",
            gate=_gate(planner="shadow", validation="strict"),
            agent_source=SnapshotAgentConfigs(SNAPSHOT),
            snapshot_path=workspace / "snapshot.json",
        )

        class Dies:
            """The shape of a transient provider error mid-loop."""

            def converse(self, invocation, messages, tools=()):
                raise ModelUnavailable("address-repair: 529 overloaded_error")

        result = plan_run(
            run,
            roster,
            ledger_root=workspace / "ledger",
            quoter=RecordedQuoter.from_file(QUOTES),
            validator=RecordedAddressValidator.from_file(VALIDATIONS),
            repairer=Dies(),
        )

        # The safe direction is unchanged: nobody was repaired, and everyone
        # B2 escalated is still escalated.
        assert result.repair is None
        assert result.escalated == result.validation.escalated

        # ...but the run now says so, in both places a reader looks.
        assert result.repair_unavailable is not None
        assert "overloaded_error" in result.repair_unavailable

        appends = list(iter_records(workspace / "ledger", RunRecord))
        reasons = appends[-1].evaluation_reasons
        assert "overloaded_error" in reasons["repair_unavailable"]

    def test_a_repair_loop_never_asked_records_nothing(self, workspace):
        """The other half. `planner-mode: off` is a decision, not a failure,
        and a reason on that run would be noise that reads like a fault."""
        run_plan(workspace, validation="strict", verifier=None)
        result = run_plan(workspace, validation="strict")
        assert result.repair is None
        assert result.repair_unavailable is None
        appends = list(iter_records(workspace / "ledger", RunRecord))
        assert "repair_unavailable" not in appends[-1].evaluation_reasons


class TestThePlanReviewSeam:
    """What `run plan` produces is what `run review` consumes.

    This is the one join the suite did not exercise. `test_review.py` builds
    its `ReviewSession` from `to_shipments(roster.recipients)` by hand, and
    `test_plan.py` stopped at the manifest, so the boundary the CLI actually
    crosses -- `PlanResult` in, `ReviewSession` out -- was covered from both
    sides and not in the middle.

    It was broken the whole time. `_review` passed `suppression.eligible`
    straight through, which is phase B's `Recipient`, to a session that solves
    over phase C's `Shipment`; the first person to drive the conversation end
    to end got `'Recipient' object has no attribute 'recipient_key'`. Both
    types carry a name and an address and differ in one field name, which is
    exactly the kind of near-miss a type boundary exists to catch and an
    untested seam lets through.
    """

    def _session(self, workspace, result, roster):
        from bbq_shipment_agent.recipients import to_shipments
        from bbq_shipment_agent.review import ReviewSession

        return ReviewSession(
            result.run,
            to_shipments(result.suppression.eligible),
            roster.origin,
            roster.ship_dates,
            ledger_root=workspace / "ledger",
            quoter=RecordedQuoter.from_file(QUOTES),
            escalated=result.validation.escalated,
            suppressed=result.suppression.suppressed,
        )

    def test_a_planned_run_opens_a_review(self, workspace):
        result = run_plan(workspace)
        roster = load_roster(workspace / "recipients.yaml")
        session = self._session(workspace, result, roster)
        assert session.manifest is not None
        assert session.terminal is None

    def test_the_review_solves_the_same_plan_the_manifest_showed(self, workspace):
        # Not merely "it constructed": a session that silently solved a
        # different set would be the same class of bug one layer deeper.
        result = run_plan(workspace)
        roster = load_roster(workspace / "recipients.yaml")
        session = self._session(workspace, result, roster)
        assert session.total_cost == result.manifest.total_cost
        assert set(session.carriers) == set(result.manifest.carriers)
        assert {s.recipient_key for s in session.shipments} == {
            row.recipient_key for row in result.manifest.rows
        }

    def test_every_eligible_recipient_crossed_the_boundary(self, workspace):
        result = run_plan(workspace)
        roster = load_roster(workspace / "recipients.yaml")
        session = self._session(workspace, result, roster)
        assert {s.recipient_key for s in session.shipments} == {
            r.key for r in result.suppression.eligible
        }
