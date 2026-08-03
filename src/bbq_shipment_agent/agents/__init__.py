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

from .metrics import InvocationMetrics, NoMetrics, SdkMetrics, metrics_for
from .model import (
    DEFAULT_MAX_TOKENS,
    AnthropicModel,
    Completion,
    ConversingModel,
    Invocation,
    ModelClient,
    ModelUnavailable,
    RecordedConversation,
    RecordedModel,
    RecordedVision,
    ToolCall,
)
from .narrator import MAX_TOOL_ITERATIONS, Narrator, NarratorUnavailable, Turn
from .tools import (
    TOOL_NAMES,
    Tool,
    ToolContractError,
    ToolError,
    assert_tool_contract,
    build_tools,
)
from .verification import (
    MAX_ATTEMPTS,
    Finding,
    Verification,
    manifest_payload,
    render_instructions,
    verify_manifest,
)

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "MAX_ATTEMPTS",
    "MAX_TOOL_ITERATIONS",
    "TOOL_NAMES",
    "AnthropicModel",
    "Completion",
    "InvocationMetrics",
    "NoMetrics",
    "SdkMetrics",
    "ConversingModel",
    "Finding",
    "Invocation",
    "ModelClient",
    "ModelUnavailable",
    "Narrator",
    "NarratorUnavailable",
    "RecordedConversation",
    "RecordedModel",
    "RecordedVision",
    "Tool",
    "ToolCall",
    "ToolContractError",
    "ToolError",
    "Turn",
    "Verification",
    "assert_tool_contract",
    "build_tools",
    "metrics_for",
    "manifest_payload",
    "render_instructions",
    "verify_manifest",
]
