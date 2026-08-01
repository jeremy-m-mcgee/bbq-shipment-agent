"""Rebuild the derived DuckDB cache from the JSONL ledger.

The cache is disposable. It is dropped and rebuilt from scratch on every call,
because the moment a rebuild becomes incremental the database starts holding
state that the JSONL does not, and it stops being derived.

Two layers per stream:

``<stream>_log``
    Every append, verbatim, one row per line. This is the audit trail.

``<stream>``
    A view folding the log into current entities: group by merge key, take the
    last non-null value of each field in `seq` order. This is what the rest of
    the pipeline queries. For event streams (agent invocations) there is
    nothing to fold and the view passes the log through unchanged.

Both the DDL and the fold are generated from the dataclass field definitions
in `schema.py`, so a new field appears in the cache without anyone remembering
to update SQL in a second place.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from .schema import RECORD_TYPES, LedgerRecord
from .writer import stream_path

_JSON_FORMAT = "newline_delimited"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def log_table_ddl(record_type: type[LedgerRecord]) -> str:
    """`CREATE TABLE` for a stream's raw log."""
    columns = ",\n    ".join(
        f"{_quote(name)} {sql_type}"
        for name, sql_type in record_type.columns().items()
    )
    return f"CREATE TABLE {_quote(record_type.stream + '_log')} (\n    {columns}\n)"


def entity_view_ddl(record_type: type[LedgerRecord]) -> str:
    """`CREATE VIEW` folding a stream's log into current entities."""
    log = _quote(record_type.stream + "_log")
    view = _quote(record_type.stream)
    key = record_type.merge_key

    if not key:
        return f"CREATE VIEW {view} AS SELECT * FROM {log}"

    projections = []
    for name in record_type.columns():
        if name in key:
            projections.append(_quote(name))
        elif name == "seq":
            # For a folded entity `seq` means "when was this last touched",
            # which is the useful reading and keeps the column non-null.
            projections.append(f"max({_quote(name)}) AS {_quote(name)}")
        else:
            # arg_max skips rows where the argument is NULL, so this is exactly
            # last-non-null-wins -- the fold the append-only design needs.
            projections.append(
                f"arg_max({_quote(name)}, {_quote('seq')}) AS {_quote(name)}"
            )

    group_by = ", ".join(_quote(name) for name in key)
    return (
        f"CREATE VIEW {view} AS\nSELECT\n    "
        + ",\n    ".join(projections)
        + f"\nFROM {log}\nGROUP BY {group_by}"
    )


def rebuild(
    root: Path | str, db_path: Path | str | None = None
) -> duckdb.DuckDBPyConnection:
    """Build the derived cache from the JSONL under `root`.

    With `db_path` the database is replaced on disk; without it the cache is
    built in memory, which is what tests and one-off queries want. Returns an
    open connection either way -- the caller owns closing it.
    """
    root = Path(root)

    if db_path is not None:
        db_path = Path(db_path)
        # Safe because the file is derived by construction: everything in it
        # came from the JSONL and is about to be rewritten from the same
        # source. Nothing is ever written here that is not recoverable.
        db_path.unlink(missing_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect(str(db_path))
    else:
        connection = duckdb.connect()

    for record_type in RECORD_TYPES:
        connection.execute(log_table_ddl(record_type))
        path = stream_path(root, record_type)
        if path.exists():
            connection.execute(
                f"INSERT INTO {_quote(record_type.stream + '_log')} "
                "SELECT * FROM read_json(?, columns = ?, format = ?)",
                [str(path), record_type.columns(), _JSON_FORMAT],
            )
        connection.execute(entity_view_ddl(record_type))

    return connection
