"""Phase B: recipient resolution. Design section 4.

B2 (validate) and B4 (dedupe and suppress) live here. Both are deterministic —
design 4 marks them so, and the model-driven repair loop that sits between
them is B3, which arrives at build order step 7.

Only B2 is in the spine. B4 is deferred to build order step 12 and has no
caller; see its module docstring.

Kept apart from `planning` because the phases answer different questions:
Phase B decides *who* is shipped to, Phase C decides *how*.
"""

from .dedupe import SuppressionReport, dedupe_shipments
from .validation import (
    AddressValidationUnavailable,
    AddressValidator,
    RecordedAddressValidator,
    ShippoAddressValidator,
    ValidationOutcome,
    ValidationReport,
    ValidationResult,
    classify,
    validate_shipments,
)

__all__ = [
    "AddressValidationUnavailable",
    "AddressValidator",
    "RecordedAddressValidator",
    "ShippoAddressValidator",
    "SuppressionReport",
    "ValidationOutcome",
    "ValidationReport",
    "ValidationResult",
    "classify",
    "dedupe_shipments",
    "validate_shipments",
]
