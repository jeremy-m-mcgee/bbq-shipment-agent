"""Phase B: recipient resolution. Design section 4.

B2 (validate) and B4 (dedupe and suppress) live here. Both are deterministic —
design 4 marks them so, and the model-driven repair loop that sits between
them is B3, which arrives at build order step 7.

Both are in the spine, B2 then B4, in that order for the reason in
`dedupe`'s module docstring.

Kept apart from `planning` because the phases answer different questions:
Phase B decides *who* is shipped to, Phase C decides *how*.
"""

from .dedupe import SuppressionReport, dedupe_recipients
from .record import Provenance, Recipient, Region, to_shipments
from .roster import (
    DEFAULT_ROSTER_PATH,
    Roster,
    RosterError,
    default_ship_dates,
    load_roster,
)
from .validation import (
    AddressValidationUnavailable,
    AddressValidator,
    RecordedAddressValidator,
    ShippoAddressValidator,
    ValidationOutcome,
    ValidationReport,
    ValidationResult,
    classify,
    validate_recipients,
)

__all__ = [
    "DEFAULT_ROSTER_PATH",
    "AddressValidationUnavailable",
    "AddressValidator",
    "Provenance",
    "Recipient",
    "RecordedAddressValidator",
    "Region",
    "Roster",
    "RosterError",
    "ShippoAddressValidator",
    "SuppressionReport",
    "ValidationOutcome",
    "ValidationReport",
    "ValidationResult",
    "classify",
    "dedupe_recipients",
    "default_ship_dates",
    "load_roster",
    "to_shipments",
    "validate_recipients",
]
