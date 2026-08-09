"""Pipeline results as plain data, for a template to render.

Templates get dictionaries of strings and numbers, never a `PlanResult`. Two
reasons, and only the second is about taste. A template that reaches into
`result.solve.partial[0].assignments[3].evaluated.configuration` is a second
place the pipeline's shape is written down, and it breaks silently -- Jinja
renders a missing attribute as nothing at all, so the page would just lose a
column. And a view function can be tested without a browser.

Nothing here computes. Every number on the page is read off a field the
pipeline already produced, which is the same rule design 4 puts on the D2
narrator and for the same reason: a UI that works out a total itself is a
second implementation of the thing being reviewed.
"""

from __future__ import annotations

import re
from typing import Any

from markupsafe import Markup, escape

from ..planning import render
from ..wiring import RunOptions

#: The narrator emits light markdown. These render bold, italic and inline code
#: and nothing else -- see `render_markdown`.
_MD_CODE = re.compile(r"`([^`\n]+?)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_ITALIC = re.compile(r"\*(?!\s)([^*\n]+?)(?<!\s)\*")


def render_markdown(text: str | None) -> Markup:
    """Render the narrator's light markdown to safe HTML.

    HTML is escaped *first*, so the only tags ever introduced are the three this
    adds; the content between them stays escaped. That is what keeps a model
    reply from injecting markup -- the same reason design 6.6 escapes anything
    sent to LaunchDarkly. Newlines are left to the bubble's `white-space:
    pre-wrap` rather than turned into `<br>`.
    """
    if not text:
        return Markup("")
    s = str(escape(text))
    s = _MD_CODE.sub(r"<code>\1</code>", s)
    s = _MD_BOLD.sub(r"<strong>\1</strong>", s)
    s = _MD_ITALIC.sub(r"<em>\1</em>", s)
    return Markup(s)


def run_header(context: Any, options: RunOptions) -> dict[str, Any]:
    """A1's answer: what this run is allowed to do, and who said so.

    The most useful thing on the page and the easiest to lose. In the CLI it
    scrolls past above a manifest; here it stays at the top, because "why did
    this run behave differently" is answered by these fields and design 2
    requires them to be recoverable.
    """
    run = context.run
    resolved = run.resolved_capabilities()
    reasons = run.cap_reasons()
    kill = run.kill_switch
    return {
        "run_id": run.run_id,
        "connection": context.connection,
        "profile": run.profile,
        "fingerprint": run.cap_fingerprint,
        "snapshot": context.snapshot,
        "ledger": str(options.ledger),
        # Empty until planning has evaluated the set: capabilities are live
        # per stage now, so a header built at A1 carries none of them. The
        # worker rebuilds this header after planning for exactly that reason.
        "capabilities": [
            {"name": name, "value": value, "reason": reasons.get(name, "")}
            for name, value in (resolved.to_mapping().items() if resolved else ())
        ],
        "kill_switch": (
            {"value": kill.value, "source": kill.source, "reason": kill.reason}
            if kill is not None
            else None
        ),
        "agents": [
            {
                "key": key,
                "available": config.available,
                "source": config.source,
                "reason": config.reason,
                "variation": config.variation_key or "",
                "version": config.version,
                "instruction_hash": config.instruction_hash,
                "model": config.model or "",
            }
            for key, config in sorted(run.agent_configs.items())
        ],
    }


def plan_view(context: Any, options: RunOptions) -> dict[str, Any]:
    """Everything the result page shows, whether or not there is a manifest.

    A run that covers nobody is not an error and does not render as one: the
    honest output is the partial plans and who they strand, which is exactly
    what the CLI prints in the same case.
    """
    result = context.result
    manifest = result.manifest
    view: dict[str, Any] = {
        "outcome": "manifest" if manifest is not None else "no-manifest",
        "reason": result.reason,
        "screenshots": [p.name for p in context.images],
        "roster": {
            "source": str(context.roster.source) if context.roster.source else "",
            "packet_count": context.roster.packet_count,
            "ship_dates": [d.isoformat() for d in context.roster.ship_dates],
        },
        "validation": {
            "mode": result.validation.mode.value,
            "corrected": result.validation.corrected_count,
            "escalated": len(result.validation.escalated),
        },
        "repair": _repair(result),
        "repair_unavailable": result.repair_unavailable,
        "escalated": [
            {"key": e.recipient_key, "name": e.name, "reason": e.reason}
            for e in result.escalated
        ],
        "suppressed": [
            {"key": e.recipient_key, "name": e.name, "reason": e.reason}
            for e in result.suppression.suppressed
        ],
        "consolidated": {
            key: list(folded)
            for key, folded in result.suppression.consolidated.items()
        },
        "remediations": [
            {
                "key": r.recipient_key,
                "name": r.name,
                "move": r.move.value,
                "describe": r.describe(),
            }
            for r in result.remediations
        ],
        "verification": _verification(result.verification),
        "extraction": _extraction(context.extraction, context.images),
        "partial": _partial(result.solve),
        "manifest": None,
        "manifest_text": "",
    }
    if manifest is not None:
        view["manifest"] = _manifest(manifest)
        view["manifest_text"] = render(manifest)
    return view


def extract_view(context: Any, options: RunOptions) -> dict[str, Any]:
    """What an extract-only run has to show: which variation read which image.

    The same key set `plan_view` produces, with the planning halves empty, so
    the template's existing guards skip them rather than each one growing a
    second condition. `outcome` is `extracted` and not `no-manifest`: there was
    never going to be a manifest, and the page must not report a normal run as
    a disappointing one.

    What it adds is the join the CLI prints. B1's config is retrieved per image
    under a context keyed on the file's content hash (design 6.6), so "which
    variation read this screenshot" is the only question an extraction run
    exists to answer, and the answer is per row rather than per run.
    """
    roster = context.roster
    keys = context.extra_reasons.get("screenshot_keys", {})
    return {
        "outcome": "extracted",
        "reason": "extract only — the run stopped after B1",
        "screenshots": [p.name for p in context.images],
        "images": [
            {
                "name": image.name,
                "key": keys.get(image.name, "?"),
                "variation": _image_config(context, keys.get(image.name, ""), "variation_key"),
                "model": _image_config(context, keys.get(image.name, ""), "model"),
            }
            for image in context.images
        ],
        "roster": {
            "source": str(roster.source) if roster.source else "",
            "packet_count": roster.packet_count,
            "ship_dates": [d.isoformat() for d in roster.ship_dates],
        },
        "recipients": [
            {
                "name": r.name,
                "address": f"{r.address.street1}, {r.address.city} "
                f"{r.address.state} {r.address.zip}",
                "confidence": r.confidence,
                "source": r.provenance.source_image if r.provenance else "",
            }
            for r in roster.recipients
        ],
        "extraction": _extraction(context.extraction, context.images),
        # Everything downstream of B1, which did not run. Present and empty so
        # the page renders one shape whatever the depth was.
        "validation": None,
        "repair": None,
        "repair_unavailable": None,
        "escalated": [],
        "suppressed": [],
        "consolidated": {},
        "remediations": [],
        "verification": None,
        "partial": {"infeasible": [], "plans": []},
        "manifest": None,
        "manifest_text": "",
    }


def review_view(controller: Any) -> dict[str, Any]:
    """The D2 review pane as plain data. Design section 4, Phase D.

    Same rule as every other view function: nothing here computes or decides.
    The manifest is `session.manifest` through the *same* `_manifest` the read-
    only page uses, so an edited plan renders identically to a planned one --
    design 4's "a pane beside the manifest, not a second manifest." Every edit
    outcome, the pending confirmation and the terminal state are read off fields
    `ReviewSession` already produced; the classification is Python's and this is
    a window onto it.

    The narrator's opening narration is `turns[0]` (design 4 requires it before
    any table) and is surfaced separately from the operator exchange in
    `turns[1:]`, so the internal opening prompt is never shown as if the
    operator typed it. A button-driven review has no narrator and no turns.
    """
    from ..review import EditOutcome

    session = controller.session
    narrator = controller.narrator
    turns = list(narrator.turns) if narrator is not None else []

    pending = None
    if session.pending is not None:
        # The held-back result is the most recent pair-moved edit; its own
        # `describe` is the sentence design 4 wants shown before confirming.
        for r in reversed(session.history):
            if r.outcome is EditOutcome.PAIR_MOVED:
                pending = {
                    "edit": r.edit.describe(),
                    "describe": r.describe(),
                    "carriers_before": list(r.carriers_before),
                    "carriers_after": list(r.carriers_after),
                    "cost_delta": r.cost_delta,
                    "newly_stranded": list(r.newly_stranded),
                }
                break

    last = session.history[-1] if session.history else None
    last_action = None
    if last is not None:
        last_action = {
            "outcome": last.outcome.value,
            "describe": last.describe(),
            "edit": last.edit.describe(),
            # Only a refusal carries these; empty otherwise. The gate is not the
            # operator's to override, so the alternatives are the way forward.
            "refusal": last.refusal,
            "alternatives": [d.isoformat() for d in last.alternatives],
        }

    return {
        "manifest": _manifest(session.manifest) if session.manifest is not None else None,
        # None when the operator has edited away every covering subset. The pane
        # stays usable so they can edit back to a plan or reject.
        "no_coverage": session.manifest is None,
        "narrator_available": controller.narrator_available,
        "opening": turns[0].reply if turns else "",
        "transcript": [
            {"prompt": t.prompt, "reply": t.reply, "tools_called": list(t.tools_called)}
            for t in turns[1:]
        ],
        "history": [
            {"edit": r.edit.describe(), "outcome": r.outcome.value, "describe": r.describe()}
            for r in session.history
        ],
        "pending": pending,
        "last_action": last_action,
        # A recorded ending (approved / approved_with_exclusions / rejected), or
        # `abandoned` when the operator left without one -- both close the pane.
        "terminal": (
            session.terminal.value
            if session.terminal is not None
            else ("abandoned" if controller.abandoned else None)
        ),
        "carriers": list(session.carriers),
        "total_cost": session.total_cost,
        "excluded": [
            {"key": e.recipient_key, "name": e.name, "reason": e.reason}
            for e in session.excluded
        ],
        # The edit controls: who is still on the plan, and the run's candidate
        # ship dates for a pin. Both come straight off the session.
        "recipients": [
            {"key": s.recipient_key, "name": s.name} for s in session.shipments
        ],
        "ship_dates": [d.isoformat() for d in session.ship_dates],
    }


def _extraction(extraction: Any, images: tuple[Any, ...]) -> dict[str, Any] | None:
    """What B1 could not read, for a page that otherwise reports only wins.

    None on a run that read no screenshots, so the template's existing guards
    skip it rather than rendering an empty panel on every roster run.

    `read` is counted against the images actually sent, because the number
    that matters is a ratio: four of seven screenshots producing nothing is
    not visible in a recipient count, and a run that lost most of its people
    otherwise renders as a tidy short list. The reason travels with each row
    for the same argument `Unreadable` makes -- an instruction variation that
    stopped asking for JSON, a model that wrapped it in prose and an empty
    reply are three different problems for three different people.
    """
    if extraction is None:
        return None
    unreadable = [{"name": u.name, "reason": u.reason} for u in extraction.unreadable]
    return {
        "images": len(images),
        "read": len(images) - len(unreadable),
        "unreadable": unreadable,
        "unresolved": [
            {"name": u.name, "note": u.note, "source": u.provenance.source_image
             if u.provenance else ""}
            for u in extraction.unresolved
        ],
    }


def _image_config(context: Any, key: str, field: str) -> str:
    config = context.run.image_configs.get(key)
    return (getattr(config, field, None) or "") if config else ""


def _manifest(manifest: Any) -> dict[str, Any]:
    names = {row.recipient_key: row.name for row in manifest.rows}
    return {
        "run_id": manifest.run_id,
        "carriers": list(manifest.carriers),
        "packet_count": manifest.packet_count,
        "total_cost": manifest.total_cost,
        "min_margin": manifest.min_thermal_margin_c,
        "fingerprint": manifest.cap_fingerprint,
        "forced_by_saturday": manifest.forced_by_saturday,
        # Named, not just flagged. A boolean says a constraint bound without
        # saying whose, which is the failure design 4 records in full.
        "saturday_only": [names.get(k, k) for k in manifest.saturday_only],
        "stranded": [names.get(k, k) for k in manifest.stranded],
        "infeasible": [names.get(k, k) for k in manifest.infeasible],
        "dates": [
            {
                "date": when.strftime("%a %d %b %Y"),
                "iso": when.isoformat(),
                "rows": [_row(r) for r in rows],
                "cost": round(sum(r.cost for r in rows), 2),
            }
            for when, rows in manifest.by_ship_date().items()
        ],
        "runners_up": [
            {
                "carriers": list(r.carriers),
                "total_cost": r.total_cost,
                "extra_cost": r.extra_cost,
                "coverage": r.coverage,
                "covers_all": r.covers_all,
                "stranded": [names.get(k, k) for k in r.stranded],
                "forced_by_saturday": r.forced_by_saturday,
                "min_margin": r.min_thermal_margin_c,
            }
            for r in manifest.runners_up
        ],
        # Against the row, not as a summary count: the operator is deciding
        # about *that shipment*, and "the validator said something it did not
        # act on" is only useful next to the address it concerns.
        "advisories": {
            names.get(key, key): list(messages)
            for key, messages in manifest.advisories.items()
        },
    }


def _row(row: Any) -> dict[str, Any]:
    return {
        "key": row.recipient_key,
        "name": row.name,
        "address": f"{row.address['street1']}, {row.address['city']} "
        f"{row.address['state']} {row.address['zip']}",
        "box_size": row.box_size,
        "gel_packs": row.gel_pack_count,
        "carrier": row.carrier,
        "service": row.service,
        "cost": row.cost,
        "arrival": row.expected_arrival.strftime("%d %b"),
        "temp": row.predicted_arrival_temp_c,
        "margin": row.thermal_margin_c,
    }


def _repair(result: Any) -> dict[str, Any] | None:
    if result.repair is None:
        return None
    return {
        "describe": result.repair.describe(),
        # In shadow the call was paid for and the repairs were deliberately
        # not used. Saying so matters: the escalations below still stand.
        "applied": result.applied_repairs,
        "repaired": [
            {
                "name": r.name,
                "address": f"{r.address.street1}, {r.address.city} "
                f"{r.address.state} {r.address.zip}",
            }
            for r in result.repair.repaired
        ],
        "rejected": list(result.repair.rejected),
    }


def _verification(verification: Any) -> dict[str, Any] | None:
    if verification is None:
        return None
    return {
        "outcome": verification.outcome,
        "describe": verification.describe(),
        "ran": verification.ran,
        "reason": verification.reason,
        "iterations": verification.iterations,
        "input_tokens": verification.input_tokens,
        "output_tokens": verification.output_tokens,
        "model_requested": verification.model_requested,
        "model_responded": verification.model_responded,
        "drifted": verification.model_drifted,
        "blockers": len(verification.blockers),
        "findings": [
            {
                "check": f.check,
                "severity": f.severity,
                "problem": f.problem,
                "shipments": list(f.shipments),
                "evidence": f.evidence,
                "blocking": f.blocking,
            }
            for f in verification.findings
        ],
        "raw": verification.raw,
    }


def _partial(solve: Any) -> dict[str, Any]:
    """What the partially covering subsets would do, when none of them covers.

    Five, the same as the CLI prints. The list is ranked by coverage and the
    tail says nothing the head does not.
    """
    return {
        "infeasible": list(solve.infeasible),
        "plans": [
            {
                "carriers": list(plan.carriers),
                "coverage": plan.coverage,
                "total_cost": plan.total_cost,
                "stranded": list(plan.stranded),
            }
            for plan in solve.partial[:5]
        ],
    }


def screenshot_catalogue(directory: Any, images: tuple[Any, ...]) -> list[dict[str, Any]]:
    """The picker's rows: one per image, with whatever is known about it.

    `ground_truth.json` is an answer key for scoring B1, and where it exists it
    also says how many people are in each screenshot and how hard they are to
    read. That turns the picker from a list of filenames into a control you
    can aim -- "read the two hard ones" -- which is the whole reason a browser
    beats `--screenshot-count`.

    Absent or unreadable, the captions are simply missing. It is a test
    fixture, not a required input, and a directory of real screenshots will
    never have one.
    """
    import json
    from pathlib import Path

    truth: dict[str, Any] = {}
    path = Path(directory) / "ground_truth.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for shot in data.get("screenshots", []):
                truth[shot.get("file", "")] = shot
        except (ValueError, OSError):
            truth = {}

    rows = []
    for image in images:
        known = truth.get(image.name, {})
        recipients = known.get("recipients", [])
        rows.append(
            {
                "name": image.name,
                "source_type": known.get("source_type", ""),
                "expected": len(recipients) if known else None,
                "difficulties": sorted(
                    {r.get("difficulty", "") for r in recipients} - {""}
                ),
                # People in the thread who must *not* come back. Captioned
                # because the picker exists to be aimed, and an image whose
                # whole point is invisible in its caption cannot be: "2
                # recipients, clean" describes the easy half of
                # `09-whatsapp-neighbours` and hides the half under test.
                "decoys": len(known.get("non_recipients", [])),
                "size_kb": round(image.stat().st_size / 1024),
            }
        )
    return rows
