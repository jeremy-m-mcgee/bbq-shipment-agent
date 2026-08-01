"""One hashing convention, shared by everything the ledger has to correlate.

Two different things get hashed in this system -- the resolved flag payload and
an agent's instruction template -- and they are compared across runs months
apart. They therefore have to agree on algorithm, encoding, and truncation, or
a hash recorded by one part of the pipeline cannot be matched against a hash
recorded by another.

Truncated to 16 hex characters deliberately. These are identity labels read by
a human scanning `git log` or a ledger line, not a security boundary: 64 bits
is far past the collision risk of a few hundred distinct values, and the short
form is legible in a table.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Hex characters kept from the digest. Changing this invalidates every hash
#: already recorded in the ledger, so it is not a knob.
HASH_LENGTH = 16


def short_hash(text: str) -> str:
    """Hash a string. The base convention everything else routes through."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def canonical_hash(value: Any) -> str:
    """Hash a JSON-serializable value, independent of key order.

    Sorting keys is what makes this stable: two payloads carrying the same
    values must produce the same hash regardless of the order LaunchDarkly or
    `json` happened to emit them in, or every run would look like a change.
    """
    return short_hash(json.dumps(value, sort_keys=True, separators=(",", ":")))
