"""What each run mode does, in one sentence, for the page to render.

The three capability modes each gate one pipeline stage: `validation` gates
B2, `planner` gates B3, `verification` gates D1. This module holds the
human-readable effect of every value a stage can read, so `run.html` shows
plain language instead of a bare enum table.

The strings restate the enum docstrings in `capabilities.py`. Centralising
them keeps the UI from inventing a second description that could drift from
what the stage actually does -- the same rule the view layer follows for
numbers, one step over onto prose.

It is keyed on the *value* a stage reads (`off`/`shadow`/`on` ...), which is a
code fact and stable whether the value came from the committed profile config
or a live LaunchDarkly evaluation. It is deliberately **not** a profile -> mode
mapping: which modes a profile produces is LaunchDarkly's to decide, and is
shown -- resolved -- on the results page.
"""

from __future__ import annotations

#: capability name -> (stage code, human title). The stage code is the label
#: the rest of the design uses, so a reader can find the same stage in
#: docs/design.md.
_STAGE: dict[str, tuple[str, str]] = {
    "validation": ("B2", "Address validation"),
    "planner": ("B3", "Address repair"),
    "verification": ("D1", "Manifest verification"),
}

#: capability name -> {value: one-sentence effect}.
_EFFECT: dict[str, dict[str, str]] = {
    "validation": {
        "off": "Skipped. Addresses are quoted exactly as they were supplied.",
        "standard": "Applies the validator's correction; only an address it "
        "cannot fix is escalated to a human.",
        "strict": "Escalates any address that was not already clean to a human.",
    },
    "planner": {
        "off": "Never runs. A broken address stays on the escalation list for "
        "a human to chase.",
        "shadow": "Runs and records what it would repair, but the repairs are "
        "not applied.",
        "on": "Repairs a correctable address and re-validates it before it "
        "reaches the plan.",
    },
    "verification": {
        "off": "Skipped. The manifest is reviewed by a human with no "
        "self-critique pass in front of it.",
        "on": "The D1 agent critiques the manifest before the operator sees it.",
    },
}

#: value -> a tag tone class in base.html. `off` reads as muted, a partial
#: state (`shadow`) as a warning, an active one as the accent. An unknown value
#: takes the accent so it is visible rather than hidden.
_TONE: dict[str, str] = {
    "off": "",
    "shadow": "warn",
    "standard": "on",
    "strict": "on",
    "on": "on",
}

_UNKNOWN_EFFECT = "An unrecognised value; the stage falls back to its default."


def describe(name: str, value: str) -> dict[str, str]:
    """One mode as plain data: which stage it gates and what this value does.

    A value the tables do not know still renders -- with the raw value and a
    generic note -- rather than dropping the mode off the page. A capability
    added to the repo without an entry here is then visible and obviously
    undescribed, which is the failure worth seeing.
    """
    stage, title = _STAGE.get(name, ("", name))
    effect = _EFFECT.get(name, {}).get(value, _UNKNOWN_EFFECT)
    return {
        "name": name,
        "stage": stage,
        "title": title,
        "value": value,
        "effect": effect,
        "tone": _TONE.get(value, "on"),
    }
