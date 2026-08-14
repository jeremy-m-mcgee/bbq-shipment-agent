"""D2's edit-handling mechanism. Design section 4, Phase D.

The three behaviours in design 4's table fall out of one re-solve-and-compare,
so these tests drive real edits through a real solve against recorded quotes
rather than asserting on a classifier in isolation.
"""

from datetime import date
from pathlib import Path

import pytest
from conftest import CapabilityGate

from bbq_shipment_agent.ledger import RunRecord, ShipmentRecord, iter_records, rebuild
from bbq_shipment_agent.planning import RecordedQuoter
from bbq_shipment_agent.recipients import load_roster, to_shipments
from bbq_shipment_agent.review import (
    Edit,
    EditKind,
    EditOutcome,
    EditResult,
    ReviewError,
    ReviewSession,
    TerminalState,
)
from bbq_shipment_agent.run import initialize_run

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = FIXTURES / "shippo-quotes-sf-dc.json"
VALIDATIONS = FIXTURES / "shippo-addresses.json"

SATURDAY = date(2026, 8, 15)
MONDAY = date(2026, 8, 17)
TUESDAY = date(2026, 8, 18)

def _open_run(tmp_path, **kwargs):
    """A run whose capabilities have been evaluated, standing in for the
    post-planning state a `ReviewSession` is created from.

    In production `run review` plans first, which evaluates the three
    capabilities live; the review then reads the fingerprint that produced.
    These tests build the session directly, so they evaluate the same set here
    or the shipment rows would point at a null fingerprint.
    """
    run = initialize_run(
        ledger_root=tmp_path / "ledger",
        gate=CapabilityGate(),
        snapshot_path=tmp_path / "snap.json",
        **kwargs,
    )
    run.planner()
    run.validation()
    run.verification()
    return run


ROSTER = """
origin:
  name: BBQ HQ
  street1: 64 Divisadero St
  city: San Francisco
  state: CA
  zip: "94117"
ship_dates:
  - 2026-08-15
  - 2026-08-17
  - 2026-08-18
recipients:
  - key: ana
    name: Ana Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500-0005"
  - key: bea
    name: Bea Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500-0005"
"""


@pytest.fixture
def session(tmp_path):
    (tmp_path / "recipients.yaml").write_text(ROSTER, encoding="utf-8")
    roster = load_roster(tmp_path / "recipients.yaml")
    run = _open_run(tmp_path, packet_count=roster.packet_count)
    return ReviewSession(
        run,
        to_shipments(roster.recipients),
        roster.origin,
        roster.ship_dates,
        ledger_root=tmp_path / "ledger",
        quoter=RecordedQuoter.from_file(QUOTES),
    )


class _ButtonDrivenReview:
    """The shape `review_view` reads: a session, and no narrator.

    The UI's controller offline, minus the worker thread — enough to render the
    pane from a session built here, without a server or a model.
    """

    def __init__(self, session):
        self.session = session
        self.narrator = None
        self.narrator_available = False
        self.abandoned = False


@pytest.fixture
def advised(tmp_path):
    """The same session, with B2 advisories on one of the two addresses."""
    (tmp_path / "recipients.yaml").write_text(ROSTER, encoding="utf-8")
    roster = load_roster(tmp_path / "recipients.yaml")
    run = _open_run(tmp_path, packet_count=roster.packet_count)
    return ReviewSession(
        run,
        to_shipments(roster.recipients),
        roster.origin,
        roster.ship_dates,
        ledger_root=tmp_path / "ledger",
        quoter=RecordedQuoter.from_file(QUOTES),
        advisories={"ana": ("Check the suite number before using this address.",)},
    )


class TestValidatorAdvisories:
    """Design 10: the validator's notes on a CLEAN address ride the manifest.

    The review session re-assembles the manifest on every solve, so an
    advisory that is not carried into the session is dropped from the only
    manifest the web UI renders on a covering run -- which is what happened,
    silently, until the session took them.
    """

    def test_they_reach_the_manifest_the_review_renders(self, advised):
        assert "Check the suite number" in advised.manifest.advisories["ana"][0]

    def test_they_survive_a_re_solve(self, advised):
        advised.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=TUESDAY))
        # A fact about the address, not about the solve: an edit cannot make
        # the validator's note untrue, so it must not make it disappear.
        assert "ana" in advised.manifest.advisories

    def test_a_session_given_none_carries_none(self, session):
        assert session.manifest.advisories == {}

    def test_they_reach_the_pane_against_the_named_row(self, advised):
        """The seam the bug lived in: session -> view -> `_manifest.html`.

        The template renders `m.advisories[row.name]` and always has; what it
        was handed on a covering run was an empty dict, because the review
        session re-assembled the manifest without them.
        """
        from bbq_shipment_agent.ui.view import review_view

        view = review_view(_ButtonDrivenReview(advised))
        assert view["manifest"]["advisories"]["Ana Ruiz"]


class TestTheDeltaOnTheAffectedRow:
    """Design 4: an applied edit shows what moved, row by row.

    A re-solve is free to change more than the field that was edited -- pinning
    one recipient to a Saturday moved that row's carrier, service, gel pack
    count and cost at once -- and a re-rendered table states none of it.
    """

    def _rows(self, session):
        return {
            row.recipient_key: (row.ship_date, row.carrier, row.service, row.cost)
            for row in session.manifest.rows
        }

    def test_the_rows_it_names_are_exactly_the_rows_that_moved(self, session):
        before = self._rows(session)
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=TUESDAY))
        after = self._rows(session)
        moved = {key for key in before if before[key] != after[key]}
        assert {change.recipient_key for change in result.row_changes} == moved

    def test_a_carrier_move_is_named_before_it_is_confirmed(self, session):
        # The pending banner is where the operator decides, so the row moves
        # have to be on the result that is proposed, not only on the applied one.
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=SATURDAY))
        assert result.outcome is EditOutcome.PAIR_MOVED
        assert result.row_changes
        fields = {field for change in result.row_changes for field, _, _ in change.changes}
        assert "carrier" in fields

    def test_a_refused_edit_moved_nothing_and_says_so(self, session):
        result = EditResult(
            edit=Edit(EditKind.SHIP_DATE, "ana", ship_date=TUESDAY),
            outcome=EditOutcome.REFUSED,
            refusal="arrival above 4.4C",
        )
        assert result.row_changes == ()

    def test_the_sentence_says_applied_once_and_names_both_totals(self, session):
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=TUESDAY))
        sentence = result.describe()
        # The pane used to print its own "Applied." in front of this one.
        assert sentence.count("Applied") == 1
        assert f"${result.cost_before:.2f}" in sentence
        assert f"${result.cost_after:.2f}" in sentence

    def test_an_edit_that_leaves_no_plan_still_reads(self, session):
        session.propose(Edit(EditKind.EXCLUDE, "bea"))
        result = session.propose(Edit(EditKind.EXCLUDE, "ana"))
        assert "no covering plan after this" in result.describe()


class TestOpening:
    def test_it_starts_from_a_solved_manifest(self, session):
        assert session.manifest is not None
        assert len(session.manifest.rows) == 2
        assert session.total_cost > 0

    def test_no_edits_have_happened_yet(self, session):
        assert session.history == []
        assert session.pending is None
        assert session.terminal is None


class TestUnchangedPlan:
    def test_an_edit_that_holds_the_carrier_set_is_applied_silently(self, session):
        before = session.carriers
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=TUESDAY))
        assert result.outcome is EditOutcome.UNCHANGED
        assert result.applied
        assert session.carriers == before
        assert result.cost_delta is not None

    def test_the_delta_is_reported(self, session):
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=TUESDAY))
        assert "Applied" in result.describe()

    def test_the_pin_actually_takes_effect(self, session):
        session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=TUESDAY))
        row = next(r for r in session.manifest.rows if r.recipient_key == "ana")
        assert row.ship_date == TUESDAY


class TestMovedPlan:
    def test_a_saturday_pin_moves_the_carrier_set_and_waits(self, session):
        # Design 3: Saturday is USPS-only, so pinning one recipient there can
        # rewrite the carrier set for the whole run. That is the case the
        # confirmation exists for.
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=SATURDAY))
        assert result.outcome is EditOutcome.PAIR_MOVED
        assert result.needs_confirmation
        assert not result.applied
        assert session.pending is not None

    def test_a_pending_edit_has_not_changed_the_plan(self, session):
        before_cost = session.total_cost
        before_carriers = session.carriers
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=SATURDAY))
        assert result.outcome is EditOutcome.PAIR_MOVED
        assert session.total_cost == before_cost
        assert session.carriers == before_carriers

    def test_confirming_applies_it(self, session):
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=SATURDAY))
        assert result.outcome is EditOutcome.PAIR_MOVED
        session.confirm()
        assert session.pending is None
        row = next(r for r in session.manifest.rows if r.recipient_key == "ana")
        assert row.ship_date == SATURDAY

    def test_discarding_leaves_the_plan_alone(self, session):
        before = session.carriers
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=SATURDAY))
        assert result.outcome is EditOutcome.PAIR_MOVED
        session.discard()
        assert session.pending is None
        assert session.carriers == before

    def test_confirming_nothing_is_an_error(self, session):
        with pytest.raises(ReviewError, match="nothing is waiting"):
            session.confirm()


class TestExclusion:
    def test_excluding_a_recipient_removes_them_from_the_plan(self, session):
        session.propose(Edit(EditKind.EXCLUDE, "bea", reason="asked to be skipped"))
        assert [s.recipient_key for s in session.shipments] == ["ana"]
        assert [r.recipient_key for r in session.manifest.rows] == ["ana"]

    def test_the_exclusion_lands_on_the_manifest_with_its_reason(self, session):
        session.propose(Edit(EditKind.EXCLUDE, "bea", reason="asked to be skipped"))
        assert [e.recipient_key for e in session.manifest.suppressed] == ["bea"]
        assert "asked to be skipped" in session.manifest.suppressed[0].reason

    def test_excluding_someone_not_in_the_run_is_an_error(self, session):
        with pytest.raises(ReviewError, match="not in this run"):
            session.propose(Edit(EditKind.EXCLUDE, "nobody"))


class TestRefusal:
    def test_a_date_that_is_not_a_candidate_is_rejected(self, session):
        with pytest.raises(ReviewError, match="not a candidate ship date"):
            session.propose(
                Edit(EditKind.SHIP_DATE, "ana", ship_date=date(2026, 8, 19))
            )

    def test_a_ship_date_edit_needs_a_date(self, session):
        with pytest.raises(ReviewError, match="needs a date"):
            session.propose(Edit(EditKind.SHIP_DATE, "ana"))

    def test_a_thermally_infeasible_pin_is_refused_with_alternatives(self, tmp_path):
        # The gate is not the operator's to override, so this returns REFUSED
        # rather than asking for confirmation -- and it comes with the dates
        # that would work, computed rather than suggested.
        from dataclasses import replace

        from bbq_shipment_agent.planning import Lane

        (tmp_path / "recipients.yaml").write_text(ROSTER, encoding="utf-8")
        roster = load_roster(tmp_path / "recipients.yaml")
        # A lane hot enough that nothing clears 4.4C on any date.
        blistering = tuple(
            replace(s, lane=Lane(key="blistering", ambient_c=95.0))
            for s in to_shipments(roster.recipients)
        )
        run = _open_run(tmp_path)
        session = ReviewSession(
            run,
            blistering,
            roster.origin,
            roster.ship_dates,
            ledger_root=tmp_path / "ledger",
            quoter=RecordedQuoter.from_file(QUOTES),
        )
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=MONDAY))
        assert result.outcome is EditOutcome.REFUSED
        assert not result.applied
        assert not result.needs_confirmation  # there is no override to offer
        assert "4.4C" in result.refusal
        assert result.alternatives == ()
        assert "No ship date in this run works" in result.describe()


class TestTerminalStates:
    def test_approving_writes_a_shipment_row_per_packet(self, session, tmp_path):
        state = session.approve()
        assert state is TerminalState.APPROVED
        rows = list(iter_records(tmp_path / "ledger", ShipmentRecord))
        assert sorted(r.recipient_key for r in rows) == ["ana", "bea"]

    def test_the_rows_carry_the_plan_and_point_at_the_run(self, session, tmp_path):
        session.approve()
        rows = list(iter_records(tmp_path / "ledger", ShipmentRecord))
        row = next(r for r in rows if r.recipient_key == "ana")
        planned = next(r for r in session.manifest.rows if r.recipient_key == "ana")
        assert row.carrier == planned.carrier
        assert row.cost == planned.cost
        assert row.cap_fingerprint == session.run.cap_fingerprint
        # Dispatch was removed, so these can never be filled (design 9).
        assert row.tracking_number is None and row.actual_arrival is None

    def test_approving_after_an_exclusion_is_a_different_terminal_state(self, session):
        session.propose(Edit(EditKind.EXCLUDE, "bea"))
        assert session.approve() is TerminalState.APPROVED_WITH_EXCLUSIONS

    def test_rejecting_writes_no_shipment_rows(self, session, tmp_path):
        assert session.reject("costs too much") is TerminalState.REJECTED
        assert list(iter_records(tmp_path / "ledger", ShipmentRecord)) == []

    def test_a_terminal_run_is_marked_completed(self, session, tmp_path):
        session.approve()
        connection = rebuild(tmp_path / "ledger")
        try:
            completed, cost = connection.execute(
                "SELECT completed_at, total_cost FROM runs WHERE run_id = ?",
                [session.run.run_id],
            ).fetchone()
        finally:
            connection.close()
        assert completed is not None
        assert cost == session.total_cost

    def test_the_edit_history_is_recorded_on_the_run(self, session, tmp_path):
        # Design 8's metric for verification-enabled is operator edit count,
        # which is only answerable if the edits are written down.
        session.propose(Edit(EditKind.EXCLUDE, "bea"))
        session.approve()
        appends = list(iter_records(tmp_path / "ledger", RunRecord))
        edits = appends[-1].evaluation_reasons["review_edits"]
        assert edits and edits[0]["edit"] == "exclude bea"

    def test_a_review_cannot_end_twice(self, session):
        session.approve()
        with pytest.raises(ReviewError, match="already ended"):
            session.approve()

    def test_editing_after_the_end_is_refused(self, session):
        session.reject()
        with pytest.raises(ReviewError, match="already ended"):
            session.propose(Edit(EditKind.EXCLUDE, "ana"))

    def test_approving_with_an_unconfirmed_edit_is_refused(self, session):
        result = session.propose(Edit(EditKind.SHIP_DATE, "ana", ship_date=SATURDAY))
        assert result.needs_confirmation
        with pytest.raises(ReviewError, match="confirm or discard"):
            session.approve()


class TestDescribeSurvivesAnEmptiedPlan:
    """An exclusion that removes the last covering shipment leaves no plan, so
    the re-solve's `cost_after` -- and thus `cost_delta` -- is None. `describe`
    must say so rather than formatting None into a float (a 500 in the UI)."""

    def test_a_pair_moved_result_with_no_cost_after_describes_cleanly(self):
        result = EditResult(
            edit=Edit(EditKind.EXCLUDE, "ana"),
            outcome=EditOutcome.PAIR_MOVED,
            carriers_before=("UPS",),
            carriers_after=(),
            cost_before=55.56,
            cost_after=None,
        )
        assert "no covering plan after this" in result.describe()  # must not raise

    def test_excluding_the_last_shipment_does_not_raise(self, session):
        # Down to nothing, one at a time. The final exclusion is the one that
        # empties the plan; describing its result must not blow up.
        for key in ("ana", "bea"):
            session.propose(Edit(EditKind.EXCLUDE, key)).describe()
