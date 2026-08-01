"""LaunchDarkly evaluation context. Design section 6.6.

One flag evaluated against a multi-context, rather than parallel config trees
per stage. That is what lets `memory` be on for `address_repair` and off for
`carrier_selection` because a targeting rule says so, and it expresses the
memory axis as targeting rather than as a separate subsystem.

The `shipment` kind is what makes specific records that have caused trouble
before targetable directly, so a recipient whose address has failed extraction
three times can be pre-flagged without any code change.

Built as a plain dict and handed to `Context.from_dict`, so the shape here is
literally the shape in the design doc and can be read against it.
"""

from __future__ import annotations

from typing import Any

from ldclient import Context

#: Stage keys used as the `stage` context kind. These are targeting
#: identifiers, not an agent registry -- `carrier_selection` is a stage that
#: has no agent, and targeting it is the point.
STAGE_RUN_INIT = "run_init"
STAGE_ADDRESS_REPAIR = "address_repair"
STAGE_INFEASIBILITY_REMEDIATION = "infeasibility_remediation"
STAGE_MANIFEST_VERIFICATION = "manifest_verification"
STAGE_REVIEW_NARRATOR = "review_narrator"
STAGE_CARRIER_SELECTION = "carrier_selection"


class ContextError(Exception):
    """The constructed context is not one LaunchDarkly would evaluate."""


def build_context(
    *,
    run_id: str,
    stage: str,
    profile: str,
    campaign: str | None = None,
    packet_count: int | None = None,
    recipient_key: str | None = None,
    shipment_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the multi-context payload for one evaluation.

    `shipment` is included only when a recipient is in scope. Run-level agents
    such as `manifest-verification` have no shipment, and inventing a
    placeholder key for them would create a targetable context that does not
    correspond to anything real.
    """
    if not run_id:
        raise ContextError("run_id is required; it is the run context's key.")
    if not stage:
        raise ContextError("stage is required; it is how flags target per agent.")

    run: dict[str, Any] = {"key": run_id, "profile": profile}
    if campaign is not None:
        run["campaign"] = campaign
    if packet_count is not None:
        run["packet_count"] = packet_count

    context: dict[str, Any] = {
        "kind": "multi",
        "run": run,
        "stage": {"key": stage},
    }

    if recipient_key is not None:
        shipment: dict[str, Any] = {"key": recipient_key}
        shipment.update(shipment_attributes or {})
        context["shipment"] = shipment
    elif shipment_attributes:
        raise ContextError(
            "shipment_attributes were supplied without a recipient_key, so there "
            "is no shipment context to attach them to."
        )

    return context


def to_ld_context(payload: dict[str, Any]) -> Context:
    """Convert to an SDK Context, refusing to pass along an invalid one.

    The SDK returns an invalid Context rather than raising, and evaluating one
    silently yields the fallback value for every flag -- a run that looks fine
    and quietly ignored its targeting. Check it here instead.
    """
    context = Context.from_dict(payload)
    if not context.valid:
        raise ContextError(f"invalid evaluation context: {context.error}")
    return context


def reason_code(reason: Any) -> str:
    """Flatten an SDK evaluation reason to a short ledger-friendly string.

    Lives here rather than beside either caller because the capability
    provider and the agent config source both write reasons to the same
    ledger fields. Two spellings of `RULE_MATCH` would make the reasons
    unqueryable across streams, which is the one thing they exist for.
    """
    if not isinstance(reason, dict):
        return "UNKNOWN"
    kind = reason.get("kind", "UNKNOWN")
    if kind == "ERROR":
        return f"ERROR:{reason.get('errorKind', 'UNKNOWN')}"
    if kind == "RULE_MATCH" and reason.get("ruleId"):
        return f"RULE_MATCH:{reason['ruleId']}"
    return str(kind)
