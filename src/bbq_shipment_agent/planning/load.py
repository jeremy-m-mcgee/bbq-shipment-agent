"""C1: define the load. Design section 4, Phase C.

"Packet contents to weight and dimensions per shipment. Currently uniform."

Uniform today, per-shipment by construction. `define_load` takes a recipient
key it does not yet use, because the moment packet contents vary by recipient
the signature should not have to change -- and a function that already accepts
the key is much harder to accidentally hoist out of the per-shipment loop than
one that takes nothing.

The load is also where design 5's central fact lives: at 1.5 lb the product is
minimal thermal ballast. Gel packs carry roughly 77 percent of the cooling
energy budget, which is what makes a larger box strictly worse rather than a
tradeoff.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import LB_PER_KG, Box

#: Design 1: 1.5 lb of product per packet.
PRODUCT_MASS_LB = 1.5
PRODUCT_MASS_KG = PRODUCT_MASS_LB / LB_PER_KG

#: Packed frozen, straight from the freezer.
PRODUCT_INITIAL_TEMP_C = -18.0

#: Specific heat of frozen packed meat, J/(kg*K). Nominal, and one of the
#: values step 4 revisits; the product contributes so little to the budget
#: that the run is insensitive to it, which is exactly design 5's point.
PRODUCT_SPECIFIC_HEAT_J_KG_K = 2_000.0

#: Bounding box of one packet, metres. Uniform today.
PRODUCT_DIMENSIONS_M = (0.12, 0.10, 0.06)


@dataclass(frozen=True)
class Load:
    """What is physically in one box, before refrigerant is added."""

    mass_kg: float
    initial_temp_c: float
    specific_heat_j_kg_k: float
    dimensions_m: tuple[float, float, float]

    @property
    def heat_capacity_j_k(self) -> float:
        """Thermal ballast. Small enough that gel packs dominate the budget."""
        return self.mass_kg * self.specific_heat_j_kg_k

    def fits_in(self, box: Box) -> bool:
        """Whether the packet physically fits the box cavity.

        Compares sorted dimensions against sorted interior, so a packet that
        fits when rotated counts as fitting. Refrigerant is not modelled as
        occupying space -- `Box.max_gel_packs` is what bounds that, and
        conflating the two would double-count the same constraint.
        """
        return all(
            item <= cavity
            for item, cavity in zip(sorted(self.dimensions_m), sorted(box.inner_m))
        )


def define_load(recipient_key: str) -> Load:
    """C1. The load for one shipment.

    Currently uniform: every recipient gets the same packet, so the key is
    accepted and ignored. See the module docstring for why it is in the
    signature anyway.
    """
    return Load(
        mass_kg=PRODUCT_MASS_KG,
        initial_temp_c=PRODUCT_INITIAL_TEMP_C,
        specific_heat_j_kg_k=PRODUCT_SPECIFIC_HEAT_J_KG_K,
        dimensions_m=PRODUCT_DIMENSIONS_M,
    )
