"""The deterministic planning spine. Design section 4, Phase C.

Build order step 3: C1 through C6 with zero model calls. Every decision here
is ordinary Python, which is design 2's central claim -- "a language model is
not in the path of any decision that can be computed."

Stages present so far:

* C1 `load`           -- packet contents to mass and dimensions
* C2 `configurations` -- the cross product, all four carriers, no pair limit
* C3 `thermal`        -- the 4.4C gate, plus the seam step 4 replaces

Still to come: C5 (six-pair solve) and C6 (manifest assembly).
"""

from .catalog import (
    BOXES,
    MAX_CARRIERS_PER_RUN,
    MAX_GEL_PACKS,
    SERVICES,
    Box,
    BoxSize,
    Carrier,
    Service,
    ShipDay,
    ShipDayError,
    services_for,
    ship_day_for,
)
from .configurations import Configuration, enumerate_configurations
from .load import Load, define_load
from .rates import (
    STATIC_RATES,
    RateCard,
    ServiceRate,
    StaticRateCard,
    billable_weight_kg,
)
from .shipment import Shipment
from .solve import (
    CARRIER_PAIRS,
    Assignment,
    PairPlan,
    PricedConfiguration,
    Solve,
    shipment_options,
    solve_pairs,
)
from .thermal import (
    DEFAULT_LANE,
    MAX_ARRIVAL_TEMP_C,
    EvaluatedConfiguration,
    Lane,
    LumpedCapacitanceModel,
    ThermalModel,
    evaluate_configurations,
    thermal_gate,
)

__all__ = [
    "BOXES",
    "CARRIER_PAIRS",
    "DEFAULT_LANE",
    "STATIC_RATES",
    "Assignment",
    "PairPlan",
    "PricedConfiguration",
    "RateCard",
    "ServiceRate",
    "Shipment",
    "Solve",
    "StaticRateCard",
    "StaticRateCard",
    "billable_weight_kg",
    "shipment_options",
    "solve_pairs",
    "MAX_ARRIVAL_TEMP_C",
    "MAX_CARRIERS_PER_RUN",
    "MAX_GEL_PACKS",
    "SERVICES",
    "Box",
    "BoxSize",
    "Carrier",
    "Configuration",
    "EvaluatedConfiguration",
    "Lane",
    "Load",
    "LumpedCapacitanceModel",
    "Service",
    "ShipDay",
    "ShipDayError",
    "ThermalModel",
    "define_load",
    "enumerate_configurations",
    "evaluate_configurations",
    "services_for",
    "ship_day_for",
    "thermal_gate",
]
