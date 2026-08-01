"""The deterministic planning spine. Design section 4, Phase C.

Build order step 3: C1 through C6 with zero model calls. Every decision here
is ordinary Python, which is design 2's central claim -- "a language model is
not in the path of any decision that can be computed."

* C1 `load`           -- packet contents to mass and dimensions
* C2 `configurations` -- parcel variants crossed with quoted services
* C3 `thermal`        -- the 4.4C gate, plus the seam step 4 replaces
* C5 `solve`          -- every legal carrier subset, ranked

`rates` is not a pipeline stage but the seam the whole spine rests on: which
carriers and services exist, what they cost, and how long they take are
answers from a live API rather than constants anyone chose. C6 remains.
"""

from .catalog import (
    BOXES,
    GEL_PACK_LATENT_HEAT_J_KG,
    GEL_PACK_MASS_KG,
    MAX_CARRIERS_PER_RUN,
    MAX_GEL_PACKS,
    Box,
    BoxSize,
    ShipDay,
    ShipDayError,
    ship_day_for,
)
from .configurations import (
    Configuration,
    Enumeration,
    enumerate_configurations,
    heaviest_variant,
    parcel_variants,
)
from .load import Load, define_load
from .rates import (
    SATURDAY_CARRIERS,
    Address,
    CarrierMessage,
    ParcelSpec,
    Quote,
    QuoteResult,
    QuotingUnavailable,
    RateQuoter,
    RecordedQuoter,
    ShippoQuoter,
    pin_carriers,
)
from .shipment import Shipment
from .solve import (
    Assignment,
    CarrierPlan,
    Solve,
    carrier_subsets,
    shipment_options,
    solve_carriers,
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
    ungateable,
)

__all__ = [
    "BOXES",
    "DEFAULT_LANE",
    "GEL_PACK_LATENT_HEAT_J_KG",
    "GEL_PACK_MASS_KG",
    "MAX_ARRIVAL_TEMP_C",
    "MAX_CARRIERS_PER_RUN",
    "MAX_GEL_PACKS",
    "SATURDAY_CARRIERS",
    "Address",
    "Assignment",
    "Box",
    "BoxSize",
    "CarrierMessage",
    "CarrierPlan",
    "Configuration",
    "Enumeration",
    "EvaluatedConfiguration",
    "Lane",
    "Load",
    "LumpedCapacitanceModel",
    "ParcelSpec",
    "Quote",
    "QuoteResult",
    "QuotingUnavailable",
    "RateQuoter",
    "RecordedQuoter",
    "ShipDay",
    "ShipDayError",
    "Shipment",
    "ShippoQuoter",
    "Solve",
    "ThermalModel",
    "carrier_subsets",
    "define_load",
    "enumerate_configurations",
    "evaluate_configurations",
    "heaviest_variant",
    "parcel_variants",
    "pin_carriers",
    "ship_day_for",
    "shipment_options",
    "solve_carriers",
    "thermal_gate",
    "ungateable",
]
