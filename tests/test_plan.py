"""The spine end to end: A1 -> B2 -> C1..C6, against real recorded answers.

Build order step 3's claim is that the system is useful on its own at this
point. That is a claim about the whole, not the parts, so these tests run the
whole: a roster file in, a manifest out, no model calls and no invented
numbers anywhere in between.
"""

import textwrap
from datetime import date
from pathlib import Path

import pytest

from bbq_shipment_agent.capabilities import ValidationMode
from bbq_shipment_agent.ledger import RunRecord, iter_records, rebuild
from bbq_shipment_agent.plan import plan_run
from bbq_shipment_agent.planning import RecordedQuoter
from bbq_shipment_agent.recipients import RecordedAddressValidator, load_roster
from bbq_shipment_agent.run import initialize_run

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = FIXTURES / "shippo-quotes-sf-dc.json"
VALIDATIONS = FIXTURES / "shippo-addresses.json"

CONFIG = """
    profiles:
      baseline:
        planner: "off"
        memory: "off"
        validation: "{validation}"
        verification: "off"
        authority: "propose_only"
    default_profile: "baseline"
    authority_ceiling: "propose_only"
    kill_switch: false
"""

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
    (tmp_path / "capabilities.yaml").write_text(
        textwrap.dedent(CONFIG).format(validation="standard"), encoding="utf-8"
    )
    (tmp_path / "recipients.yaml").write_text(ROSTER, encoding="utf-8")
    return tmp_path


def run_plan(workspace, *, validation=None, roster_text=None):
    if validation is not None:
        (workspace / "capabilities.yaml").write_text(
            textwrap.dedent(CONFIG).format(validation=validation), encoding="utf-8"
        )
    if roster_text is not None:
        (workspace / "recipients.yaml").write_text(roster_text, encoding="utf-8")

    roster = load_roster(workspace / "recipients.yaml")
    run = initialize_run(
        ledger_root=workspace / "ledger",
        config_path=workspace / "capabilities.yaml",
        snapshot_path=workspace / "snapshot.json",
        packet_count=roster.packet_count,
    )
    return plan_run(
        run,
        roster,
        ledger_root=workspace / "ledger",
        quoter=RecordedQuoter.from_file(QUOTES),
        validator=RecordedAddressValidator.from_file(VALIDATIONS),
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

    def test_the_mode_comes_off_the_resolved_capabilities(self, workspace):
        result = run_plan(workspace, validation="strict")
        assert result.validation.mode is ValidationMode.STRICT
        assert result.run.capabilities.validation is ValidationMode.STRICT

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
        result = run_plan(workspace)
        appends = list(iter_records(workspace / "ledger", RunRecord))
        reasons = appends[-1].evaluation_reasons
        assert set(reasons["available_carriers"]) == {"UPS", "USPS"}
        assert reasons["validation_mode"] == "standard"


class TestNoModelsRan:
    def test_the_spine_invokes_no_agents(self, workspace):
        # Design 11 step 3: "This should produce a complete manifest with zero
        # model calls." The ledger is where that claim is checkable.
        from bbq_shipment_agent.ledger import AgentInvocationRecord

        run_plan(workspace)
        invocations = list(
            iter_records(workspace / "ledger", AgentInvocationRecord)
        )
        assert invocations == []


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
