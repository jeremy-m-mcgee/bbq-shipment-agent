
"""C3: the thermal gate, and the seam step 4 replaces. Design sections 4 and 5.

## The threshold is not a model parameter

`MAX_ARRIVAL_TEMP_C` is a permission-level constraint about food safety. It
does not move when the model is recalibrated, and design 3 puts it beyond the
reach of flags, CLI arguments, and environment variables. It lives here as a
module constant so there is exactly one place it could be changed and that
place is a commit.

The distinction matters more than it looks: everything else in this module is
an estimate that step 4 improves. The threshold is the one number that is not.

## What the model is today

Two-phase lumped capacitance. While any solid gel remains, the contents
approach the gel's melting point; once the latent budget is spent, they warm
toward ambient with a time constant set by the load's heat capacity over the
box's UA.

That structure is design 5's, and it produces the behaviour design 5 insists
on: hold time falls as surface area rises, so the larger box is strictly worse
at this product weight, with no compensating ballast because the mass is not
there.

The constants are nominal. Step 4 slots a calibrated model in behind
`ThermalModel` without touching this gate, and design 5 notes the calibration
path depends on E3 accumulating real transit data.

**Known artefact of the placeholder, and it is worse than "approximate":** the
two-phase form gives a cliff, not a gradient. The load's time constant is
about 1.5 hours against transit times measured in days, so the contents reach
the gel's phase temperature long before hold time runs out. Every feasible
configuration therefore arrives at essentially 0C, and their margins differ by
a few microkelvin -- numerically distinct, operationally identical. The
crossing to infeasible then happens inside about twenty minutes at the end of
hold.

Two consequences worth knowing before step 4:

* D1's thin-margin check has nothing to discriminate on -- no feasible
  configuration is ever thinner than any other.
* C5's "minimum thermal margin across the run" is a constant, so it carries no
  information when comparing carrier pairs.

Neither is a pipeline defect. Both fields are computed, recorded, and
correctly plumbed; they simply have no variance until a calibrated model
supplies one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from .catalog import (
    GEL_PACK_LATENT_HEAT_J_KG,
    GEL_PACK_MASS_KG,
    GEL_PACK_PHASE_TEMP_C,
)
from .configurations import Configuration
from .load import Load

#: Design 3, hard constraint: arrival at or below 4.4C, for food safety.
#: A permission-level constraint, not a model parameter. Never a flag, a CLI
#: argument, or an environment variable.
MAX_ARRIVAL_TEMP_C = 4.4

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class Lane:
    """Design 5's lane-based ambient assumption.

    Static per lane today. Design 10 records seasonal adjustment as an open
    question with no calibration data behind it yet, so this stays a stated
    assumption rather than a computed value.
    """

    key: str
    ambient_c: float
    zone: int | None = None


#: Used when a shipment has no lane assigned. Deliberately a named constant
#: rather than a default argument: an unlabelled ambient temperature appearing
#: in a thermal record is exactly the kind of silent assumption design 5 asks
#: to have stated.
DEFAULT_LANE = Lane(key="default", ambient_c=22.0)


class ThermalModel(Protocol):
    """Predicts arrival temperature. The swap point design 5 promises.

    Narrow on purpose: it answers one question and has no view on whether the
    answer is acceptable. The gate owns the threshold, so a recalibrated model
    can never quietly change what counts as safe.
    """

    def predict_arrival_temp_c(
        self, load: Load, configuration: Configuration, lane: Lane
    ) -> float: ...


class LumpedCapacitanceModel:
    """Design 5's approach with nominal constants. Replaced in step 4."""

    def predict_arrival_temp_c(
        self, load: Load, configuration: Configuration, lane: Lane
    ) -> float:
        ua = configuration.box.ua_w_k
        transit_s = configuration.transit_days * SECONDS_PER_DAY

        # Time constant of the load alone. The product is minimal ballast at
        # 1.5 lb, so this is short and the gate is sharp once gel is gone.
        tau_s = load.heat_capacity_j_k / ua

        # Latent budget, and how fast it is spent. Heat leaks in against the
        # gel's melting point for as long as any solid fraction remains.
        latent_budget_j = (
            configuration.gel_packs * GEL_PACK_MASS_KG * GEL_PACK_LATENT_HEAT_J_KG
        )
        leak_w = ua * (lane.ambient_c - GEL_PACK_PHASE_TEMP_C)
        if leak_w <= 0:
            # Ambient at or below the gel's phase temperature: nothing drives
            # heat inward, so the contents never rise past it.
            return min(load.initial_temp_c, GEL_PACK_PHASE_TEMP_C)
        hold_s = latent_budget_j / leak_w

        if transit_s <= hold_s:
            # Still refrigerated on arrival: approaching the gel temperature
            # from below, never past it.
            return GEL_PACK_PHASE_TEMP_C + (
                load.initial_temp_c - GEL_PACK_PHASE_TEMP_C
            ) * math.exp(-transit_s / tau_s)

        temp_at_exhaustion_c = GEL_PACK_PHASE_TEMP_C + (
            load.initial_temp_c - GEL_PACK_PHASE_TEMP_C
        ) * math.exp(-hold_s / tau_s)
        return lane.ambient_c - (lane.ambient_c - temp_at_exhaustion_c) * math.exp(
            -(transit_s - hold_s) / tau_s
        )


@dataclass(frozen=True)
class EvaluatedConfiguration:
    """One configuration with its predicted arrival temperature.

    `thermal_margin_c` is headroom below the threshold: positive is feasible,
    and larger is safer. Recorded on every shipment row so a run that arrived
    warm can be told apart from one that was never going to make it.
    """

    configuration: Configuration
    predicted_arrival_temp_c: float
    lane: Lane

    @property
    def thermal_margin_c(self) -> float:
        return MAX_ARRIVAL_TEMP_C - self.predicted_arrival_temp_c

    @property
    def feasible(self) -> bool:
        """At or below the threshold. `<=` because 4.4C itself is allowed."""
        return self.predicted_arrival_temp_c <= MAX_ARRIVAL_TEMP_C


def evaluate_configurations(
    load: Load,
    configurations: tuple[Configuration, ...],
    lane: Lane = DEFAULT_LANE,
    model: ThermalModel | None = None,
) -> tuple[EvaluatedConfiguration, ...]:
    """Predict arrival temperature for every configuration, gating nothing.

    Kept separate from the gate so C4 can see the near misses. A shipment that
    C3 left with nothing feasible needs to know *how far* short its best
    option fell -- "two hours of hold time" and "a full day" call for
    different remediation, and a filtered list cannot tell them apart.
    """
    thermal_model = model or LumpedCapacitanceModel()
    return tuple(
        EvaluatedConfiguration(
            configuration=configuration,
            predicted_arrival_temp_c=thermal_model.predict_arrival_temp_c(
                load, configuration, lane
            ),
            lane=lane,
        )
        for configuration in configurations
    )


def thermal_gate(
    load: Load,
    configurations: tuple[Configuration, ...],
    lane: Lane = DEFAULT_LANE,
    model: ThermalModel | None = None,
) -> tuple[EvaluatedConfiguration, ...]:
    """C3. Filter to configurations arriving at or below the threshold.

    A hard gate, and occasionally empty -- design 4 expects that and routes it
    to C4 rather than treating it as an error. Returning an empty tuple is the
    honest answer; raising here would make an ordinary planning outcome look
    like a failure.
    """
    return tuple(
        evaluated
        for evaluated in evaluate_configurations(load, configurations, lane, model)
        if evaluated.feasible
    )
