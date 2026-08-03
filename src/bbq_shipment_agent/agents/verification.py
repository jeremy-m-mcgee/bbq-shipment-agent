"""D1: verify the manifest. Design section 4, Phase D.

The first agent to actually run. Build order step 2 built the whole
LaunchDarkly path for it -- config retrieval, the instruction hash, the
run-start snapshot, the ledger record -- and then stopped, because nothing
produced a manifest to check. Step 3 produced one.

## Read-only, and what that settles

Design 6.2 grants `manifest-verification` "manifest read, read-only", while
design 4 says the loop "revises and re-checks within a bounded budget". Design
10 records the contradiction and picks the reading consistent with the tool
grant: *the agent reports, and the Python spine acts on the report*. So there
is no revision loop over the manifest here, and there are no tools.

The bounded budget that does exist is over the agent's own output: a reply
that is not parseable JSON is retried once with the parse error attached. That
is a budget on the invocation, not on the plan, and it does not put the model
any closer to the manifest.

## Everything the model is told comes from LaunchDarkly

The instruction text, the model name and the model parameters are read off the
`AgentConfig` captured at A1. This module contributes the *payload* -- the
manifest as structured data -- and nothing else. There is no prompt text here
and no model name here, which is design 6.1's line drawn in code: LD decides
what an agent is told, Python decides what it can do.

## The input recipient list is part of the payload

Check 1 asks that every input recipient appear in exactly one of eligible,
suppressed or escalated. The manifest alone cannot answer that -- it knows who
it accounted for, not who was submitted -- so the roster's keys are sent with
it. Without them the check silently passes on a manifest that dropped someone.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent_configs import AgentConfig
from ..capabilities import VerificationMode
from ..context import STAGE_MANIFEST_VERIFICATION
from ..ledger import AgentInvocationRecord
from ..planning import Manifest
from ..run import Run, record_agent_invocation
from .metrics import metrics_for
from .model import Completion, Invocation, ModelClient, ModelUnavailable

CONFIG_KEY = "manifest-verification"

#: Parse attempts before the reply is recorded unparseable. Two, because the
#: failure this recovers from is a model wrapping JSON in prose, which one
#: nudge fixes or does not.
MAX_ATTEMPTS = 2

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

SEVERITIES = ("blocker", "warning", "note")


@dataclass(frozen=True)
class Finding:
    """One thing the agent thinks is wrong."""

    check: str
    severity: str
    problem: str
    shipments: tuple[str, ...] = ()
    evidence: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == "blocker"

    def describe(self) -> str:
        who = f" [{', '.join(self.shipments)}]" if self.shipments else ""
        return f"{self.severity:<8} {self.check}{who}: {self.problem}"


@dataclass(frozen=True)
class Verification:
    """D1's result: what the agent found, and what it cost to ask."""

    #: "clean" | "findings" | "unparseable" | "skipped" | "unavailable"
    outcome: str
    findings: tuple[Finding, ...] = ()
    clean_checks: tuple[str, ...] = ()
    iterations: int = 0
    #: Why the agent did not run, when it did not.
    reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    #: What LaunchDarkly asked for, and what actually answered. Equal on a
    #: normal run. Carried separately because they are two different claims
    #: and only the first one is what the ledger attributes the run to.
    model_requested: str | None = None
    model_responded: str | None = None
    #: The reply as received, kept when it could not be parsed so the operator
    #: can see what the model actually said rather than only that it failed.
    raw: str | None = None
    record: AgentInvocationRecord | None = None

    @property
    def ran(self) -> bool:
        return self.outcome in {"clean", "findings", "unparseable"}

    @property
    def blockers(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.blocking)

    @property
    def model_drifted(self) -> bool:
        """Whether something other than the requested model answered.

        Not an error -- an alias resolving to a dated snapshot is normal and
        expected. It is worth surfacing because the ledger records the model
        LaunchDarkly served, and if that is not what ran, the record names an
        attribution nobody can reproduce.
        """
        return bool(
            self.model_requested
            and self.model_responded
            and self.model_requested != self.model_responded
        )

    def describe(self) -> str:
        if not self.ran:
            return f"D1 did not run: {self.reason}"
        if self.outcome == "unparseable":
            return "D1 returned a reply that could not be parsed as findings."
        if not self.findings:
            return f"D1 found nothing. Checks clean: {len(self.clean_checks)}."
        return f"D1 found {len(self.findings)} issue(s), {len(self.blockers)} blocking."


@dataclass
class _Attempt:
    """One trip to the model, kept so the caller can see what was tried."""

    completion: Completion | None = None
    error: str | None = None
    parsed: dict[str, Any] | None = None
    findings: list[Finding] = field(default_factory=list)
    clean: list[str] = field(default_factory=list)


def manifest_payload(
    manifest: Manifest, input_recipients: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The manifest as structured data, for a model to cite fields from.

    Deliberately not `render()`. The instructions require every finding to be
    grounded in a manifest field, and a field the agent has to parse back out
    of a formatted table is one it can misread. This is the same data the
    manifest carries, in the shape the checks are written against.
    """
    return {
        "run_id": manifest.run_id,
        "carriers": list(manifest.carriers),
        "carrier_count": len(manifest.carriers),
        "forced_by_saturday": manifest.forced_by_saturday,
        # Who forced it. `forced_by_saturday` on its own says a constraint
        # bound without saying whose, and an agent required to ground every
        # claim in a field will otherwise pick a shipment and be wrong.
        "saturday_only": list(manifest.saturday_only),
        # What the validator said about addresses it left alone.
        "validator_advisories": {k: list(v) for k, v in manifest.advisories.items()},
        "packet_count": manifest.packet_count,
        "total_cost": manifest.total_cost,
        "min_thermal_margin_c": manifest.min_thermal_margin_c,
        "cap_fingerprint": manifest.cap_fingerprint,
        # Check 1 is unanswerable without this. See the module docstring.
        "input_recipients": list(input_recipients),
        "eligible": [
            {
                "recipient_key": row.recipient_key,
                "name": row.name,
                "validated_address": row.address,
                "box_size": row.box_size,
                "gel_pack_count": row.gel_pack_count,
                "ship_date": row.ship_date.isoformat(),
                "carrier": row.carrier,
                "service": row.service,
                "cost": row.cost,
                "expected_arrival": row.expected_arrival.isoformat(),
                "predicted_arrival_temp_c": row.predicted_arrival_temp_c,
                "thermal_margin": row.thermal_margin_c,
            }
            for row in manifest.rows
        ],
        "suppressed": [
            {"recipient_key": e.recipient_key, "name": e.name, "reason": e.reason}
            for e in manifest.suppressed
        ],
        "escalated": [
            {"recipient_key": e.recipient_key, "name": e.name, "reason": e.reason}
            for e in manifest.escalated
        ],
        "stranded": list(manifest.stranded),
        "infeasible": list(manifest.infeasible),
        "runners_up": [
            {
                "carriers": list(other.carriers),
                "total_cost": other.total_cost,
                "extra_cost": other.extra_cost,
                "covers_all": other.covers_all,
                "stranded": list(other.stranded),
            }
            for other in manifest.runners_up
        ],
    }


def render_instructions(config: AgentConfig, context: dict[str, Any]) -> str:
    """Interpolate LD's Mustache template with the evaluation context.

    The un-rendered template is what gets hashed and snapshotted -- see
    `agent_configs` for why -- and this is the other half of that split: the
    rendered text is what the model sees and is never stored.

    `chevron` is the renderer the LaunchDarkly AI SDK uses, declared as a
    direct dependency rather than leaned on transitively so that our rendering
    and LD's cannot drift apart on a version bump.
    """
    import chevron

    return chevron.render(config.instructions or "", {"ldctx": context})


def verify_manifest(
    run: Run,
    manifest: Manifest,
    *,
    ledger_root: Path | str,
    model: ModelClient,
    input_recipients: tuple[str, ...] = (),
) -> Verification:
    """D1. Check the manifest against the run goal before a human sees it.

    Gated by `verification-enabled`, and skipping is a normal outcome rather
    than a degraded one: design 6.10's `baseline` profile has verification off
    by construction, so a run with no agent is a manually reviewed manifest,
    which is less helpful and not less correct.

    The invocation is recorded in the ledger whenever the agent actually ran,
    including when its reply could not be parsed. An invocation that happened
    is a fact, and a ledger that only records the successful ones cannot answer
    what design 8 asks of `verification-enabled`.
    """
    if run.capabilities.verification is not VerificationMode.ON:
        return Verification(
            outcome="skipped",
            reason=f"verification-enabled is {run.capabilities.verification.value}",
        )

    config = run.agent_configs.get(CONFIG_KEY)
    if config is None or not config.available:
        reason = (
            "no config was retrieved at run start"
            if config is None
            else f"{config.source}/{config.reason}"
        )
        return Verification(outcome="unavailable", reason=reason)

    # Design 6.4 mitigation 1 in miniature. Python offers this agent no tools
    # at all, so any declared tool is a mismatch, and a mid-run surprise is
    # exactly what that mitigation exists to convert into a startup error. The
    # general registry check is build order step 6; this is the case it covers
    # that can be checked today.
    if config.declared_tools:
        return Verification(
            outcome="unavailable",
            reason=(
                f"the AI Config declares tools {list(config.declared_tools)} but "
                f"{CONFIG_KEY} is offered none (design 6.2: manifest read, "
                "read-only). Remove them in LaunchDarkly or grant them in Python."
            ),
        )

    context = run.context_for_stage(STAGE_MANIFEST_VERIFICATION)
    try:
        invocation = Invocation.from_config(config, render_instructions(config, context))
    except ModelUnavailable as exc:
        return Verification(outcome="unavailable", reason=str(exc))

    payload = json.dumps(
        manifest_payload(manifest, input_recipients), indent=2, sort_keys=True
    )
    # One tracker for this invocation. D1 is a single logical call even when
    # a parse retry makes it two round trips.
    metrics = metrics_for(config)

    attempts: list[_Attempt] = []
    prompt = payload
    for _ in range(MAX_ATTEMPTS):
        attempt = _Attempt()
        attempts.append(attempt)
        try:
            attempt.completion = model.complete(invocation, prompt)
        except ModelUnavailable as exc:
            # A model that cannot be reached at all is not a bounded-retry
            # case: the same call would fail the same way.
            metrics.track_error()
            return Verification(
                outcome="unavailable", reason=str(exc), iterations=len(attempts)
            )

        parsed = _parse(attempt.completion.text)
        if isinstance(parsed, str):
            attempt.error = parsed
            # The nudge carries the parse error rather than repeating the ask.
            # A model that produced prose around JSON needs to be told that,
            # not told the same thing again.
            prompt = (
                f"{payload}\n\nYour previous reply could not be parsed: "
                f"{parsed}. Reply with the JSON object described in your "
                "instructions and nothing else."
            )
            continue

        attempt.parsed = parsed
        attempt.findings = _findings(parsed)
        attempt.clean = [str(c) for c in parsed.get("clean") or []]
        break

    tokens_in = sum(a.completion.input_tokens for a in attempts if a.completion)
    tokens_out = sum(a.completion.output_tokens for a in attempts if a.completion)
    metrics.track_tokens(tokens_in, tokens_out)
    # Success is about the invocation, not the manifest: an agent that
    # correctly reports six blockers did its job. Only an unparseable reply
    # is a failed invocation.
    if attempts[-1].parsed is None:
        metrics.track_error()
    else:
        metrics.track_success()

    return _result(run, ledger_root, attempts, invocation)


def _parse(text: str) -> dict[str, Any] | str:
    """The parsed object, or a string saying why it could not be parsed."""
    candidate = text.strip()
    if not candidate:
        return "the reply was empty"

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # Models fence JSON or wrap it in a sentence often enough that
        # extracting the outermost object is worth doing before giving up.
        match = _JSON_BLOCK.search(candidate)
        if match is None:
            return "no JSON object in the reply"
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            return f"invalid JSON ({exc.msg})"

    if not isinstance(parsed, dict):
        return f"expected a JSON object, got {type(parsed).__name__}"
    if "findings" not in parsed:
        return "the JSON object has no `findings` key"
    if not isinstance(parsed["findings"], list):
        return "`findings` is not a list"
    return parsed


def _findings(parsed: dict[str, Any]) -> list[Finding]:
    """Coerce the agent's findings, dropping any that carry no claim.

    Lenient about shape and strict about substance. A finding with no
    `problem` says nothing an operator can act on, and the instructions are
    explicit that a finding must be grounded in a manifest field -- so an
    empty one is dropped rather than shown as a blank row.
    """
    findings: list[Finding] = []
    for raw in parsed["findings"]:
        if not isinstance(raw, dict):
            continue
        problem = str(raw.get("problem") or "").strip()
        if not problem:
            continue
        severity = str(raw.get("severity") or "note").lower()
        shipments = raw.get("shipments")
        findings.append(
            Finding(
                check=str(raw.get("check") or "unnamed"),
                severity=severity if severity in SEVERITIES else "note",
                problem=problem,
                shipments=tuple(str(s) for s in shipments)
                if isinstance(shipments, list)
                else (),
                evidence=str(raw.get("evidence") or ""),
            )
        )
    return findings


def _result(
    run: Run,
    ledger_root: Path | str,
    attempts: list[_Attempt],
    invocation: Invocation,
) -> Verification:
    """Assemble the result and write the ledger record."""
    last = attempts[-1]
    tokens_in = sum(a.completion.input_tokens for a in attempts if a.completion)
    tokens_out = sum(a.completion.output_tokens for a in attempts if a.completion)

    if last.parsed is None:
        outcome = "unparseable"
    elif last.findings:
        outcome = "findings"
    else:
        outcome = "clean"

    record = record_agent_invocation(
        ledger_root,
        run,
        CONFIG_KEY,
        outcome=outcome,
        iterations=len(attempts),
    )

    return Verification(
        outcome=outcome,
        findings=tuple(last.findings),
        clean_checks=tuple(last.clean),
        iterations=len(attempts),
        reason=last.error,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        model_requested=invocation.model,
        model_responded=last.completion.model if last.completion else None,
        raw=last.completion.text if last.parsed is None and last.completion else None,
        record=record,
    )
