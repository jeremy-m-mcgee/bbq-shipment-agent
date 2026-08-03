"""Ambient temperature per destination. Design section 5, and section 10.

Design 5 lists ambient as a *lane-based assumption*, "Assumed, stated". Until
now it was one number — `DEFAULT_LANE` at 22C — standing in for every
destination in the country, which section 10 identified as the thing actually
collapsing the thermal envelope. At 22C the gate tops out at two elapsed days
however many gel packs go in, so every shipment without an explicit lane was
pushed onto overnight and 2-day service regardless of where it was going.

## Every number in this file is an assumption, and none of it is measured

That is not a disclaimer, it is the design. Section 5 gave up on calibration
when E3 was removed, so there is no fitted value to reach for and none of these
bands came from a dataset. What they are is the operator's stated belief about
their own lanes, in a committed file, so that a surprising plan can be traced
to the assumption that produced it and the assumption can be argued with.

The bands are coarse on purpose. A model with no calibration path does not earn
two-decimal ambients, and a file that looked precise would invite exactly the
false confidence the thermal record exists to prevent.

## Destination climate is a proxy for the lane, not the lane

A parcel from San Francisco to Washington sits in trucks, warehouses and on a
doorstep, and the ambient that matters is a blend along the whole route. This
models it from the destination state, which is a proxy. It is a better proxy
than one national number and a worse one than instrumentation, and it is worth
knowing which of those you have.

## An unmapped destination gets the most conservative band

Deliberately, and it is the one place this file is *not* neutral. An unstated
assumption should not be the optimistic one when a food safety gate depends on
it — the same reasoning that leaves Saturday delivery unmodelled because
erring long is the safe direction for a gate. Marking a lane cool is how an
operator earns a cheaper plan, rather than getting one by default from a
number nobody chose.

Consequence worth stating plainly: filling nothing in makes plans *more*
expensive than the old 22C default did, not less.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .thermal import Lane

DEFAULT_LANES_PATH = Path("config/lanes.yaml")


class LaneBookError(ValueError):
    """The lane configuration cannot be read as written."""


@dataclass(frozen=True)
class LaneBook:
    """The committed ambient assumptions, and how to apply them."""

    #: Band name -> ambient in C, before any seasonal offset.
    bands: dict[str, float]
    #: Two-letter state code -> band name. Partial by design; anything absent
    #: takes `default_band`.
    states: dict[str, str] = field(default_factory=dict)
    #: The band an unmapped destination gets. See the module docstring: this
    #: should be the hottest band, not the average one.
    default_band: str = ""
    #: Month number -> degrees added to the band. Optional, and the other half
    #: of design 10's ambient question.
    seasonal_offset_c: dict[int, float] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_LANES_PATH) -> LaneBook:
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise LaneBookError(f"{path}: {exc}") from None
        except yaml.YAMLError as exc:
            raise LaneBookError(f"{path}: not valid YAML. {exc}") from None
        return cls.from_mapping(raw, source=str(path))

    @classmethod
    def from_mapping(cls, raw: Any, *, source: str = "lanes") -> LaneBook:
        if not isinstance(raw, dict):
            raise LaneBookError(f"{source}: expected a mapping.")

        bands_raw = raw.get("bands")
        if not isinstance(bands_raw, dict) or not bands_raw:
            raise LaneBookError(f"{source}: `bands` must be a non-empty mapping.")
        bands = {}
        for name, value in bands_raw.items():
            try:
                bands[str(name)] = float(value)
            except (TypeError, ValueError):
                raise LaneBookError(
                    f"{source}: band {name!r} must be a number of degrees C, "
                    f"got {value!r}."
                ) from None

        default_band = str(raw.get("default_band") or "")
        if default_band not in bands:
            raise LaneBookError(
                f"{source}: default_band {default_band!r} is not one of "
                f"{sorted(bands)}. An unmapped destination has to land "
                "somewhere, and it should be the most conservative band."
            )

        states = {}
        for code, band in (raw.get("states") or {}).items():
            band = str(band)
            if band not in bands:
                raise LaneBookError(
                    f"{source}: state {code!r} maps to unknown band {band!r}. "
                    f"Known: {sorted(bands)}."
                )
            states[str(code).upper()] = band

        seasonal = {}
        for month, offset in (raw.get("seasonal_offset_c") or {}).items():
            try:
                month_number = int(month)
                seasonal[month_number] = float(offset)
            except (TypeError, ValueError):
                raise LaneBookError(
                    f"{source}: seasonal_offset_c key {month!r} must be a month "
                    "number 1-12 mapping to a number of degrees."
                ) from None
            if not 1 <= month_number <= 12:
                raise LaneBookError(
                    f"{source}: seasonal_offset_c month {month_number} is not 1-12."
                )

        return cls(
            bands=bands,
            states=states,
            default_band=default_band,
            seasonal_offset_c=seasonal,
        )

    def band_for(self, state: str) -> str:
        return self.states.get((state or "").upper(), self.default_band)

    def lane_for(self, state: str, when: date | None = None) -> Lane:
        """The ambient assumption for one destination, on one ship date.

        `when` picks the seasonal offset. Candidate ship dates within a run are
        a Saturday, Monday and Tuesday of one week, so they share a month
        except across a boundary, where the difference is one month's offset on
        one shipment — immaterial against bands this coarse, and not worth
        making `Shipment.lane` vary per configuration to capture.
        """
        band = self.band_for(state)
        ambient = self.bands[band]
        offset = self.seasonal_offset_c.get(when.month, 0.0) if when else 0.0

        code = (state or "??").upper()
        mapped = "" if code in self.states else "-unmapped"
        season = f"+{when:%b}" if when and offset else ""
        return Lane(
            # The key travels onto the manifest and into the ledger, so it
            # says which assumption produced the number rather than only what
            # the number was.
            key=f"{code}:{band}{mapped}{season}",
            ambient_c=ambient + offset,
        )
