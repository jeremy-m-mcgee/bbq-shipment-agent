"""LaunchDarkly evaluation context. Design section 6.6.

One flag evaluated against a multi-context, rather than parallel config trees
per stage. What the `stage` kind actually varies in the built system is AI
Config retrieval: each configured stage is evaluated under its own kind, so
`address-repair` and `review-narrator` can be served different instruction
text and different models by targeting rule.

Capability flags are *not* evaluated that way. A1 resolves them all once under
`stage: run_init` and every stage reads the resolved set, which is what lets
one run have one fingerprint. Design 6.6 says so explicitly; this docstring
used to claim the opposite, with an example about a capability being on for
one stage and off for another, and a targeting rule written against that
claim would never have fired.

## One builder, and why it is not merely tidy

`ContextBuilder` is the only thing in the package that constructs a context.
Nothing else calls `Context.from_dict`, and a test pins that.

The reason is rollouts and experiments rather than neatness. A percentage
rollout is only coherent if every evaluation of that flag within a run sees
the same attributes, and an experiment is only attributable if the evaluation
event and the metric event carry the same context. Both were true before this
existed, but only by coincidence: each call site rebuilt the dict from `Run`'s
fields, and A1 hand-built its own because it runs before `Run` exists. One
object, constructed once at the top of A1 and carried on the run, makes them
true by construction.

## Attributes are declared, not passed

`_ATTRIBUTES` names what each kind may carry. Anything else is refused rather
than forwarded. That is the same shape as `resolve` rejecting unknown
capability keys, and it exists for the same reason the ledger has a secrets
rule: a context is sent to LaunchDarkly's servers, so the set of things that
can be attached to one should be a list somebody has read, not whatever a
caller had to hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ldclient import Context

#: Stage keys used as the `stage` context kind. These are targeting
#: identifiers, not an agent registry -- `infeasibility_remediation` is a
#: stage that has no agent, and it stays here because C4 is still a stage.
#:
#: Stages nobody evaluates against were removed: a targeting rule written for
#: one would never fire, and its presence here implied otherwise.
STAGE_RUN_INIT = "run_init"
STAGE_EXTRACTION = "extraction"
STAGE_ADDRESS_REPAIR = "address_repair"
STAGE_INFEASIBILITY_REMEDIATION = "infeasibility_remediation"
STAGE_MANIFEST_VERIFICATION = "manifest_verification"
STAGE_REVIEW_NARRATOR = "review_narrator"

#: What each kind may carry beyond its `key`. Read `_individual`.
#:
#: The `shipment` kind is deliberately absent. It was defined here, described
#: in design 6.6 and 7, and never evaluated against by anything in `src/` --
#: no caller ever passed a `recipient_key`. A kind nothing evaluates is
#: decorative, which this design treats as a defect wherever it appears -- the
#: `authority` and `memory` capabilities were removed on the same test -- so
#: it went rather than being left as an invitation. Per-unit
#: targeting is the `image` kind's job instead: B1 is the one stage with an
#: answer key to score a variation against. If B3 ever needs per-recipient
#: targeting, the kind comes back here and nowhere else, which is what having
#: one builder is for.
#:
#: `image` carries no attributes at all, and that is the whole design. Its key
#: is a content hash, so nothing about a screenshot of somebody's private
#: message thread leaves this machine -- not the filename, not the directory
#: it sat in. What the hash refers to is recorded on the run row, where it is
#: committed but local.
#:
#: `image_count` is how many screenshots were submitted for this run, and it
#: is a `run` attribute rather than an `image` one for the same reason
#: `packet_count` is: it describes the run, and the `image` kind is key-only
#: on purpose. It is a count and never a name, so it leaks nothing a run row
#: does not already hold. See `ContextBuilder.image_count` for why it is
#: settled before A1 rather than at B1.
#:
#: `user` is keyed on a username and carries the department as its one
#: attribute. It is here rather than as two more `run` attributes because of
#: what a key is for: `run.key` is a fresh UUID, so a percentage rollout
#: bucketed on the run kind re-rolls every run and never splits a population.
#: A username is stable across runs and present on every evaluation in one, so
#: a rule or a rollout written against it behaves the way whoever wrote it
#: expects. That is the same argument that earned the `image` kind its place,
#: applied to the other stable identifier this system has.
#:
#: Department is an attribute rather than a fourth kind because nothing
#: buckets on a department as a unit. A rule can read the attribute, and LD
#: can bucket by it inside the `user` kind if that day comes; a `department`
#: kind nothing evaluated against would be the decorative kind this design
#: keeps deleting.
#:
#: Both values come from `config/operators.yaml` and nowhere else. The
#: department is never an input, so a form and a rule cannot disagree about
#: which one somebody is in.
_ATTRIBUTES: dict[str, frozenset[str]] = {
    "run": frozenset({"profile", "campaign", "packet_count", "image_count"}),
    "stage": frozenset(),
    "image": frozenset(),
    "user": frozenset({"department"}),
}


class ContextError(Exception):
    """The constructed context is not one LaunchDarkly would evaluate."""


@dataclass(frozen=True)
class ImageIdentity:
    """One screenshot, as LaunchDarkly and as the ledger each need it.

    `key` is a content hash and is what LD sees. `name` is the filename and
    never leaves the machine in a context -- it goes on the run row instead,
    beside the key, so a variation LD served can be traced back to a file
    locally without the filename having been sent anywhere.

    Content rather than filename because the same bytes are the same unit: a
    renamed or re-sorted directory is not a new population to bucket, and
    `RecordedVision` already matches replays on content for the same reason.
    """

    key: str
    name: str

    @classmethod
    def of(cls, path: Path) -> ImageIdentity:
        from .hashing import short_hash

        return cls(key=short_hash(path.read_bytes().hex()), name=path.name)


def _individual(kind: str, key: str, attributes: dict[str, Any]) -> dict[str, Any]:
    """One individual context, with its attributes checked against the list.

    Attributes whose value is None are dropped rather than sent: LaunchDarkly
    would store the null and a targeting rule would have to know to expect it,
    which makes "absent" and "present but unset" two states where the design
    has one.

    The check cannot fail from inside this module today, because every caller
    passes literal fields. It is here for the edit that adds a fifth field to
    `ContextBuilder` and forgets to declare it, which is the failure it exists
    to catch -- and which it turns into an error at the first evaluation
    rather than an attribute silently reaching LD.
    """
    if not key:
        raise ContextError(f"the {kind} context needs a key; it is how LD targets it.")
    allowed = _ATTRIBUTES.get(kind)
    if allowed is None:
        raise ContextError(
            f"{kind!r} is not a declared context kind. Known: "
            f"{', '.join(sorted(_ATTRIBUTES))}."
        )
    present = {name: value for name, value in attributes.items() if value is not None}
    undeclared = sorted(set(present) - allowed)
    if undeclared:
        raise ContextError(
            f"the {kind} context may not carry {undeclared}. Declare it in "
            f"_ATTRIBUTES first -- a context is sent to LaunchDarkly, so what "
            f"may be attached to one is a list, not a caller's choice."
        )
    return {"key": key, **present}


@dataclass(frozen=True)
class ContextBuilder:
    """This run's targeting identity, and the only source of contexts.

    Frozen, and built once at the top of A1 before the capability provider is
    contacted, so the context A1 evaluates against and the one B1 is retrieved
    under are the same object rather than two dicts that happen to agree.
    """

    run_id: str
    profile: str
    campaign: str | None = None
    packet_count: int | None = None
    #: How many screenshots this run submitted to B1.
    #:
    #: Settled immediately before extraction, which in this pipeline means
    #: before A1 rather than at B1: `wiring.screenshots_for` resolves the
    #: images first precisely so B1's config can be retrieved per image at run
    #: start (design 6.6). A count attached at B1 would arrive after every
    #: evaluation that could target on it -- including `screenshot-extraction`
    #: itself -- so a targeting rule written against it would never fire. It
    #: reaches the builder at construction instead, which is also what keeps
    #: the run kind identical across every evaluation in the run.
    #:
    #: `None` means nobody said, which is a hand-built builder or a caller
    #: that predates images. `initialize_run` always passes a number, so a
    #: roster run says 0 -- "no screenshots" is a measurement, not a silence.
    image_count: int | None = None
    #: Who this run claims to be, as a `user` context kind. Both fields come
    #: from `config/operators.yaml` via `OperatorPool.get`, which is what
    #: makes the department a lookup rather than a claim -- see `operators`.
    #:
    #: `None` means no `user` kind at all rather than one keyed "unknown".
    #: An unattributed run and a run by somebody nobody listed are the same
    #: thing, and giving them a key would make them look like a population.
    operator: str | None = None
    department: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ContextError("run_id is required; it is the run context's key.")
        if not self.profile:
            raise ContextError(
                "profile is required; a run that cannot say which profile it "
                "resolved is not one a targeting rule can describe."
            )
        if self.department and not self.operator:
            # A kind cannot exist without a key, so a department with nobody
            # to hang it on would be silently dropped -- and a rule written
            # against it would silently never fire.
            raise ContextError(
                "a department needs an operator to belong to; the user "
                "context is keyed on the username. Both come from "
                "config/operators.yaml together."
            )

    def for_stage(self, stage: str) -> dict[str, Any]:
        """The multi-context for evaluating something at one stage."""
        if not stage:
            raise ContextError("stage is required; it is how flags target per stage.")
        context = {
            "kind": "multi",
            "run": _individual(
                "run",
                self.run_id,
                {
                    "profile": self.profile,
                    "campaign": self.campaign,
                    "packet_count": self.packet_count,
                    "image_count": self.image_count,
                },
            ),
            "stage": _individual("stage", stage, {}),
        }
        if self.operator:
            # On every stage's context rather than only A1's, because this is
            # the kind AI Config retrieval can usefully target: a variation
            # served to one operator has to reach `review_narrator` as well as
            # `run_init` or the rule fires once and then stops.
            context["user"] = _individual(
                "user", self.operator, {"department": self.department}
            )
        return context

    def for_image(self, stage: str, image: ImageIdentity) -> dict[str, Any]:
        """The stage context plus the one screenshot being evaluated for.

        This is the only kind in the system whose key is stable across runs
        and appears more than once within one, which is what makes it the
        only unit a percentage rollout or an experiment can mean anything on.
        The run key is a fresh UUID, so bucketing on it re-rolls every time.
        """
        context = self.for_stage(stage)
        context["image"] = _individual("image", image.key, {})
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
