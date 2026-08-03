"""B3: repair addresses that failed validation. Design section 4, Phase B.

"Runs only on the correctable and failed set. Re-reads the source image
region, proposes a correction, re-validates through Shippo, retries within a
bounded budget. Records that still fail are escalated to a human queue, never
silently dropped."

The first tool-using agent, and the one design 6.4 calls the risky half of the
registry: its tools have signatures that change while the system is built, so
its instruction text is more expensive to edit than the read-only agents'.

## The validator adjudicates, and Python asks it

The agent reports whether it validated a correction. **That claim is not
trusted.** Every proposal is re-validated here before it is accepted, because
"the model said it checked" and "it checks out" are different facts and only
one of them is worth putting on a manifest.

This is the concrete form of design 4's warning about B3. The failure to
design against is not a bad repair but a *good-looking* one: a model that
infers a house number can produce an address that validates cleanly for the
wrong doorstep, and nothing downstream can tell. Re-validating does not catch
that — nothing catches that — but trusting an unverified claim would add a
second way to be wrong on top of it, and this one is free to close.

## Why it re-reads pixels rather than text

`read_image_region` hands back a crop of the original screenshot. B3 is
invoked precisely because the extraction was wrong, so re-reading the
extracted text would be asking the same question that already got the wrong
answer. The provenance pointer exists for this, and it is why phase B carries
`Recipient` rather than `Shipment` (see `record`).

## Escalation is a success

A record that reaches a human with "here is what I could read and here is why
I could not resolve it" is a good outcome. Design 4 says these are "escalated
to a human queue, never silently dropped", and the instruction text says so
twice, because the pressure on a repair loop is always to produce a repair.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..agents.model import ConversingModel, Invocation, ModelUnavailable
from ..agents.tools import Tool, ToolError, ToolImage, build_tools
from ..agents.verification import render_instructions
from ..context import STAGE_ADDRESS_REPAIR
from ..planning.manifest import Excluded
from ..planning.rates import Address
from ..run import record_agent_invocation
from .record import Recipient
from .validation import AddressValidator, ValidationOutcome

CONFIG_KEY = "address-repair"

#: Tool round trips allowed for one repair batch. Design 4's "bounded budget".
#: Generous because a batch may hold several records and each wants at least a
#: read and a validate; the bound is against a loop, not against effort.
MAX_ITERATIONS = 16

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class RepairUnavailable(Exception):
    """B3 cannot run. The set stays escalated, which is the safe direction."""


@dataclass(frozen=True)
class RepairResult:
    """What B3 made of the set it was given."""

    repaired: tuple[Recipient, ...] = ()
    escalated: tuple[Excluded, ...] = ()
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tools_called: tuple[str, ...] = ()
    #: Proposals the validator rejected after the agent claimed them repaired.
    #: Not an accusation, a measurement -- design 8 wants a repair success
    #: rate, and a claim that did not survive checking is part of it.
    rejected: tuple[str, ...] = ()

    def describe(self) -> str:
        parts = [f"{len(self.repaired)} repaired", f"{len(self.escalated)} escalated"]
        if self.rejected:
            parts.append(f"{len(self.rejected)} proposal(s) failed re-validation")
        return "B3 " + ", ".join(parts)


def repair_addresses(
    run: Any,
    needing_repair: tuple[Recipient, ...],
    *,
    validator: AddressValidator,
    model: ConversingModel,
    screenshots: Path | str | None = None,
    ledger_root: Path | str,
) -> RepairResult:
    """B3. Returns repaired records and an escalation list.

    Everyone given to this function comes back in exactly one of the two:
    design 4 forbids silent drops, and a repair loop that loses a record is
    worse than one that repairs nothing.
    """
    if not needing_repair:
        return RepairResult()

    config = run.agent_configs.get(CONFIG_KEY)
    if config is None or not config.available:
        reason = (
            "no config was retrieved at run start"
            if config is None
            else f"{config.source}/{config.reason}"
        )
        raise RepairUnavailable(f"{CONFIG_KEY}: {reason}")

    context = run.context_for_stage(STAGE_ADDRESS_REPAIR)
    invocation = Invocation.from_config(config, render_instructions(config, context))
    tools = build_tools(CONFIG_KEY, validator=validator, screenshots=screenshots)
    by_name = {tool.name: tool for tool in tools}

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _brief(needing_repair)}
    ]
    called: list[str] = []
    tokens_in = tokens_out = 0
    parsed: dict[str, Any] | None = None
    iterations = 0

    for iterations in range(1, MAX_ITERATIONS + 1):
        try:
            completion = model.converse(invocation, messages, tools)
        except ModelUnavailable as exc:
            raise RepairUnavailable(str(exc)) from exc
        tokens_in += completion.input_tokens
        tokens_out += completion.output_tokens
        messages.append(
            {
                "role": "assistant",
                "content": completion.raw_content
                if completion.raw_content is not None
                else completion.text,
            }
        )

        if not completion.tool_calls:
            candidate = _parse(completion.text)
            parsed = None if isinstance(candidate, str) else candidate
            break

        results = []
        for call in completion.tool_calls:
            called.append(call.name)
            results.append(_run(by_name, call))
        messages.append({"role": "user", "content": results})

    repaired, escalated, rejected = _adjudicate(needing_repair, parsed, validator)

    record_agent_invocation(
        ledger_root,
        run,
        CONFIG_KEY,
        outcome=f"repaired:{len(repaired)}/{len(needing_repair)}",
        iterations=iterations,
    )

    return RepairResult(
        repaired=repaired,
        escalated=escalated,
        iterations=iterations,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        tools_called=tuple(called),
        rejected=rejected,
    )


def _brief(needing_repair: tuple[Recipient, ...]) -> str:
    """What the agent is told about the batch. Not instructions -- those are
    LaunchDarkly's -- but the records and where to find them."""
    rows = []
    for r in needing_repair:
        provenance = r.provenance
        region = provenance.region if provenance else None
        rows.append(
            {
                "recipient_key": r.key,
                "name": r.name,
                "extracted_address": {
                    "street1": r.address.street1,
                    "city": r.address.city,
                    "state": r.address.state,
                    "zip": r.address.zip,
                },
                "confidence": r.confidence,
                "source_image": provenance.source_image if provenance else None,
                "region": (
                    {"x": region.x, "y": region.y,
                     "width": region.width, "height": region.height}
                    if region
                    else None
                ),
            }
        )
    return (
        "These records failed address validation. Repair what you can and "
        "escalate the rest.\n\n" + json.dumps({"records": rows}, indent=2)
    )


def _run(by_name: dict[str, Tool], call: Any) -> dict[str, Any]:
    """Run one tool call and shape it for the model.

    A `ToolImage` becomes an image block: `read_image_region` exists to show
    pixels, and describing them in words would be re-answering the question
    that was already answered wrongly.
    """
    tool = by_name.get(call.name)
    if tool is None:
        return _result(call.id, {"error": f"no such tool: {call.name}"}, True)
    try:
        outcome = tool.run(**call.arguments)
    except ToolError as exc:
        return _result(call.id, {"error": str(exc)}, True)
    except TypeError as exc:
        return _result(call.id, {"error": f"bad arguments for {call.name}: {exc}"}, True)
    except Exception as exc:  # noqa: BLE001 - a broken tool must not kill the loop
        return _result(call.id, {"error": f"{call.name} failed: {exc}"}, True)

    if isinstance(outcome, ToolImage):
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": [outcome.block, {"type": "text", "text": outcome.note}],
        }
    return _result(call.id, outcome, False)


def _result(call_id: str, payload: Any, is_error: bool) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": json.dumps(payload, default=str),
        "is_error": is_error,
    }


def _parse(text: str) -> dict[str, Any] | str:
    candidate = (text or "").strip()
    if not candidate:
        return "the reply was empty"
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(candidate)
        if match is None:
            return "no JSON object in the reply"
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            return f"invalid JSON ({exc.msg})"
    if not isinstance(parsed, dict):
        return f"expected a JSON object, got {type(parsed).__name__}"
    return parsed


def _adjudicate(
    needing_repair: tuple[Recipient, ...],
    parsed: dict[str, Any] | None,
    validator: AddressValidator,
) -> tuple[tuple[Recipient, ...], tuple[Excluded, ...], tuple[str, ...]]:
    """Accept only the proposals the validator actually accepts.

    The agent reports `validated: true`; this asks. See the module docstring:
    "the model said it checked" and "it checks out" are different facts.
    """
    originals = {r.key: r for r in needing_repair}
    repaired: list[Recipient] = []
    escalated: list[Excluded] = []
    rejected: list[str] = []
    handled: set[str] = set()

    for raw in (parsed or {}).get("repairs", []) or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("recipient_key") or "")
        original = originals.get(key)
        proposed = raw.get("proposed")
        if original is None or key in handled or not isinstance(proposed, dict):
            continue

        address = Address(
            name=original.name,
            street1=str(proposed.get("street1") or "").strip(),
            city=str(proposed.get("city") or "").strip(),
            state=str(proposed.get("state") or "").strip(),
            zip=str(proposed.get("zip") or "").strip(),
        )
        if not all((address.street1, address.city, address.state, address.zip)):
            continue

        try:
            result = validator.validate(address)
        except Exception as exc:  # noqa: BLE001 - an unvalidatable proposal is not a repair
            handled.add(key)
            rejected.append(key)
            escalated.append(
                Excluded(key, original.name, f"repair could not be validated: {exc}")
            )
            continue

        handled.add(key)
        if result.outcome is ValidationOutcome.FAILED:
            rejected.append(key)
            escalated.append(
                Excluded(
                    key,
                    original.name,
                    "the proposed repair did not validate: "
                    f"{address.street1}, {address.city} {address.state} "
                    f"{address.zip} — {'; '.join(result.messages) or 'no detail'}",
                )
            )
            continue
        # The validator's canonical form, not the agent's proposal, for the
        # same reason B2 quotes against the corrected address.
        repaired.append(replace(original, address=result.usable))

    for raw in (parsed or {}).get("escalations", []) or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("recipient_key") or "")
        original = originals.get(key)
        if original is None or key in handled:
            continue
        handled.add(key)
        read = str(raw.get("read_from_image") or "").strip()
        reason = str(raw.get("reason") or "no reason given").strip()
        escalated.append(
            Excluded(key, original.name, f"{reason}{f' (read: {read})' if read else ''}")
        )

    # Anyone the agent said nothing about. Design 4 forbids silent drops, and
    # an unparseable reply must not quietly empty the run.
    for key, original in originals.items():
        if key not in handled:
            escalated.append(
                Excluded(
                    key,
                    original.name,
                    "the repair loop returned no verdict for this record",
                )
            )

    return tuple(repaired), tuple(escalated), tuple(rejected)
