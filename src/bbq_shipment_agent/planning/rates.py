"""Quoting: what services exist for a parcel on a lane, and what they cost.

This module replaced a static table, and the reason is worth recording because
it changes C2's shape.

The first version declared four carriers and nine services as constants, with
transit times I chose. Against the real Shippo API that was wrong in kind, not
just in value: DHL Express will not quote US domestic at all, UPS refused the
origin as out of its service area, FedEx had no account, and USPS returned a
service ("Ground Advantage") that was not in the enum. Which carriers and
services exist is a property of the account *and* the specific lane, resolved
at runtime. A static catalog cannot represent that.

So the enumeration inverts. C2 no longer takes a cross product against a
declared service list; it builds parcel variants and asks what can carry them.
Price and transit estimate arrive in the same answer, which means C2 and C5's
pricing step collapse into one call -- a divergence from design 4's ordering,
forced by the fact that you cannot learn which services exist without also
being told what they cost.

## The carrier set is pinned per shipment

Quoting the same lane with different parcels returned UPS for some and not
others. That was rate limiting on Shippo's shared master account, but nothing
in the data distinguishes it from a real service restriction, so the
enumeration would have concluded that UPS can carry three gel packs and not
six -- and C5 would have reported a stranding that does not exist.

The fix is to establish a shipment's carrier set once, from a reference
parcel, and then require that set for every other parcel on the same lane,
retrying transient failures until it arrives. Per shipment rather than per
run, because carriers genuinely do vary by destination: a run-wide pin would
force a carrier onto a lane it does not serve.

## Quotes are treated as date-independent

A parcel is quoted once and the result reused across every candidate ship
date. Shippo does accept a `shipment_date` that can affect estimates, so this
is an approximation, taken because the alternative multiplies an already large
call count by the number of candidate dates. Stated here rather than hidden.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .catalog import GEL_PACK_MASS_KG, BoxSize
from .load import Load

#: Carrier tokens are whatever the quoting service reports (Shippo's
#: `provider`). Not an enum: the set is discovered, not declared.
Carrier = str

#: Design 3: Saturday is USPS only for perishables, given weekend ground
#: schedules. Matched against the reported carrier token.
SATURDAY_CARRIERS: frozenset[str] = frozenset({"USPS"})

#: Substrings that mark a carrier failure as worth retrying. Deliberately a
#: small allow-list: anything not matched is treated as a real answer, because
#: retrying a permanent rejection ("out of service area") just burns time and
#: then reports the same thing.
TRANSIENT_MARKERS: tuple[str, ...] = (
    "too many requests",
    "rate limit",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "try again",
    "service unavailable",
)

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_S = 2.0


class QuotingUnavailable(Exception):
    """Quoting failed in a way that must stop the run.

    Raised rather than degraded, deliberately, and unlike the LaunchDarkly
    path where unreachable is a normal path. A missing instruction set costs a
    weaker proposal that a human reviews anyway; a missing rate costs a
    manifest full of invented money that looks exactly like a real one.
    """


@dataclass(frozen=True)
class Address:
    """A postal address, in the shape the quoting service wants."""

    name: str
    street1: str
    city: str
    state: str
    zip: str
    country: str = "US"

    def cache_key(self) -> str:
        return f"{self.street1}|{self.city}|{self.state}|{self.zip}|{self.country}".lower()


@dataclass(frozen=True)
class ParcelSpec:
    """One physical parcel variant: a box size plus a gel pack count.

    Box size sets the dimensions, gel pack count sets the weight. Those are
    the only two things about a configuration a carrier prices on, which is
    why the parcel is the unit of quoting rather than the configuration.
    """

    box_size: BoxSize
    gel_packs: int
    length_cm: float
    width_cm: float
    height_cm: float
    weight_kg: float

    @classmethod
    def build(cls, load: Load, box, gel_packs: int) -> ParcelSpec:
        length, width, height = (round(d * 100, 2) for d in box.outer_m)
        return cls(
            box_size=box.size,
            gel_packs=gel_packs,
            length_cm=length,
            width_cm=width,
            height_cm=height,
            weight_kg=round(
                load.mass_kg + gel_packs * GEL_PACK_MASS_KG + box.tare_kg, 3
            ),
        )

    def cache_key(self) -> str:
        return f"{self.length_cm}x{self.width_cm}x{self.height_cm}@{self.weight_kg}"


@dataclass(frozen=True)
class Quote:
    """One priced service for one parcel on one lane.

    `estimated_days` is the transit estimate the thermal gate runs on. Design
    5 lists transit duration as the one externally-supplied planning input,
    and this is where it enters -- previously it was a constant I chose, which
    was optimistic by a day or two on every service.
    """

    carrier: Carrier
    service_token: str
    service_name: str
    amount: float
    currency: str
    estimated_days: int | None
    parcel: ParcelSpec
    #: Carrier's own words about the estimate, e.g. "Delivery by the end of
    #: the second business day". The only signal distinguishing a business-day
    #: quote from a calendar-day one.
    duration_terms: str = ""

    @property
    def business_days(self) -> bool:
        """Whether `estimated_days` counts business days rather than calendar.

        Inferred from the carrier's prose because there is no structured
        field for it. Defaults to calendar when the carrier says nothing,
        which matches USPS and errs toward the shorter transit -- so a missing
        `duration_terms` on a business-day service would understate transit.
        Worth revisiting if a carrier appears that quotes business days
        silently.
        """
        return "business" in self.duration_terms.lower()

    @property
    def key(self) -> str:
        return f"{self.carrier.lower()}:{self.service_token}"

    @property
    def has_transit_estimate(self) -> bool:
        """Whether this quote can be thermally gated at all.

        A quote with no estimate cannot be assessed for arrival temperature,
        and guessing one would reintroduce exactly the fabrication this module
        exists to remove. C3 drops these and says so.
        """
        return self.estimated_days is not None


@dataclass(frozen=True)
class CarrierMessage:
    """Why a carrier did not quote.

    Recorded rather than discarded. Without these, a carrier silently missing
    from a run is indistinguishable from a carrier that has no service on the
    lane -- which is exactly how a rate limiter passed for a service
    restriction until it was looked for.
    """

    source: str
    code: str
    text: str

    @property
    def transient(self) -> bool:
        haystack = f"{self.code} {self.text}".lower()
        return any(marker in haystack for marker in TRANSIENT_MARKERS)

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "code": self.code, "text": self.text}


@dataclass(frozen=True)
class QuoteResult:
    """Everything one quoting call returned, including what it refused."""

    quotes: tuple[Quote, ...]
    messages: tuple[CarrierMessage, ...] = ()
    attempts: int = 1

    @property
    def carriers(self) -> frozenset[str]:
        return frozenset(q.carrier for q in self.quotes)

    @property
    def transient_failures(self) -> tuple[CarrierMessage, ...]:
        return tuple(m for m in self.messages if m.transient)

    def missing(self, required: frozenset[str]) -> frozenset[str]:
        return required - self.carriers


class RateQuoter(Protocol):
    """Returns every service that will carry this parcel on this lane.

    `require` is the pinned carrier set. An implementation should retry
    transient failures until every required carrier has answered, and raise
    `QuotingUnavailable` if it cannot -- planning against a carrier set that
    happened to be short is worse than not planning.
    """

    def quote(
        self,
        origin: Address,
        destination: Address,
        parcel: ParcelSpec,
        require: frozenset[str] = frozenset(),
    ) -> QuoteResult: ...


class ShippoQuoter:
    """Live quotes from Shippo, with retry and an on-disk cache.

    A run asks for every box size against every gel pack count for every
    recipient, so an uncached run is hundreds of calls and UPS answers "Too
    Many Requests" on Shippo's shared master account. The cache makes
    re-running a plan during development free rather than rate-limited, and
    the retry turns the remaining flakiness into latency, which at three to
    five runs a year costs nothing.
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache_path: Path | str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_s: float = DEFAULT_BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("SHIPPO_API_KEY", "")
        if not key:
            raise QuotingUnavailable(
                "SHIPPO_API_KEY is unset or empty. Quoting is not optional: "
                "without it every cost on the manifest would be invented."
            )
        self._key = key
        self._client: Any = None
        self._max_attempts = max_attempts
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, dict[str, Any]] = {}
        if self._cache_path and self._cache_path.exists():
            try:
                self._cache = json.loads(self._cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._cache = {}

    def _sdk(self) -> Any:
        # Imported lazily so an offline path never pays for the SDK.
        if self._client is None:
            from shippo import Shippo

            self._client = Shippo(api_key_header=self._key)
        return self._client

    def quote(
        self,
        origin: Address,
        destination: Address,
        parcel: ParcelSpec,
        require: frozenset[str] = frozenset(),
    ) -> QuoteResult:
        cache_key = f"{origin.cache_key()}>{destination.cache_key()}#{parcel.cache_key()}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            result = _result_from_dict(cached, parcel)
            # A cached answer that already satisfies the pin is reusable. One
            # that does not was cached mid-rate-limit and has to be refetched,
            # or the flakiness simply becomes permanent on disk.
            if not result.missing(require):
                return result

        best: QuoteResult | None = None
        attempts_made = 0
        for attempt in range(1, self._max_attempts + 1):
            attempts_made = attempt
            result = self._fetch(origin, destination, parcel, attempt)
            if best is None or len(result.quotes) > len(best.quotes):
                best = result
            if not result.missing(require) and not result.transient_failures:
                break
            if attempt < self._max_attempts:
                self._sleep(self._backoff_s * attempt)

        assert best is not None
        # `attempts` means how many calls it took to get here, not which call
        # happened to be best. The distinction matters in the failure message:
        # "after 1 attempts" when four were made reads as a retry that never ran.
        best = QuoteResult(quotes=best.quotes, messages=best.messages, attempts=attempts_made)
        missing = best.missing(require)
        if missing:
            reasons = "; ".join(
                f"{m.source}: {m.text[:80]}" for m in best.messages if m.source in missing
            )
            raise QuotingUnavailable(
                f"{sorted(missing)} did not quote {parcel.cache_key()} to "
                f"{destination.city} after {best.attempts} attempts. "
                f"{reasons or 'No carrier message explains why.'} "
                "Planning against a short carrier set would report strandings "
                "that are artefacts of the quoting call."
            )
        if not best.quotes:
            raise QuotingUnavailable(
                f"no carrier quoted {parcel.cache_key()} to {destination.city}."
            )

        self._cache[cache_key] = _result_to_dict(best)
        self._flush()
        return best

    def _fetch(
        self, origin: Address, destination: Address, parcel: ParcelSpec, attempt: int
    ) -> QuoteResult:
        from shippo.models import components as c

        def address(a: Address) -> Any:
            return c.AddressCreateRequest(
                name=a.name, street1=a.street1, city=a.city,
                state=a.state, zip=a.zip, country=a.country,
            )

        shipment = self._sdk().shipments.create(
            c.ShipmentCreateRequest(
                address_from=address(origin),
                address_to=address(destination),
                parcels=[
                    c.ParcelCreateRequest(
                        length=str(parcel.length_cm),
                        width=str(parcel.width_cm),
                        height=str(parcel.height_cm),
                        distance_unit=c.DistanceUnitEnum.CM,
                        weight=str(parcel.weight_kg),
                        mass_unit=c.WeightUnitEnum.KG,
                    )
                ],
                async_=False,
            )
        )

        quotes = tuple(
            Quote(
                carrier=r.provider,
                service_token=getattr(r.servicelevel, "token", "") or "",
                service_name=getattr(r.servicelevel, "name", "") or "",
                amount=float(r.amount),
                currency=r.currency,
                estimated_days=r.estimated_days,
                duration_terms=getattr(r, "duration_terms", "") or "",
                parcel=parcel,
            )
            for r in (shipment.rates or [])
        )
        messages = tuple(
            CarrierMessage(
                source=str(getattr(m, "source", "") or ""),
                code=str(getattr(m, "code", "") or ""),
                text=str(getattr(m, "text", "") or ""),
            )
            for m in (shipment.messages or [])
        )
        return QuoteResult(quotes=quotes, messages=messages, attempts=attempt)

    def _flush(self) -> None:
        if not self._cache_path:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8"
        )


class RecordedQuoter:
    """Replays quotes captured from a live run. For tests and offline work.

    Deliberately not a rate *table*: it holds real quotes that were really
    returned, so nothing here is a number anyone made up. A lane it has no
    recording for raises rather than inventing a plausible answer.
    """

    def __init__(self, recording: dict[str, dict[str, Any]]) -> None:
        self._recording = recording

    @classmethod
    def from_file(cls, path: Path | str) -> RecordedQuoter:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def quote(
        self,
        origin: Address,
        destination: Address,
        parcel: ParcelSpec,
        require: frozenset[str] = frozenset(),
    ) -> QuoteResult:
        key = f"{origin.cache_key()}>{destination.cache_key()}#{parcel.cache_key()}"
        if key not in self._recording:
            raise QuotingUnavailable(
                f"no recorded quotes for {key}. Re-record against the live API "
                "rather than substituting an invented rate."
            )
        result = _result_from_dict(self._recording[key], parcel)
        missing = result.missing(require)
        if missing:
            raise QuotingUnavailable(
                f"recording for {key} is missing pinned carriers {sorted(missing)}."
            )
        return result


def pin_carriers(
    quoter: RateQuoter,
    origin: Address,
    destination: Address,
    reference: ParcelSpec,
) -> frozenset[str]:
    """Establish which carriers serve this lane, once.

    Everything else about the shipment is then quoted against this set. The
    reference parcel should be the *heaviest and largest* variant under
    consideration: a carrier that will take the worst case will take the
    others, whereas pinning off the smallest parcel can pin a carrier that
    later refuses a bigger box for a real reason and turn that into a hard
    failure.
    """
    return quoter.quote(origin, destination, reference).carriers


def _result_to_dict(result: QuoteResult) -> dict[str, Any]:
    return {
        "rates": [
            {
                "carrier": q.carrier,
                "service_token": q.service_token,
                "service_name": q.service_name,
                "amount": q.amount,
                "currency": q.currency,
                "estimated_days": q.estimated_days,
                "duration_terms": q.duration_terms,
            }
            for q in result.quotes
        ],
        "messages": [m.to_dict() for m in result.messages],
        "attempts": result.attempts,
    }


def _result_from_dict(data: dict[str, Any], parcel: ParcelSpec) -> QuoteResult:
    return QuoteResult(
        quotes=tuple(
            Quote(
                carrier=row["carrier"],
                service_token=row["service_token"],
                service_name=row["service_name"],
                amount=row["amount"],
                currency=row["currency"],
                estimated_days=row["estimated_days"],
                duration_terms=row.get("duration_terms", ""),
                parcel=parcel,
            )
            for row in data.get("rates", [])
        ),
        messages=tuple(
            CarrierMessage(source=m["source"], code=m["code"], text=m["text"])
            for m in data.get("messages", [])
        ),
        attempts=int(data.get("attempts", 1)),
    )
