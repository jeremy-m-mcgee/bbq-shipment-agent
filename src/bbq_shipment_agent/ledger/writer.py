"""Append-only JSONL ledger writer.

The ledger is the source of truth and is committed to the repo. DuckDB is a
derived cache built from these files and is never written to directly -- see
`rebuild.py`.

There is deliberately no update, delete, or truncate method on this class.
Correcting or completing a value means appending another partial record, which
is what keeps a surprising run diagnosable months later.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from .schema import LedgerRecord, utc_now


class LedgerCorruption(Exception):
    """The on-disk log is not in a state the writer can safely extend."""


def stream_path(root: Path, record_type: type[LedgerRecord]) -> Path:
    return Path(root) / f"{record_type.stream}.jsonl"


def iter_records(
    root: Path, record_type: type[LedgerRecord]
) -> Iterator[LedgerRecord]:
    """Every append for a stream, in write order, as typed records.

    This is the raw log, not the folded view: a shipment that was backfilled
    appears once per append. Use `rebuild` to query entities.
    """
    path = stream_path(root, record_type)
    if not path.exists():
        return
    for lineno, line in enumerate(_iter_lines(path), start=1):
        try:
            yield record_type.from_dict(json.loads(line))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise LedgerCorruption(f"{path}:{lineno}: {exc}") from exc


def _iter_lines(path: Path) -> Iterator[str]:
    """Yield complete lines, refusing to read past a torn final write."""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.endswith("\n"):
                # Every append is a single O_APPEND write followed by fsync, so
                # a missing terminator means the process died mid-write. Better
                # to stop loudly than to silently drop or misparse the tail.
                raise LedgerCorruption(
                    f"{path}: final line has no newline terminator, which means "
                    "a write was interrupted. Inspect and repair the tail by "
                    "hand before appending."
                )
            if line.strip():
                yield line


class LedgerWriter:
    """Appends records to per-stream JSONL files under `root`.

    Each append is opened, written, flushed, fsynced, and closed. At ~22
    shipments across three to five runs a year the cost is irrelevant, and it
    buys durability without any open-handle lifecycle to get wrong.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._next_seq: dict[str, int] = {}

    def append(self, record: LedgerRecord) -> LedgerRecord:
        """Write one record. Returns it with `seq` and `timestamp` filled in.

        Both are writer-owned. A caller-supplied `seq` is rejected rather than
        honored, because sequence numbers are what the fold in `rebuild`
        orders by and a hand-picked one could silently reorder history.
        """
        record_type = type(record)
        if record.seq is not None:
            raise ValueError(
                "seq is assigned by the writer; leave it unset on append."
            )
        for key_field in record_type.merge_key:
            if getattr(record, key_field) is None:
                raise ValueError(
                    f"{record_type.__name__}.{key_field} is part of the merge "
                    "key and must be set on every append, otherwise this line "
                    "cannot be folded into an entity."
                )

        record.seq = self._claim_seq(record_type)
        if record.timestamp is None:
            record.timestamp = utc_now()

        line = json.dumps(record.to_dict(), separators=(",", ":"), sort_keys=True)
        path = stream_path(self.root, record_type)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def append_all(self, records: Iterable[LedgerRecord]) -> list[LedgerRecord]:
        return [self.append(record) for record in records]

    def _claim_seq(self, record_type: type[LedgerRecord]) -> int:
        stream = record_type.stream
        if stream not in self._next_seq:
            self._next_seq[stream] = self._scan_next_seq(record_type)
        seq = self._next_seq[stream]
        self._next_seq[stream] = seq + 1
        return seq

    def _scan_next_seq(self, record_type: type[LedgerRecord]) -> int:
        """Resume numbering from what is on disk.

        Derived from the max `seq` actually present rather than a line count,
        so this doubles as a parse check of the existing log at open time. A
        run that is going to fail on a malformed ledger should fail before it
        starts appending to it.
        """
        highest = -1
        for record in iter_records(self.root, record_type):
            if record.seq is None:
                raise LedgerCorruption(
                    f"{stream_path(self.root, record_type)}: a line is missing "
                    "its seq, so append order cannot be established."
                )
            highest = max(highest, record.seq)
        return highest + 1
