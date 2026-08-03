"""B1: extract recipients from screenshots. Design section 4, Phase B.

"Screenshots in, structured recipient records out, via a vision model. Not
OCR. Each extracted field carries a confidence value and a provenance pointer
back to the source image and region, which B3 depends on."

## Not an agent, and configured by LaunchDarkly anyway

Design 6.3 is explicit that this is a model call: no loop, no tool access. It
still takes its model and instruction text from LaunchDarkly, because design
6.1 puts "model, temperature, token ceiling" there unconditionally and its
dividing test — could this differ between two runs of identical code with both
still correct, and do you want to attribute an outcome to the difference — is
emphatically yes for which vision model reads a screenshot.

Design 8 warns that nothing in this system reaches significance at 22 packets
a few times a year. B1 is the exception and the reason this config is worth
having: `tests/fixtures/screenshots/` is a ground-truth answer key, so a
variation can be scored offline, repeatedly, against known-correct
extractions. Every other flag's metric is an anecdote.

## One call per image

Regions are pixel coordinates in one image, so batching several into a call
invites the model to mix them up, and a per-image call is also the unit a
scoring run wants. "Single shot" in design 6.3 means no loop, not one call for
the whole run.

## Reading, not correcting

The instructions forbid fixing what the image says, and that is load-bearing
rather than fussy. B2 classifies an address and B3 repairs it, and both need
to know what was actually written: an address B1 silently corrected is one
nobody downstream can check, and it fails in exactly the way design 4 warns
about for B3 — a plausible address for the wrong doorstep, indistinguishable
from a right one.

So a damaged ZIP arrives as `9411?` and validation fails on it, which is the
correct outcome. Repair is B3's job.

## People with no address are not failures

Someone can clearly want a packet and never give an address. They come back
as `Unresolved` rather than as a `Recipient`, because `Recipient` requires an
address and an optional one would push "might have no address" through every
stage downstream. They become escalations: a person for a human to chase.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent_configs import AgentConfig
from ..agents.metrics import metrics_for
from ..agents.model import ConversingModel, Invocation, ModelUnavailable
from ..agents.verification import render_instructions
from ..context import STAGE_EXTRACTION
from ..planning.manifest import Excluded
from ..planning.rates import Address
from ..run import record_agent_invocation
from .record import Provenance, Recipient, Region
from .roster import slugify

CONFIG_KEY = "screenshot-extraction"

#: Attempts per image before the reply is recorded unparseable. Two, for the
#: same reason as D1: the failure this recovers from is a model wrapping JSON
#: in prose, which one nudge fixes or does not.
MAX_ATTEMPTS = 2

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class ExtractionError(Exception):
    """B1 could not read a screenshot at all."""


@dataclass(frozen=True)
class Unresolved:
    """Someone asking for a packet who gave no usable address.

    Not a failed extraction. Design 4 sends these to the escalation queue
    rather than dropping them, and the note is what a human needs to chase.
    """

    name: str
    provenance: Provenance
    note: str = ""

    def to_excluded(self) -> Excluded:
        return Excluded(
            recipient_key=slugify(self.name),
            name=self.name,
            reason=f"no usable address in the source image: {self.note or 'none given'}",
        )


@dataclass(frozen=True)
class ExtractionResult:
    """B1's output for one run."""

    recipients: tuple[Recipient, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()
    #: Images that produced no parseable reply. Distinct from an image with no
    #: recipients in it, which is a valid answer.
    unreadable: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 0

    def escalations(self) -> tuple[Excluded, ...]:
        return tuple(u.to_excluded() for u in self.unresolved)

    def describe(self) -> str:
        parts = [f"{len(self.recipients)} recipient(s)"]
        if self.unresolved:
            parts.append(f"{len(self.unresolved)} without an address")
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} unreadable image(s)")
        return "B1 extracted " + ", ".join(parts)


def image_block(path: Path) -> dict[str, Any]:
    """One screenshot, in the shape the model API wants.

    Base64 rather than a URL: these are local fixtures and local runs, and a
    URL would make extraction depend on something being reachable.
    """
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
        },
    }


def extract_from_images(
    run: Any,
    images: tuple[Path, ...],
    *,
    model: ConversingModel,
    ledger_root: Path | str,
    config: AgentConfig | None = None,
) -> ExtractionResult:
    """B1. One call per image, results concatenated.

    `config` defaults to the one captured at A1 -- identity comes from run
    start, never a fresh lookup, for the same reason every other invocation
    record does.

    The invocation is recorded, which matters more here than anywhere else.
    Design 6.4 mitigation 2 wants the variation key, version and instruction
    hash on every invocation, and B1 is the stage whose whole justification is
    comparing variations against an answer key: without the record, the ledger
    cannot say which prompt produced which extraction, and the comparison has
    nothing to join on.
    """
    config = config or run.agent_configs.get(CONFIG_KEY)
    if config is None or not config.available:
        reason = (
            "no config was retrieved at run start"
            if config is None
            else f"{config.source}/{config.reason}"
        )
        raise ExtractionError(f"{CONFIG_KEY}: {reason}")

    context = run.context_for_stage(STAGE_EXTRACTION)
    invocation = Invocation.from_config(config, render_instructions(config, context))
    # One tracker for the batch: B1 is a single logical invocation of the
    # extraction config, however many images it reads.
    metrics = metrics_for(config)

    metrics = metrics_for(config)
    recipients: list[Recipient] = []
    unresolved: list[Unresolved] = []
    unreadable: list[str] = []
    tokens_in = tokens_out = attempts = 0

    for path in images:
        parsed, used_in, used_out, tries = _read_one(model, invocation, path)
        tokens_in += used_in
        tokens_out += used_out
        attempts += tries
        if parsed is None:
            unreadable.append(path.name)
            continue
        found, missing = _records_from(parsed, path)
        recipients.extend(found)
        unresolved.extend(missing)

    metrics.track_tokens(tokens_in, tokens_out)
    metrics.track_error() if unreadable else metrics.track_success()

    record_agent_invocation(
        ledger_root,
        run,
        CONFIG_KEY,
        outcome=(
            f"extracted:{len(recipients)}"
            + (f" unresolved:{len(unresolved)}" if unresolved else "")
            + (f" unreadable:{len(unreadable)}" if unreadable else "")
        ),
        # One image is one call, so attempts across the batch is the honest
        # iteration count -- a retried image costs two.
        iterations=attempts,
    )

    return ExtractionResult(
        recipients=tuple(recipients),
        unresolved=tuple(unresolved),
        unreadable=tuple(unreadable),
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        attempts=attempts,
    )


def _read_one(
    model: Any, invocation: Invocation, path: Path
) -> tuple[dict[str, Any] | None, int, int, int]:
    """One image, with a bounded retry on an unparseable reply."""
    tokens_in = tokens_out = 0
    content: list[dict[str, Any]] = [
        image_block(path),
        {"type": "text", "text": "Extract every recipient from this screenshot."},
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            completion = model.converse(invocation, messages)
        except ModelUnavailable as exc:
            raise ExtractionError(f"{path.name}: {exc}") from exc
        tokens_in += completion.input_tokens
        tokens_out += completion.output_tokens

        parsed = _parse(completion.text)
        if not isinstance(parsed, str):
            return parsed, tokens_in, tokens_out, attempt

        messages = [
            {"role": "user", "content": content},
            {"role": "assistant", "content": completion.text or "(empty)"},
            {
                "role": "user",
                "content": (
                    f"That could not be parsed: {parsed}. Reply with the JSON "
                    "object described in your instructions and nothing else."
                ),
            },
        ]
    return None, tokens_in, tokens_out, MAX_ATTEMPTS


def _parse(text: str) -> dict[str, Any] | str:
    """The parsed object, or a string saying why it could not be parsed."""
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
    if not isinstance(parsed.get("recipients"), list):
        return "the JSON object has no `recipients` list"
    return parsed


def _region(raw: Any) -> Region | None:
    if not isinstance(raw, dict):
        return None
    try:
        return Region(
            x=int(raw["x"]), y=int(raw["y"]),
            width=int(raw["width"]), height=int(raw["height"]),
        )
    except (KeyError, TypeError, ValueError):
        # A malformed region is not worth losing the extraction over: B3 falls
        # back to re-reading the whole screenshot.
        return None


def _records_from(
    parsed: dict[str, Any], path: Path
) -> tuple[list[Recipient], list[Unresolved]]:
    recipients: list[Recipient] = []
    unresolved: list[Unresolved] = []

    for raw in parsed["recipients"]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            # No name and no address is not a person, it is noise.
            continue

        provenance = Provenance(
            source_image=path.name, region=_region(raw.get("region"))
        )
        street = str(raw.get("street1") or "").strip()
        city = str(raw.get("city") or "").strip()
        state = str(raw.get("state") or "").strip()
        postcode = str(raw.get("zip") or "").strip()

        if not (street and city and state and postcode):
            unresolved.append(
                Unresolved(
                    name=name,
                    provenance=provenance,
                    note=str(raw.get("note") or "").strip(),
                )
            )
            continue

        confidence = raw.get("confidence")
        recipients.append(
            Recipient(
                key=slugify(name),
                name=name,
                address=Address(
                    name=name, street1=street, city=city, state=state, zip=postcode
                ),
                provenance=provenance,
                confidence=float(confidence)
                if isinstance(confidence, (int, float))
                else None,
            )
        )

    return recipients, unresolved
