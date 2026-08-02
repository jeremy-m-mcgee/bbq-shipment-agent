"""Ledger record schemas. See docs/design.md section 7.

Three record streams, one JSONL file each, all append-only.

Append-only storage and the design's "backfill actuals into the existing
ledger row" (E3) cannot both be literally true, so a line is not a row: it is
a *partial update* to a row. Every append carries its merge key plus whatever
fields are known at the time, and null fields are omitted entirely. The
derived table folds all appends for a key by taking the last non-null value of
each field in `seq` order.

That one rule covers every deferred write in the pipeline. A1 opens a run
record with `started_at`; the run's closing append adds `total_cost` and
`completed_at`; E3 adds `actual_arrival` to a shipment days later. None of
them mutate a byte already on disk, and the ledger stays a clean `git` diff.

The consequence to know about: a field can never be un-set once written, only
overwritten with another non-null value.

`AgentInvocationRecord` is the exception. An invocation is an event, not an
entity, so those lines never merge -- two invocations of the same agent on the
same shipment are two facts, not a correction.
"""

from __future__ import annotations

from dataclasses import Field, dataclass, field, fields
from datetime import date, datetime, timezone
from typing import Any, ClassVar

# Bump when a field changes meaning or is removed. Adding a nullable field does
# not require a bump: DuckDB reads an absent key as NULL, so old lines stay
# readable against the new schema.
SCHEMA_VERSION = 1

DUCKDB_TYPE = "duckdb_type"


def utc_now() -> str:
    """Wall-clock stamp for a ledger line. UTC, microseconds, ISO 8601."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _normalize_timestamp(field_name: str, value: Any) -> str:
    """Coerce an instant to a UTC ISO 8601 string.

    The ledger stores UTC and nothing else. DuckDB's `TIMESTAMP` drops the
    offset when it parses, and its `TIMESTAMPTZ` needs `pytz` in the Python
    client, so normalizing here is what makes the cheap column type lossless.

    A naive value is rejected rather than assumed to be UTC. Carrier arrival
    times are the fields most likely to arrive with a local offset, and a
    silently mis-zoned arrival is exactly the error the thermal record exists
    to make visible.
    """
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"{field_name}: {value!r} is not an ISO 8601 timestamp."
            ) from exc
    else:
        raise TypeError(
            f"{field_name}: expected a datetime or ISO 8601 string, "
            f"got {type(value).__name__}."
        )
    if moment.tzinfo is None:
        raise ValueError(
            f"{field_name}: {value!r} carries no UTC offset. Pass an "
            "offset-aware value; the ledger will not guess a time zone."
        )
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _normalize_date(field_name: str, value: Any) -> str:
    """Coerce a calendar date to a `YYYY-MM-DD` string."""
    if isinstance(value, datetime):
        # `datetime` is a `date` subclass, so this must be checked first.
        # Truncating silently would be wrong: ship date decides carrier
        # eligibility, and a datetime here means the caller has confused an
        # instant with a calendar day.
        raise TypeError(
            f"{field_name} is a calendar date; pass a date, not a datetime."
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValueError(
                f"{field_name}: {value!r} is not an ISO 8601 date."
            ) from exc
    raise TypeError(
        f"{field_name}: expected a date or ISO 8601 string, "
        f"got {type(value).__name__}."
    )


_NORMALIZERS = {"TIMESTAMP": _normalize_timestamp, "DATE": _normalize_date}


def _req(duckdb_type: str) -> Any:
    """A field that must be supplied on every append."""
    return field(metadata={DUCKDB_TYPE: duckdb_type})


def _opt(duckdb_type: str) -> Any:
    """A field that may be filled in by a later append."""
    return field(default=None, metadata={DUCKDB_TYPE: duckdb_type})


@dataclass(kw_only=True)
class LedgerRecord:
    """Base for the three record streams.

    Keyword-only by design. These carry up to twenty fields and positional
    construction at a call site would be unreadable and easy to misorder.
    """

    #: JSONL filename stem, and the derived table name.
    stream: ClassVar[str]
    #: Fields that identify the entity a line updates. Empty means the stream
    #: is an event log and lines are never folded together.
    merge_key: ClassVar[tuple[str, ...]] = ()

    run_id: str = _req("VARCHAR")

    # Assigned by the writer, not by callers.
    seq: int | None = _opt("BIGINT")
    timestamp: str | None = _opt("TIMESTAMP")
    schema_version: int = field(
        default=SCHEMA_VERSION, metadata={DUCKDB_TYPE: "INTEGER"}
    )

    def __post_init__(self) -> None:
        """Canonicalize temporal fields so the JSONL is the normalized form.

        Runs on `from_dict` too, which keeps a read/write round trip stable.
        """
        for name, sql_type in self.columns().items():
            normalize = _NORMALIZERS.get(sql_type)
            if normalize is None:
                continue
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, normalize(name, value))

    @classmethod
    def columns(cls) -> dict[str, str]:
        """Column name to DuckDB type, in declaration order."""
        return {f.name: f.metadata[DUCKDB_TYPE] for f in fields(cls)}

    @classmethod
    def field_map(cls) -> dict[str, Field]:
        return {f.name: f for f in fields(cls)}

    def to_dict(self) -> dict[str, Any]:
        """Serializable form, with nulls dropped.

        Dropping nulls is not just compaction. Under the fold, an absent key
        and a null key mean the same thing -- "this append says nothing about
        that field" -- so omitting them makes each line an honest record of
        what the writer actually knew.
        """
        return {
            f.name: value
            for f in fields(self)
            if (value := getattr(self, f.name)) is not None
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerRecord:
        known = cls.field_map()
        unknown = set(data) - set(known)
        if unknown:
            raise ValueError(
                f"{cls.__name__} has no field(s) {sorted(unknown)}. "
                "A line written by a newer schema cannot be read by this one."
            )
        return cls(**data)


@dataclass(kw_only=True)
class RunRecord(LedgerRecord):
    """One run. Opened at A1, closed once the run reaches a terminal state."""

    stream: ClassVar[str] = "runs"
    merge_key: ClassVar[tuple[str, ...]] = ("run_id",)

    profile: str | None = _opt("VARCHAR")
    # Section 2 requires the resolved capability set on every run, and section
    # 6.9's planner prerequisite needs to count prior shadow runs -- neither is
    # answerable from `profile` alone, since profile definitions change over
    # time while the ledger does not.
    cap_fingerprint: str | None = _opt("VARCHAR")
    cap_snapshot: dict[str, Any] | None = _opt("JSON")
    packet_count: int | None = _opt("INTEGER")
    carrier_pair: list[str] | None = _opt("VARCHAR[]")
    total_cost: float | None = _opt("DOUBLE")
    suppressed_count: int | None = _opt("INTEGER")
    escalated_count: int | None = _opt("INTEGER")
    stranded_count: int | None = _opt("INTEGER")
    # The parsed capability overrides LaunchDarkly proposed, before the clamp
    # and the prerequisites ran. Stored rather than hashed: a hash cannot be
    # inverted, so "what did LD ask for on the run that behaved oddly" was
    # unanswerable from a digest. These are enum-validated values, not the raw
    # payload -- see the secrets note in CLAUDE.md.
    flag_payload: dict[str, Any] | None = _opt("JSON")
    evaluation_reasons: dict[str, Any] | None = _opt("JSON")
    started_at: str | None = _opt("TIMESTAMP")
    completed_at: str | None = _opt("TIMESTAMP")


@dataclass(kw_only=True)
class ShipmentRecord(LedgerRecord):
    """One shipment within a run. `actual_arrival` arrives via E3 backfill."""

    stream: ClassVar[str] = "shipments"
    merge_key: ClassVar[tuple[str, ...]] = ("run_id", "recipient_key")

    recipient_key: str = _req("VARCHAR")
    name: str | None = _opt("VARCHAR")
    validated_address: dict[str, Any] | None = _opt("JSON")
    box_size: str | None = _opt("VARCHAR")
    gel_pack_count: int | None = _opt("INTEGER")
    ship_date: str | None = _opt("DATE")
    carrier: str | None = _opt("VARCHAR")
    service: str | None = _opt("VARCHAR")
    cost: float | None = _opt("DOUBLE")
    expected_arrival: str | None = _opt("TIMESTAMP")
    actual_arrival: str | None = _opt("TIMESTAMP")
    predicted_arrival_temp: float | None = _opt("DOUBLE")
    thermal_margin: float | None = _opt("DOUBLE")
    tracking_number: str | None = _opt("VARCHAR")
    idempotency_key: str | None = _opt("VARCHAR")
    # A pointer to the run row, not a copy of it. ~22 shipments per run would
    # otherwise each carry an identical `cap_snapshot` blob into a committed
    # append-only file, which is what the fingerprint exists to avoid.
    #
    # This assumes a shipment's capabilities are the run's. True today: A1
    # resolves once and nothing re-evaluates per shipment. If design 6.6's
    # `shipment` context kind is ever used to vary a flag per recipient, a
    # fingerprint here could name a set no run row describes, and the fix is a
    # `capability_sets` stream keyed by fingerprint -- deliberately not built
    # for a case that does not exist yet.
    cap_fingerprint: str | None = _opt("VARCHAR")


@dataclass(kw_only=True)
class AgentInvocationRecord(LedgerRecord):
    """One invocation of one agent. Never folded -- see the module docstring.

    Three fields identify the text that produced a behavior, and they are not
    redundant with each other (design section 6.4):

    `instruction_variation_key` and `instruction_version` are LaunchDarkly's
    account of which variation was served. They are the right thing to quote
    back to the console, and they resolve only against LD.

    `instruction_hash` is a fact about the bytes, computable with no network.
    It joins a ledger line to the committed snapshot, and it catches a
    snapshot hand-edited away from the metadata beside it -- a check no
    version number can perform on a repo file.
    """

    stream: ClassVar[str] = "agent_invocations"
    merge_key: ClassVar[tuple[str, ...]] = ()

    agent_key: str = _req("VARCHAR")
    shipment_key: str | None = _opt("VARCHAR")
    instruction_variation_key: str | None = _opt("VARCHAR")
    instruction_version: int | None = _opt("INTEGER")
    instruction_hash: str | None = _opt("VARCHAR")
    model: str | None = _opt("VARCHAR")
    iterations: int | None = _opt("INTEGER")
    outcome: str | None = _opt("VARCHAR")


#: Every stream the ledger knows how to write and rebuild.
RECORD_TYPES: tuple[type[LedgerRecord], ...] = (
    RunRecord,
    ShipmentRecord,
    AgentInvocationRecord,
)

STREAMS: dict[str, type[LedgerRecord]] = {r.stream: r for r in RECORD_TYPES}
