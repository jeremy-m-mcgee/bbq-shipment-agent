# `manifest-verification` — draft instructions

**Status: staging draft, not a source of truth.**
`
Design 6.1 puts agent instruction text in LaunchDarkly, and 6.4 mitigation 3
makes `config/ld-snapshot.json` the repo's copy — an audit trail and the
offline cache, pulled at every run start. This file is neither. It exists
because the AI Config does not yet exist in LaunchDarkly, so there is nothing
to snapshot and nowhere else for a draft to live.

Once the config is created and a run has written the snapshot, **the snapshot
is authoritative and this file will drift silently**. Delete it then, or treat
every difference as this file being wrong.

Build order: step 2, the first and only agent config needed to close it
(design 11). `manifest-verification` goes first because it touches no tools,
so it exercises the LaunchDarkly path without also exercising tool contract
handling.

---

## Config settings

| Setting | Value | Why |
|---|---|---|
| Mode | **Agent**, not Completion | Agent configs carry `instructions` as a string; completion configs carry `messages` as a list. `_from_variation` reads `instructions` only, so a completion-mode config parses cleanly and then reports `available = False`. |
| Key | `manifest-verification` | Must match `AGENT_STAGES` in `agent_configs.py`. |
| Model | `claude-sonnet-5` | Design 6.2 says "Text, cheap model viable", and that is true of the *task*. It does not bind as a cost constraint at 3–5 runs a year — the whole model ladder spans pennies annually. `claude-haiku-4-5` is the cheap alternative. |
| Temperature | **Leave unset** | Not a preference — `claude-sonnet-5` returns 400 on a non-default `temperature`, `top_p`, or `top_k`, and Opus-tier models reject them outright. Only `claude-haiku-4-5` still accepts them. |
| Effort | `low` or `medium` | `output_config.effort`, supported on `claude-sonnet-5`. Not available on `claude-haiku-4-5` — it errors there. |
| Output | JSON schema | `output_config.format` enforces the response shape server-side, so schema conformance is not a reason to pick a stronger model. |
| Tools | none | Read-only. Step 6's tool contract assertion will check this declaration against Python's registry. |

Targeting is evaluated against the `stage` context kind with key
`manifest_verification` (design 6.6), so per-agent rules work without a
separate flag per agent.

---

## Instruction text

Paste everything inside the fence.

```
You verify a shipping manifest for frozen food before a human reviews it.
Your job is to find real problems. Finding none is a normal, frequent, and
completely acceptable outcome.

# Checks

1. Accounting. Every input recipient appears in exactly one of: eligible,
   suppressed, escalated. Flag anyone appearing in none or more than one.
2. Duplicate destinations. Two eligible shipments to the same validated
   address. Same recipient name at different addresses is not a duplicate.
3. Cost outliers. Any shipment cost far from the run's distribution. State
   the value, the run median, and the ratio.
4. Thin thermal margins. Read the computed `thermal_margin` field. Report
   the lowest few, and any below 0.5C, as worth a human's attention.
5. Arrival feasibility. The selected service's expected_arrival must be
   consistent with its ship_date. Flag any arrival on or before its ship
   date, or a Saturday ship date on a non-USPS carrier.
6. Carrier count. The run uses at most 2 distinct carriers. More is a
   blocker.
7. Box size. A large box is almost always wrong at this product weight:
   more surface area, worse thermal performance, higher dimensional
   weight. Flag any large box as worth investigating.

# Rules

- Cite the manifest fields your finding rests on. A finding you cannot
  ground in a field on the manifest is not a finding.
- Do not recompute thermal physics. The 4.4C gate ran upstream in code
  and is not yours to enforce, second-guess, or restate. You are checking
  that the manifest is internally consistent, not that the model is right.
- Do not propose a different carrier pair, ship date, or configuration.
  The solve ran upstream and you are downstream of it.
- Report; do not fix. You have read access only.
- Do not summarize what the manifest says. The human can read it. Report
  only what is wrong or suspicious.
- Say plainly when a check passes with nothing to report. Do not pad,
  reassure, or list what you verified as though it were a finding.

# Output

{
  "findings": [
    {
      "check": "<one of the seven above>",
      "severity": "blocker" | "warning" | "note",
      "shipments": ["<recipient_key>", ...],
      "evidence": "<the field values this rests on>",
      "problem": "<one sentence>"
    }
  ],
  "clean": ["<names of checks that found nothing>"]
}

blocker = a hard constraint is violated. warning = likely wrong, a human
should look. note = worth a glance.

Return an empty findings list if there is nothing wrong. That is a good
result, not a failed attempt.
```

---

## Why it is shaped this way

**The last three rules exist to fight the failure mode named in design 8.**
`verification-enabled` "is supposed to reduce the number of problems that reach
the human. If the operator's edit count during review does not drop, it is
generating self-congratulatory checks rather than finding real issues. This is
a common failure and easy to miss." An empty `findings` list has to be an
acceptable answer, stated more than once, or the agent will manufacture work to
look useful. The separate `clean` list keeps the pass auditable without letting
checks that found nothing masquerade as findings.

**Checks 1–6 are design 4's D1 list verbatim.** Check 7 is not, and is close to
free: design 5 says the larger box "should almost never be selected, and its
selection is a signal worth investigating", and `box_size` is already a field on
every shipment row.

**The thermal rule is a boundary, not a nicety.** The 4.4C threshold is a Python
constant and a permission-level constraint. An agent that restates it in its own
words is one console edit away from restating it wrongly, so the instruction
tells it the gate is not its business at all.

**No Mustache variables.** The SDK interpolates `{{...}}` and always injects the
full evaluation context as `ldctx`. Instructions are hashed and snapshotted
un-rendered precisely so recipient data never reaches a committed file; keeping
the template variable-free means the manifest arrives as a message instead, and
the instruction hash changes only when someone edits the text.

**0.5C in check 4 is a review heuristic, not a design constant.** It is exactly
the kind of value worth iterating on from the console, which is the argument for
instructions living in LaunchDarkly at all.

---

## Open question

Design 4 D1 says the loop "revises and re-checks within a bounded budget".
Design 6.2 grants it "manifest read, read-only". A read-only agent cannot revise
a manifest.

This draft resolves it as report-only, matching the tool grant. That also works
if "revises" meant the Python spine re-solves and re-invokes. If the intent was
that the agent proposes corrections, this text needs rewriting and the tool
grant needs changing — a design decision, not a wording one.
