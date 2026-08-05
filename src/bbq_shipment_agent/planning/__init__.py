"""The deterministic planning spine. Design section 4, Phase C.

C1 through C6, with zero model calls anywhere in the package. Every decision
here is ordinary Python, which is design 2's central claim -- "a language model
is not in the path of any decision that can be computed."

* C1 `load`           -- packet contents to mass and dimensions
* C2 `configurations` -- parcel variants crossed with quoted services
* C3 `thermal`        -- the 4.4C gate and the model behind it
* C4 `remediation`    -- what to do with a shipment nothing can carry
* C5 `solve`          -- every legal carrier subset, ranked
* C6 `manifest`       -- the reviewable work package

C4 is in that list rather than in `agents/` because design 6.3 demoted it: its
one open-ended move turned out to be physically impossible, and what remained
is arithmetic.

Two modules are not stages. `rates` is the seam the whole spine rests on --
which carriers and services exist, what they cost and how long they take are
answers from a live API rather than constants anyone chose. `lanes` reads the
committed ambient assumptions that `thermal` gates against.
"""

from .catalog import (
    BOXES,
    GEL_PACK_LATENT_HEAT_J_KG,
    GEL_PACK_MASS_KG,
    MAX_CARRIERS_PER_RUN,
    MAX_GEL_PACKS,
    MIN_GEL_PACKS,
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
    smallest_fitting_box,
)
from .lanes import DEFAULT_LANES_PATH, LaneBook, LaneBookError
from .load import Load, define_load
from .manifest import (
    Excluded,
    Manifest,
    ManifestRow,
    RunnerUp,
    assemble_manifest,
    render,
)
from .remediation import (
    DEFAULT_HORIZON_MONTHS,
    Remediation,
    RemediationMove,
    remediate,
    remediate_all,
)
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
)

__all__ = [
    "Address",
    "Assignment",
    "BOXES",
    "Box",
    "BoxSize",
    "CarrierMessage",
    "CarrierPlan",
    "Configuration",
    "DEFAULT_HORIZON_MONTHS",
    "DEFAULT_LANE",
    "DEFAULT_LANES_PATH",
    "Enumeration",
    "EvaluatedConfiguration",
    "Excluded",
    "GEL_PACK_LATENT_HEAT_J_KG",
    "GEL_PACK_MASS_KG",
    "Lane",
    "LaneBook",
    "LaneBookError",
    "Load",
    "LumpedCapacitanceModel",
    "MAX_ARRIVAL_TEMP_C",
    "MAX_CARRIERS_PER_RUN",
    "MAX_GEL_PACKS",
    "MIN_GEL_PACKS",
    "Manifest",
    "ManifestRow",
    "ParcelSpec",
    "Quote",
    "QuoteResult",
    "QuotingUnavailable",
    "RateQuoter",
    "RecordedQuoter",
    "Remediation",
    "RemediationMove",
    "RunnerUp",
    "SATURDAY_CARRIERS",
    "ShipDay",
    "ShipDayError",
    "Shipment",
    "ShippoQuoter",
    "Solve",
    "ThermalModel",
    "assemble_manifest",
    "carrier_subsets",
    "define_load",
    "enumerate_configurations",
    "evaluate_configurations",
    "heaviest_variant",
    "parcel_variants",
    "pin_carriers",
    "remediate",
    "remediate_all",
    "render",
    "ship_day_for",
    "shipment_options",
    "smallest_fitting_box",
    "solve_carriers",
    "thermal_gate",
]
