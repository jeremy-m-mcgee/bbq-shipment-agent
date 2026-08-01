import json

import pytest

from bbq_shipment_agent.ledger import (
    AgentInvocationRecord,
    LedgerCorruption,
    LedgerWriter,
    RunRecord,
    ShipmentRecord,
    iter_records,
    stream_path,
)


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "ledger"


def test_each_stream_gets_its_own_file(ledger):
    writer = LedgerWriter(ledger)
    writer.append(RunRecord(run_id="r1"))
    writer.append(ShipmentRecord(run_id="r1", recipient_key="k1"))
    writer.append(AgentInvocationRecord(run_id="r1", agent_key="a"))

    assert {p.name for p in ledger.iterdir()} == {
        "runs.jsonl",
        "shipments.jsonl",
        "agent_invocations.jsonl",
    }


def test_seq_and_timestamp_are_assigned_by_the_writer(ledger):
    writer = LedgerWriter(ledger)
    first = writer.append(RunRecord(run_id="r1"))
    second = writer.append(RunRecord(run_id="r2"))

    assert (first.seq, second.seq) == (0, 1)
    assert first.timestamp is not None


def test_seq_is_per_stream(ledger):
    writer = LedgerWriter(ledger)
    writer.append(RunRecord(run_id="r1"))
    shipment = writer.append(ShipmentRecord(run_id="r1", recipient_key="k1"))
    assert shipment.seq == 0


def test_a_caller_supplied_seq_is_refused(ledger):
    # A hand-picked seq could silently reorder history under the fold.
    with pytest.raises(ValueError, match="assigned by the writer"):
        LedgerWriter(ledger).append(RunRecord(run_id="r1", seq=99))


def test_numbering_resumes_across_writer_instances(ledger):
    LedgerWriter(ledger).append(RunRecord(run_id="r1"))
    LedgerWriter(ledger).append(RunRecord(run_id="r2"))

    seqs = [record.seq for record in iter_records(ledger, RunRecord)]
    assert seqs == [0, 1]


def test_appending_never_rewrites_existing_bytes(ledger):
    writer = LedgerWriter(ledger)
    writer.append(ShipmentRecord(run_id="r1", recipient_key="k1", cost=18.4))
    path = stream_path(ledger, ShipmentRecord)
    before = path.read_bytes()

    writer.append(
        ShipmentRecord(
            run_id="r1",
            recipient_key="k1",
            actual_arrival="2026-08-10T14:02:00+00:00",
        )
    )
    assert path.read_bytes().startswith(before)


def test_a_missing_merge_key_is_refused(ledger):
    # Without it the line could never be folded into an entity.
    with pytest.raises(ValueError, match="merge key"):
        LedgerWriter(ledger).append(ShipmentRecord(run_id="r1", recipient_key=None))


def test_writer_has_no_mutating_api():
    for forbidden in ("update", "delete", "truncate", "rewrite"):
        assert not hasattr(LedgerWriter, forbidden)


class TestCorruption:
    def test_a_torn_final_write_is_reported_not_ignored(self, ledger):
        writer = LedgerWriter(ledger)
        writer.append(RunRecord(run_id="r1"))
        path = stream_path(ledger, RunRecord)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"run_id":"r2","se')

        with pytest.raises(LedgerCorruption, match="newline terminator"):
            list(iter_records(ledger, RunRecord))

    def test_a_malformed_line_names_its_line_number(self, ledger):
        writer = LedgerWriter(ledger)
        writer.append(RunRecord(run_id="r1"))
        path = stream_path(ledger, RunRecord)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("not json\n")

        with pytest.raises(LedgerCorruption, match=r":2:"):
            list(iter_records(ledger, RunRecord))

    def test_a_damaged_log_is_caught_before_the_run_appends_to_it(self, ledger):
        path = ledger
        path.mkdir(parents=True)
        (path / "runs.jsonl").write_text("not json\n", encoding="utf-8")

        with pytest.raises(LedgerCorruption):
            LedgerWriter(path).append(RunRecord(run_id="r1"))


def test_lines_are_deterministic_and_compact(ledger):
    writer = LedgerWriter(ledger)
    writer.append(RunRecord(run_id="r1", profile="baseline", packet_count=22))
    line = stream_path(ledger, RunRecord).read_text(encoding="utf-8").strip()

    assert " " not in line  # compact separators keep the committed diff small
    assert list(json.loads(line)) == sorted(json.loads(line))
