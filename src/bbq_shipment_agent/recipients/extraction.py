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
from ..context import STAGE_EXTRACTION, ImageIdentity
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

#: A reply that is nothing but one fenced block. Anchored at both ends so a
#: fence quoted mid-prose is left to `_JSON_BLOCK` rather than mistaken for
#: the whole answer.
_FENCE = re.compile(r"^```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n?```$", re.DOTALL)

#: What the retry asks for, stated rather than referred to.
#:
#: It used to say "the JSON object described in your instructions", which
#: assumes the served instructions describe one. Instruction text lives in
#: LaunchDarkly and is free to vary -- that is the point of it living there --
#: so a variation whose output spec is a bullet list left the retry pointing at
#: a specification that did not exist. Measured against `sonnet-3-5-output-block`
#: on `05-email.png`: the second attempt returned byte-identical output to the
#: first, so the retry cost a vision call and could not have succeeded.
#:
#: Python owns the parser, so Python knows what it needs and can say so without
#: the instructions agreeing. Naming the fields matters as much as the shape:
#: that variation asks for `street` and `zip9`, which parse and then fail the
#: completeness check, which reads as a person with no address rather than as a
#: reply in the wrong schema.
RETRY_SHAPE = (
    "Reply with one JSON object and nothing else — no prose, no code fence. "
    'It must have a single key "recipients", holding a list with one entry '
    "per person. Each entry uses exactly these keys: "
    '"name", "street1", "city", "state", "zip", "confidence", "note", and '
    '"region" as {"x": 0, "y": 0, "width": 0, "height": 0}. '
    "Use those key names even if you were told different ones earlier."
)


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
class Unreadable:
    """An image whose reply could not be parsed, and why it could not be.

    The reason is the point. `_parse` computes a diagnosis -- prose instead of
    JSON, malformed JSON, a JSON object with no `recipients` list -- and until
    this record existed it was used for the retry nudge and then discarded, so
    the ledger recorded a bare `unreadable` and design 2's "diagnosable from
    the committed JSONL" did not hold for the one stage whose variations are
    meant to be compared.

    It distinguishes the failures that matter to different people. An
    instruction variation that stopped asking for JSON is a console edit; a
    model that wraps JSON in prose is a model choice; an empty reply is
    neither. All three read as "unreadable" without the reason.
    """

    name: str
    reason: str


@dataclass(frozen=True)
class ExtractionResult:
    """B1's output for one run."""

    recipients: tuple[Recipient, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()
    #: Images that produced no parseable reply, each with the reason. Distinct
    #: from an image with no recipients in it, which is a valid answer.
    unreadable: tuple[Unreadable, ...] = ()
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

    **The config is per image, not per batch.** A1 retrieves
    `screenshot-extraction` once for each screenshot, under a context keyed on
    that image's content hash, so a targeting rule or a percentage rollout can
    serve different instructions or a different vision model to different
    images within one run. This loop honours that: each image is rendered,
    invoked, tracked and recorded against the config that image was served.

    `config` overrides that for a caller invoking B1 directly. Identity still
    comes from run start either way -- nothing here looks anything up.

    The invocation is recorded per image, which matters more here than
    anywhere else. Design 6.4 mitigation 2 wants the variation key, version
    and instruction hash on every invocation, and B1 is the stage whose whole
    justification is comparing variations against an answer key. One record
    per batch could not express a run where two variations were served, so the
    comparison would have nothing to join on in exactly the case the rollout
    was run to measure.
    """
    recipients: list[Recipient] = []
    unresolved: list[Unresolved] = []
    unreadable: list[Unreadable] = []
    tokens_in = tokens_out = attempts = 0

    for path in images:
        image = ImageIdentity.of(path)
        served = config or run.config_for_image(image)
        if served is None or not served.available:
            reason = (
                "no config was retrieved at run start"
                if served is None
                else f"{served.source}/{served.reason}"
            )
            raise ExtractionError(f"{CONFIG_KEY}: {reason}")

        context = run.contexts.for_image(STAGE_EXTRACTION, image)
        invocation = Invocation.from_config(
            served, render_instructions(served, context)
        )
        # One tracker per image, because one config per image: a tracker is
        # minted by the config that was served, and mixing two variations'
        # events into one tracker would attribute them to whichever came
        # first.
        metrics = metrics_for(served)

        parsed, used_in, used_out, tries = _read_one(model, invocation, path)
        tokens_in += used_in
        tokens_out += used_out
        attempts += tries

        metrics.track_tokens(used_in, used_out)
        # A string is the reason it could not be parsed -- the same either-or
        # `_parse` returns. Recorded rather than reduced to a flag: without it
        # an instruction variation that stopped asking for JSON is
        # indistinguishable from a model that answered in prose.
        if isinstance(parsed, str):
            unreadable.append(Unreadable(name=path.name, reason=parsed))
            metrics.track_error()
            outcome = f"unreadable: {parsed}"
        else:
            found, missing = _records_from(parsed, path)
            recipients.extend(found)
            unresolved.extend(missing)
            metrics.track_success()
            outcome = f"extracted:{len(found)}" + (
                f" unresolved:{len(missing)}" if missing else ""
            )

        record_agent_invocation(
            ledger_root,
            run,
            CONFIG_KEY,
            config=served,
            image_key=image.key,
            outcome=outcome,
            iterations=tries,
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
) -> tuple[dict[str, Any] | str, int, int, int]:
    """One image, with a bounded retry on an unparseable reply.

    Returns the parsed object, or the reason it could not be parsed -- the
    same either-or `_parse` uses, so the diagnosis survives the retry loop
    instead of collapsing to `None`. The reason from the *last* attempt is the
    one returned: it describes the reply the run actually gave up on.
    """
    tokens_in = tokens_out = 0
    reason = "the model was never called"
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
        reason = parsed

        messages = [
            {"role": "user", "content": content},
            {"role": "assistant", "content": completion.text or "(empty)"},
            {"role": "user", "content": f"That could not be parsed: {reason}.\n\n{RETRY_SHAPE}"},
        ]
    return reason, tokens_in, tokens_out, MAX_ATTEMPTS


def _unfence(candidate: str) -> str:
    """The contents of a lone markdown code fence, or the text unchanged.

    A fence is how a model volunteers that something is JSON, and stripping it
    is not tolerance for a malformed reply -- it is reading the reply.
    """
    match = _FENCE.match(candidate)
    return match.group(1).strip() if match else candidate


def _load(candidate: str) -> Any:
    """Parsed JSON, or `ValueError` carrying the reason it is not.

    The whole string first, and only then the embedded-value search, because
    the search is destructive on a reply that was already valid. `_JSON_BLOCK`
    spans the first brace to the last, so on a top-level array it strips the
    brackets and turns `[{...}, {...}]` into `{...}, {...}` -- one object
    followed by a comma, which is where `invalid JSON (Extra data)` came from
    on eleven ledger rows. The model had returned valid JSON and the extractor
    made it invalid.
    """
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK.search(candidate)
    if match is None:
        raise ValueError("no JSON object in the reply")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON ({exc.msg})") from exc


def _parse(text: str) -> dict[str, Any] | str:
    """The parsed object, or a string saying why it could not be parsed.

    Tolerant about the container and unchanged about the contents. Instruction
    text lives in LaunchDarkly and is free to vary, so which wrapper a reply
    arrives in is not something Python can insist on -- but what a recipient
    row has to contain is still `_records_from`'s business, and that is
    deliberately untouched here.
    """
    candidate = _unfence((text or "").strip())
    if not candidate:
        return "the reply was empty"
    try:
        parsed = _load(candidate)
    except ValueError as exc:
        return str(exc)
    # A bare list is the recipients list. An instruction that asks for output
    # "for each recipient" invites exactly this, and refusing it discards a
    # correct extraction over the shape of its container.
    if isinstance(parsed, list):
        parsed = {"recipients": parsed}
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
