import pytest

from bbq_shipment_agent.ledger import (
    RECORD_TYPES,
    AgentInvocationRecord,
    LedgerWriter,
    RunRecord,
    ShipmentRecord,
    rebuild,
)


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "ledger"


@pytest.fixture
def writer(ledger):
    return LedgerWriter(ledger)


def test_rebuild_of_an_empty_ledger_yields_the_full_schema(tmp_path):
    # No JSONL has been written yet. The tables must still exist with the
    # right columns, or stage code would fail differently on a first run.
    connection = rebuild(tmp_path / "nothing-here")
    for record_type in RECORD_TYPES:
        rows = connection.execute(f'SELECT * FROM "{record_type.stream}"').fetchall()
        assert rows == []
        described = connection.execute(
            f'DESCRIBE SELECT * FROM "{record_type.stream}_log"'
        ).fetchall()
        assert [row[0] for row in described] == list(record_type.columns())
    connection.close()


class TestFold:
    def test_a_later_append_completes_an_earlier_one(self, writer, ledger):
        writer.append(
            RunRecord(run_id="r1", profile="baseline", packet_count=22,
                      started_at="2026-08-01T00:00:00+00:00")
        )
        writer.append(
            RunRecord(run_id="r1", total_cost=402.1,
                      completed_at="2026-08-01T02:00:00+00:00")
        )

        row = rebuild(ledger).execute(
            "SELECT profile, packet_count, total_cost, completed_at IS NOT NULL "
            "FROM runs"
        ).fetchall()
        assert row == [("baseline", 22, 402.1, True)]

    def test_the_e3_backfill_lands_without_erasing_the_original_row(
        self, writer, ledger
    ):
        writer.append(
            ShipmentRecord(
                run_id="r1", recipient_key="k1", name="Ann", cost=18.4,
                carrier="usps", expected_arrival="2026-08-10T12:00:00+00:00",
            )
        )
        writer.append(
            ShipmentRecord(
                run_id="r1", recipient_key="k1",
                actual_arrival="2026-08-10T14:02:00+00:00",
            )
        )

        row = rebuild(ledger).execute(
            "SELECT name, cost, carrier, actual_arrival FROM shipments"
        ).fetchone()
        assert row[:3] == ("Ann", 18.4, "usps")
        assert row[3] is not None

    def test_a_later_non_null_value_overwrites_an_earlier_one(self, writer, ledger):
        writer.append(ShipmentRecord(run_id="r1", recipient_key="k1", cost=18.4))
        writer.append(ShipmentRecord(run_id="r1", recipient_key="k1", cost=21.0))

        assert rebuild(ledger).execute("SELECT cost FROM shipments").fetchone() == (21.0,)

    def test_runs_and_shipments_stay_separate_entities(self, writer, ledger):
        writer.append(ShipmentRecord(run_id="r1", recipient_key="k1", cost=1.0))
        writer.append(ShipmentRecord(run_id="r1", recipient_key="k2", cost=2.0))
        writer.append(ShipmentRecord(run_id="r2", recipient_key="k1", cost=3.0))

        rows = rebuild(ledger).execute(
            "SELECT run_id, recipient_key, cost FROM shipments "
            "ORDER BY run_id, recipient_key"
        ).fetchall()
        assert rows == [("r1", "k1", 1.0), ("r1", "k2", 2.0), ("r2", "k1", 3.0)]

    def test_folded_seq_reports_when_the_entity_was_last_touched(self, writer, ledger):
        writer.append(ShipmentRecord(run_id="r1", recipient_key="k1"))
        writer.append(ShipmentRecord(run_id="r1", recipient_key="k1"))

        assert rebuild(ledger).execute("SELECT seq FROM shipments").fetchone() == (1,)

    def test_agent_invocations_are_kept_as_distinct_events(self, writer, ledger):
        # Two invocations of the same agent on the same shipment are two facts,
        # not a correction of one another.
        for iteration in (1, 2):
            writer.append(
                AgentInvocationRecord(
                    run_id="r1", agent_key="address-repair", shipment_key="k1",
                    iterations=iteration, outcome="retry",
                )
            )

        rows = rebuild(ledger).execute(
            "SELECT iterations FROM agent_invocations ORDER BY seq"
        ).fetchall()
        assert rows == [(1,), (2,)]


def test_the_log_table_keeps_every_append(writer, ledger):
    writer.append(ShipmentRecord(run_id="r1", recipient_key="k1", cost=18.4))
    writer.append(ShipmentRecord(run_id="r1", recipient_key="k1", cost=21.0))

    connection = rebuild(ledger)
    assert connection.execute("SELECT count(*) FROM shipments_log").fetchone() == (2,)
    assert connection.execute("SELECT count(*) FROM shipments").fetchone() == (1,)


def test_typed_columns_are_queryable_as_their_types(writer, ledger):
    writer.append(
        ShipmentRecord(
            run_id="r1", recipient_key="k1", ship_date="2026-08-08",
            cost=18.4, predicted_arrival_temp=3.1,
        )
    )
    writer.append(RunRecord(run_id="r1", carrier_pair=["usps", "ups"]))

    connection = rebuild(ledger)
    assert connection.execute(
        "SELECT count(*) FROM shipments WHERE ship_date > DATE '2026-08-01'"
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT list_contains(carrier_pair, 'usps') FROM runs"
    ).fetchone() == (True,)


def test_json_columns_survive_the_round_trip(writer, ledger):
    writer.append(
        ShipmentRecord(
            run_id="r1", recipient_key="k1",
            validated_address={"street": "1 Main St", "zip": "02139"},
        )
    )

    assert rebuild(ledger).execute(
        "SELECT validated_address ->> '$.zip' FROM shipments"
    ).fetchone() == ("02139",)


def test_rebuild_is_destructive_and_reproducible(writer, tmp_path, ledger):
    db = tmp_path / "cache" / "ledger.duckdb"
    writer.append(RunRecord(run_id="r1", profile="baseline"))

    rebuild(ledger, db).close()
    connection = rebuild(ledger, db)
    assert connection.execute("SELECT count(*) FROM runs_log").fetchone() == (1,)
    connection.close()


def test_a_run_is_reconstructible_from_the_ledger_alone(writer, ledger):
    # The whole point of section 7: a surprising run must be diagnosable months
    # later from the committed JSONL and nothing else.
    writer.append(
        RunRecord(
            run_id="r1", profile="planner_trial", flag_payload_hash="deadbeef",
            evaluation_reasons={"planner-mode": "TARGET_MATCH"},
        )
    )
    writer.append(
        ShipmentRecord(
            run_id="r1", recipient_key="k1", cap_fingerprint="cap-v1",
            cap_snapshot={"planner": "shadow"}, flag_payload_hash="deadbeef",
        )
    )
    writer.append(
        AgentInvocationRecord(
            run_id="r1", agent_key="manifest-verification",
            instruction_variation_key="v3", instruction_hash="cafe1234",
            model="claude-sonnet-5", iterations=2, outcome="pass",
        )
    )

    connection = rebuild(ledger)
    assert connection.execute(
        "SELECT r.profile, r.flag_payload_hash, s.cap_fingerprint, a.instruction_hash "
        "FROM runs r JOIN shipments s USING (run_id) "
        "JOIN agent_invocations a USING (run_id)"
    ).fetchone() == ("planner_trial", "deadbeef", "cap-v1", "cafe1234")
