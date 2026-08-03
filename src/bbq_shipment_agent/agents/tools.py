"""What each agent is allowed to do, and the check that says so at startup.

Design 6.1 draws the line this module sits on:

> LD decides what an agent is told. Python decides what an agent can do. An
> instruction can ask for a tool that Python does not offer, and the correct
> outcome is a caught error, not an expanded capability.

Instruction text moved to LaunchDarkly under the medium split, which broke the
guarantee that an instruction and the tool signatures it references always
ship together. Design 6.4 mitigation 1 buys that back and calls it "the single
highest-value mitigation": each AI Config declares the tool names it expects,
A1 checks that set against what Python actually registers, and a mismatch
aborts the run at startup rather than surfacing mid-flight — where "mid-flight"
means during a live shipping run.

## The contract and the implementation are separate on purpose

`TOOL_NAMES` is static and knowable at import time. It is what A1 asserts
against, and A1 runs before a quoter, a validator or a thermal model exists.

`build_tools` produces the callable versions, and needs those dependencies
injected. Keeping them apart means the startup check does not drag a Shippo
connection into `initialize_run`. `test_tools` pins that the two agree, so the
contract cannot quietly describe a tool nobody implemented.

## Declared is a subset, not an equality

An agent declaring *fewer* tools than Python offers is not drift: Python is
permitted to offer something the instructions never mention, and refusing that
would make adding a tool a breaking change to four configs. The failure being
caught is the other direction — an instruction referencing a tool that does
not exist, which fails at the moment the model tries to call it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..agent_configs import AGENT_KEYS, AgentConfig

#: Agent key -> the tool names Python offers it. Design 6.2's registry, in the
#: form the startup assertion needs.
#:
#: `manifest-verification` is deliberately and permanently empty: design 6.2
#: grants it "manifest read, read-only", and design 10 settles the apparent
#: contradiction in favour of that grant. D1 refuses to run if its config
#: declares a tool, which is this rule enforced a second time at invocation.
TOOL_NAMES: dict[str, frozenset[str]] = {
    "address-repair": frozenset({"validate_address"}),
    "infeasibility-remediation": frozenset(),
    "manifest-verification": frozenset(),
    "review-narrator": frozenset({"propose_edit", "confirm_edit", "read_manifest"}),
}


class ToolContractError(Exception):
    """An agent config expects a tool Python does not offer.

    Raised at A1 and never caught: design 6.4 makes this a startup error by
    design, because the alternative is discovering it when a model calls the
    tool during a run that is quoting real carriers.
    """


class ToolError(Exception):
    """A tool was called and could not do its job.

    Returned to the model as a tool result rather than raised out of the loop.
    An agent that asks for an address that cannot be validated should be told
    so and given the chance to try something else; killing the run would turn
    every bad argument into an outage.
    """


@dataclass(frozen=True)
class Tool:
    """One capability Python offers an agent.

    `input_schema` is JSON Schema, in the shape the model API expects. It is
    written here rather than derived from the callable's signature because it
    is a contract with the model, and a contract that changes silently when
    someone renames a parameter is not one.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[..., Any]

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def assert_tool_contract(configs: dict[str, AgentConfig]) -> None:
    """Design 6.4 mitigation 1. Called from A1, before the run record exists.

    Checks every retrieved config's declared tools against `TOOL_NAMES`. An
    unavailable config is skipped: it has no instructions, so it cannot be
    referencing a tool, and failing a run because a config nobody will invoke
    declares something is the wrong trade when 6.10 makes unavailable normal.
    """
    problems: list[str] = []
    for agent_key, config in sorted(configs.items()):
        if not config.available:
            continue
        offered = TOOL_NAMES.get(agent_key)
        if offered is None:
            problems.append(
                f"{agent_key}: retrieved from LaunchDarkly but Python has no "
                f"tool contract for it. Known agents: {', '.join(AGENT_KEYS)}."
            )
            continue
        missing = sorted(set(config.declared_tools) - offered)
        if missing:
            problems.append(
                f"{agent_key}: the AI Config declares {missing} but Python "
                f"offers {sorted(offered) or 'nothing'}. Either register the "
                f"tool in Python or remove it from the config — an instruction "
                f"referencing a tool that does not exist fails when the model "
                f"calls it, mid-run."
            )

    if problems:
        raise ToolContractError(
            "agent tool contract mismatch at run start:\n  " + "\n  ".join(problems)
        )


def build_tools(agent_key: str, **dependencies: Any) -> tuple[Tool, ...]:
    """The callable tools for one agent, bound to its dependencies.

    Separate from `TOOL_NAMES` so the startup assertion stays free of I/O. The
    keyword arguments are the seams — a validator, a quoter, a thermal model —
    injected the same way every other live dependency in this package is.
    """
    builder = _BUILDERS.get(agent_key)
    if builder is None:
        return ()
    return builder(**dependencies)


def _address_repair_tools(validator: Any = None, **_: Any) -> tuple[Tool, ...]:
    """B3's tools. `read_image_region` joins them when B1 exists (step 5).

    Only `validate_address` is offered today, which is why `TOOL_NAMES` lists
    only that: the contract describes what Python actually has, not what the
    design intends it to have eventually. A contract that promised the image
    tool would make the assertion pass for an instruction that cannot work.
    """
    if validator is None:
        return ()

    def validate_address(
        street1: str, city: str, state: str, zip: str, name: str = ""
    ) -> dict[str, Any]:
        from ..planning import Address

        result = validator.validate(
            Address(name=name or "recipient", street1=street1, city=city,
                    state=state, zip=zip)
        )
        return {
            "outcome": result.outcome.value,
            "validated_address": {
                "street1": result.corrected.street1,
                "city": result.corrected.city,
                "state": result.corrected.state,
                "zip": result.corrected.zip,
            },
            "messages": list(result.messages),
        }

    return (
        Tool(
            name="validate_address",
            description=(
                "Validate a US postal address. Returns one of three outcomes: "
                "'clean' (the address is deliverable as given), 'correctable' "
                "(deliverable, but a material field was changed — compare the "
                "returned address against what you sent), or 'failed' (no "
                "such address). Only a 'clean' or 'correctable' result is a "
                "repair. The validator adds ZIP+4; that is not a correction."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "street1": {"type": "string", "description": "Street address"},
                    "city": {"type": "string"},
                    "state": {"type": "string", "description": "Two-letter code"},
                    "zip": {"type": "string", "description": "Five digits"},
                    "name": {"type": "string", "description": "Recipient name"},
                },
                "required": ["street1", "city", "state", "zip"],
            },
            run=validate_address,
        ),
    )


def _review_narrator_tools(session: Any = None, **_: Any) -> tuple[Tool, ...]:
    """D2's tools. Design 6.2 grants "manifest read, re-solve trigger".

    The re-solve trigger is split in two because design 4 splits it: an edit
    that moves the optimal carrier set is *proposed* and waits, and confirming
    it is a separate act by the operator. One tool that both proposed and
    applied would collapse that distinction and let a model apply a run-wide
    change on its own — which is exactly what the confirmation exists to stop.

    Note what is absent: nothing here approves, rejects, or excludes without
    going through the same classification. Terminal states are the operator's,
    taken through `ReviewSession`, not something the narrator can call.
    """
    if session is None:
        return ()

    from datetime import date as _date

    from ..review import Edit, EditKind

    def read_manifest() -> dict[str, Any]:
        from .verification import manifest_payload

        if session.manifest is None:
            return {
                "manifest": None,
                "problem": "no carrier subset covers the run as currently edited",
                "infeasible": list(session.solve.infeasible),
            }
        return manifest_payload(
            session.manifest,
            tuple(s.recipient_key for s in session.shipments),
        )

    def propose_edit(
        kind: str, recipient_key: str, ship_date: str = "", reason: str = ""
    ) -> dict[str, Any]:
        try:
            edit = Edit(
                kind=EditKind(kind),
                recipient_key=recipient_key,
                ship_date=_date.fromisoformat(ship_date) if ship_date else None,
                reason=reason,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        from ..review import ReviewError

        try:
            result = session.propose(edit)
        except ReviewError as exc:
            raise ToolError(str(exc)) from exc

        return {
            "outcome": result.outcome.value,
            "applied": result.applied,
            "needs_confirmation": result.needs_confirmation,
            "refusal": result.refusal,
            "feasible_dates": [d.isoformat() for d in result.alternatives],
            "carriers_before": list(result.carriers_before),
            "carriers_after": list(result.carriers_after),
            "cost_before": result.cost_before,
            "cost_after": result.cost_after,
            "cost_delta": result.cost_delta,
            "newly_stranded": list(result.newly_stranded),
        }

    def confirm_edit() -> dict[str, Any]:
        from ..review import ReviewError

        try:
            result = session.confirm()
        except ReviewError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "applied": True,
            "carriers": list(session.carriers),
            "total_cost": session.total_cost,
            "edit": result.edit.describe(),
        }

    return (
        Tool(
            name="read_manifest",
            description=(
                "Return the current manifest as structured data: every "
                "shipment with its carrier, service, cost, ship date, "
                "expected arrival and thermal margin, plus the runner-up "
                "carrier subsets and anyone excluded, escalated or stranded. "
                "Call this again after any applied edit — the plan changes."
            ),
            input_schema={"type": "object", "properties": {}},
            run=read_manifest,
        ),
        Tool(
            name="propose_edit",
            description=(
                "Re-solve the plan with one change applied, and report what "
                "it would do. This does NOT necessarily apply the change. "
                "Three outcomes: 'unchanged' means the optimal carrier set "
                "held and the edit is already applied; 'pair_moved' means the "
                "edit rewrites carrier and cost across the whole run and is "
                "waiting for the operator to confirm it; 'refused' means the "
                "change puts a shipment above the 4.4C arrival limit and will "
                "not be applied at all, with feasible dates offered instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["ship_date", "exclude"],
                        "description": "ship_date pins a recipient to one "
                        "date; exclude removes them from the run.",
                    },
                    "recipient_key": {"type": "string"},
                    "ship_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD, required for kind=ship_date. "
                        "Must be one of the run's candidate ship dates.",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["kind", "recipient_key"],
            },
            run=propose_edit,
        ),
        Tool(
            name="confirm_edit",
            description=(
                "Apply the edit that propose_edit held back because it moved "
                "the optimal carrier set. Call this only after the operator "
                "has explicitly agreed to the run-wide change."
            ),
            input_schema={"type": "object", "properties": {}},
            run=confirm_edit,
        ),
    )


_BUILDERS: dict[str, Callable[..., tuple[Tool, ...]]] = {
    "address-repair": _address_repair_tools,
    "review-narrator": _review_narrator_tools,
}
