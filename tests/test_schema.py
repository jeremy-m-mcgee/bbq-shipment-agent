from dataclasses import fields
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from bbq_shipment_agent.ledger import (
    RECORD_TYPES,
    AgentInvocationRecord,
    RunRecord,
    ShipmentRecord,
)


def test_nulls_are_dropped_so_a_line_records_only_what_was_known():
    record = RunRecord(run_id="r1", profile="baseline")
    assert record.to_dict() == {
        "run_id": "r1",
        "profile": "baseline",
        "schema_version": 1,
    }


def test_round_trip_through_dict_is_stable():
    original = ShipmentRecord(
        run_id="r1",
        recipient_key="k1",
        seq=3,
        cost=18.4,
        ship_date="2026-08-08",
        validated_address={"zip": "02139"},
        timestamp="2026-08-01T00:00:00+00:00",
    )
    assert ShipmentRecord.from_dict(original.to_dict()) == original


def test_reading_a_line_from_a_newer_schema_fails_loudly():
    with pytest.raises(ValueError, match="no field"):
        RunRecord.from_dict({"run_id": "r1", "field_from_the_future": 1})


@pytest.mark.parametrize("record_type", RECORD_TYPES)
def test_every_field_declares_a_duckdb_type(record_type):
    # The rebuild generates DDL from this metadata. A field declared with a
    # bare `= None` instead of `_opt(...)` would have none, so guard the
    # mistake here rather than at rebuild time.
    declared = [f.name for f in fields(record_type)]
    assert list(record_type.columns()) == declared


@pytest.mark.parametrize("record_type", RECORD_TYPES)
def test_every_merge_key_field_exists_as_a_column(record_type):
    assert set(record_type.merge_key) <= set(record_type.columns())


def test_agent_invocations_are_events_and_never_merge():
    assert AgentInvocationRecord.merge_key == ()


class TestTemporalNormalization:
    def test_offset_timestamps_are_converted_to_utc(self):
        record = ShipmentRecord(
            run_id="r1",
            recipient_key="k1",
            actual_arrival="2026-08-10T14:02:00-07:00",
        )
        assert record.actual_arrival == "2026-08-10T21:02:00.000000+00:00"

    def test_datetime_objects_are_accepted(self):
        moment = datetime(2026, 8, 10, 14, 2, tzinfo=timezone(timedelta(hours=-7)))
        record = ShipmentRecord(
            run_id="r1", recipient_key="k1", expected_arrival=moment
        )
        assert record.expected_arrival == "2026-08-10T21:02:00.000000+00:00"

    def test_naive_timestamps_are_refused_rather_than_assumed_utc(self):
        with pytest.raises(ValueError, match="no UTC offset"):
            ShipmentRecord(
                run_id="r1", recipient_key="k1", actual_arrival="2026-08-10T14:02:00"
            )

    def test_date_objects_are_accepted(self):
        record = ShipmentRecord(
            run_id="r1", recipient_key="k1", ship_date=date(2026, 8, 8)
        )
        assert record.ship_date == "2026-08-08"

    def test_a_datetime_is_refused_where_a_calendar_date_is_meant(self):
        # Truncating would be silent, and ship date decides carrier eligibility.
        with pytest.raises(TypeError, match="calendar date"):
            ShipmentRecord(
                run_id="r1",
                recipient_key="k1",
                ship_date=datetime(2026, 8, 8, tzinfo=UTC),
            )

    def test_garbage_timestamps_are_refused(self):
        with pytest.raises(ValueError, match="not an ISO 8601 timestamp"):
            RunRecord(run_id="r1", started_at="last tuesday")
