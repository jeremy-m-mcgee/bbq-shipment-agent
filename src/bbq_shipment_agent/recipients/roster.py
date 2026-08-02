"""The run input file: who is being shipped to, from where, on which dates.

Build order step 3 takes "a hand-written recipient list as input", and this is
the parser for it. Deliberately a file rather than CLI arguments: a run is ~22
recipients with an optional lane and an optional ship-date pin each, which is
not something anyone types at a prompt, and a file can be diffed between runs.

## Not committed

The real roster holds names and home addresses. `recipients.example.yaml` is
the committed template with fake entries, the way `.env.example` is; the file
you actually run against is gitignored. Nothing here writes the roster
anywhere -- only the chosen plan reaches the ledger, and that happens at E2.

## Why the parser is strict

Three failure modes are worth an error rather than a default:

* **An unquoted ZIP.** YAML 1.1 reads `78701` as an integer and `02134` as
  *octal*, which silently becomes 1116. A ZIP that parsed as a number is
  refused with the fix in the message. Same class of trap as `off`/`on` in
  `capabilities.yaml`.
* **A duplicate recipient key.** B4 is deferred (design 11, step 12), so
  nothing downstream consolidates. A repeated key in the file is a typo the
  operator can see, and finding out here beats finding out from a manifest
  with two rows for one person.
* **A missing origin.** Every cost and every transit estimate is measured
  from it. Guessing a default would make the whole manifest quietly wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..planning import DEFAULT_LANE, Address, Lane, Shipment, ship_day_for

DEFAULT_ROSTER_PATH = Path("recipients.yaml")

_SLUG = re.compile(r"[^a-z0-9]+")

#: Design 3's ship days, as `date.weekday()` values.
_SHIP_WEEKDAYS = (5, 0, 1)  # Saturday, Monday, Tuesday


class RosterError(ValueError):
    """The run input file cannot be read as written."""


@dataclass(frozen=True)
class Roster:
    """One run's input: the origin, the recipients, the candidate dates."""

    origin: Address
    shipments: tuple[Shipment, ...]
    ship_dates: tuple[date, ...]
    source: Path | None = None

    @property
    def packet_count(self) -> int:
        return len(self.shipments)


def default_ship_dates(after: date) -> tuple[date, ...]:
    """The next Saturday, Monday and Tuesday strictly after `after`.

    A convenience, and the one time-dependent thing in the spine. Put explicit
    `ship_dates` in the roster for a run you want to be able to reproduce
    later; the CLI prints whichever set it used either way.
    """
    dates = []
    for weekday in _SHIP_WEEKDAYS:
        ahead = (weekday - after.weekday()) % 7 or 7
        dates.append(after + timedelta(days=ahead))
    return tuple(sorted(dates))


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise RosterError(f"cannot derive a recipient key from {name!r}; set `key`.")
    return slug


def load_roster(path: Path | str, *, today: date | None = None) -> Roster:
    """Parse the run input file. See the module docstring for the strictness."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RosterError(f"{path}: {exc}") from None
    except yaml.YAMLError as exc:
        raise RosterError(f"{path}: not valid YAML. {exc}") from None

    if not isinstance(raw, dict):
        raise RosterError(
            f"{path}: expected a mapping with `origin` and `recipients` keys. "
            "See recipients.example.yaml."
        )

    origin = _address(raw.get("origin"), f"{path}: origin", default_name="Origin")
    lanes = _lanes(raw.get("lanes") or {}, path)
    entries = raw.get("recipients")
    if not isinstance(entries, list) or not entries:
        raise RosterError(f"{path}: `recipients` must be a non-empty list.")

    shipments: list[Shipment] = []
    seen: dict[str, str] = {}
    for index, entry in enumerate(entries, start=1):
        shipment = _shipment(entry, f"{path}: recipients[{index}]", lanes)
        if shipment.recipient_key in seen:
            raise RosterError(
                f"{path}: recipient key {shipment.recipient_key!r} appears twice "
                f"({seen[shipment.recipient_key]} and {shipment.name}). "
                "Deduplication is deferred (design 11, step 12), so fix the file "
                "or give one of them an explicit `key`."
            )
        seen[shipment.recipient_key] = shipment.name
        shipments.append(shipment)

    declared = raw.get("ship_dates")
    if declared is None:
        ship_dates = default_ship_dates(today or date.today())
    else:
        if not isinstance(declared, list) or not declared:
            raise RosterError(f"{path}: `ship_dates`, if present, must be a list.")
        ship_dates = tuple(
            sorted(_date(value, f"{path}: ship_dates") for value in declared)
        )

    # Validated here rather than at C2 so an unshippable date is an error about
    # the file the operator just wrote, naming the day of the week.
    for when in ship_dates:
        ship_day_for(when)
    for shipment in shipments:
        if shipment.required_ship_date is not None:
            ship_day_for(shipment.required_ship_date)

    return Roster(
        origin=origin, shipments=tuple(shipments), ship_dates=ship_dates, source=path
    )


def _lanes(raw: Any, path: Path) -> dict[str, Lane]:
    if not isinstance(raw, dict):
        raise RosterError(f"{path}: `lanes`, if present, must be a mapping.")
    lanes: dict[str, Lane] = {}
    for key, values in raw.items():
        if not isinstance(values, dict) or "ambient_c" not in values:
            raise RosterError(
                f"{path}: lane {key!r} needs an `ambient_c`. Design 5 makes "
                "ambient a stated assumption, so it has to be stated."
            )
        lanes[str(key)] = Lane(
            key=str(key),
            ambient_c=float(values["ambient_c"]),
            zone=values.get("zone"),
        )
    return lanes


def _shipment(entry: Any, where: str, lanes: dict[str, Lane]) -> Shipment:
    if not isinstance(entry, dict):
        raise RosterError(f"{where}: expected a mapping, got {type(entry).__name__}.")

    name = entry.get("name")
    if not name:
        raise RosterError(f"{where}: `name` is required.")
    name = str(name)

    lane_key = entry.get("lane")
    if lane_key is None:
        lane = DEFAULT_LANE
    elif str(lane_key) in lanes:
        lane = lanes[str(lane_key)]
    else:
        raise RosterError(
            f"{where}: lane {lane_key!r} is not defined. Declared lanes: "
            f"{sorted(lanes) or 'none'}."
        )

    pin = entry.get("ship_date")
    return Shipment(
        recipient_key=str(entry["key"]) if entry.get("key") else slugify(name),
        name=name,
        address=_address(entry, where, default_name=name),
        lane=lane,
        required_ship_date=_date(pin, f"{where}.ship_date") if pin else None,
    )


def _address(raw: Any, where: str, *, default_name: str) -> Address:
    if not isinstance(raw, dict):
        raise RosterError(f"{where}: expected a mapping with street1/city/state/zip.")

    missing = [f for f in ("street1", "city", "state") if not raw.get(f)]
    if missing:
        raise RosterError(f"{where}: missing {', '.join(missing)}.")

    zip_code = raw.get("zip")
    if zip_code is None:
        raise RosterError(f"{where}: missing zip.")
    if not isinstance(zip_code, str):
        # See the module docstring: unquoted `02134` is octal in YAML 1.1.
        raise RosterError(
            f"{where}: zip {zip_code!r} parsed as a number. Quote it -- "
            'zip: "02134" -- or YAML reads a leading zero as octal.'
        )

    return Address(
        name=str(raw.get("name") or default_name),
        street1=str(raw["street1"]),
        city=str(raw["city"]),
        state=str(raw["state"]),
        zip=zip_code,
        country=str(raw.get("country") or "US"),
    )


def _date(value: Any, where: str) -> date:
    if isinstance(value, datetime):
        # YAML gives a datetime for `2026-08-15T00:00:00`. Ship dates are
        # calendar days, and a datetime would not compare equal to one.
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise RosterError(f"{where}: {value!r} is not a YYYY-MM-DD date.") from None
