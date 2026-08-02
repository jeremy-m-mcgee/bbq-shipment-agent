"""The model-driven loops. Design section 6.2.

Four agents, invoked independently by the Python spine, sharing no context and
handing nothing off to each other (design 9). One of them runs today:

* `manifest-verification` (D1) -- read-only critique of the manifest.

`address-repair` (B3), `infeasibility-remediation` (C4) and `review-narrator`
(D2) arrive at build order steps 7, 11 and 8. All four already have their
config retrieved and snapshotted at A1, which is why this package holds the
invocation and not the configuration.

The division of labour is design 6.1's, and it is worth restating where the
code lives: `agent_configs` gets the instruction text, the model name and the
model parameters from LaunchDarkly; `model` puts them on the wire; `metrics`
reports what happened back; and the per-agent modules supply the payload and
decide what to do with the answer. No prompt text and no model name is written
in this package.
"""

from .metrics import AgentMetrics, LaunchDarklyMetrics, NoMetrics, launchdarkly_metrics
from .model import (
    DEFAULT_MAX_TOKENS,
    AnthropicModel,
    Completion,
    Invocation,
    ModelClient,
    ModelUnavailable,
    RecordedModel,
)
from .verification import (
    AGENT_KEY,
    MAX_ATTEMPTS,
    Finding,
    Verification,
    manifest_payload,
    render_instructions,
    verify_manifest,
)

__all__ = [
    "AGENT_KEY",
    "DEFAULT_MAX_TOKENS",
    "MAX_ATTEMPTS",
    "AgentMetrics",
    "AnthropicModel",
    "Completion",
    "Finding",
    "Invocation",
    "LaunchDarklyMetrics",
    "ModelClient",
    "ModelUnavailable",
    "NoMetrics",
    "RecordedModel",
    "Verification",
    "launchdarkly_metrics",
    "manifest_payload",
    "render_instructions",
    "verify_manifest",
]
