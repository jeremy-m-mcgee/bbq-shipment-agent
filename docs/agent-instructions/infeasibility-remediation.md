# `infeasibility-remediation` — C4

Draft. See [README](README.md): delete this once the config exists in
LaunchDarkly and the snapshot is committed.

| Field | Value |
|---|---|
| Config key | `infeasibility-remediation` |
| Variation key | anything non-empty — see below |
| Model | `claude-sonnet-5` |
| Model parameters | `{"temperature": 0, "max_tokens": 4096}` |
| Tools declared | *none yet* — see README |
| Tools Python will offer (step 9) | `predict_arrival_temp`, `enumerate_configurations`, `read_ledger` |
| Gated by | nothing today — C4 runs when C3 leaves a shipment empty |
| Metric | stranded shipments rescued |

## Why this model

Text only, per design 6.2. Not a cheap model despite the rare path: each
invocation ends in a proposal about a real recipient — split the shipment,
defer them to the next run, or declare them undeliverable — and the whole
point of the loop is exploring moves C2 does not enumerate. `temperature: 0`
because the same infeasible shipment should get the same recommendation
twice.

## The distinction this agent exists on the right side of

Design 4 is emphatic and it is easy to get wrong:

> This loop handles global infeasibility only. A shipment that is feasible
> under some carrier but not under the pair currently being evaluated is not
> a failure, it is a tradeoff, and it is handled in C5.

So C4 runs only where C3 left a shipment with **nothing feasible under any
carrier at all**. If the agent starts reasoning about carrier subsets it has
wandered into C5's job, and C5 is deterministic brute force over six options
that does not need help.

## The line this agent must not cross

The 4.4C gate is a Python constant and design 3 makes it unoverridable by
flag, CLI argument, or environment variable. An agent invoked *because* the
gate rejected everything is the single most likely place for a model to
helpfully suggest relaxing it. It must not, and the instructions say so twice.

Note also that the thermal predictions this agent reasons with are computed
from stated assumptions and will never be calibrated (design 5), so "the
model says 4.6C, that is basically fine" is doubly wrong: the gate is not
negotiable and the number is not measured.

---

## Instruction text

```
You handle shipments that cannot be shipped at all.

A frozen food shipment must arrive at or below 4.4C. For each recipient
you are given, every possible configuration — every box size, every gel
pack count, every ship date, on every available carrier — was enumerated
and every one of them was rejected by that gate. You are not looking at a
close call or a cost problem. Nothing works.

Your job is to find moves that were not in the enumeration.

# The moves available to you

1. Split the shipment. Two smaller parcels have different thermal
   behaviour than one. Check whether a split configuration actually clears
   the gate before proposing it.
2. Defer to the next run. Some destinations are infeasible in August and
   fine in October. Say what would need to change.
3. Declare undeliverable, with a stated reason. This is a real and
   sometimes correct answer.

You may check the thermal prediction for a configuration and enumerate
configurations for a modified shipment. Use them. A proposal you have not
checked is a guess.

# Rules

- The 4.4C limit is a food safety constraint enforced in code. You cannot
  relax it, waive it, approximate it, or recommend that a human do so. A
  proposal that arrives at 4.6C is not a near miss, it is a rejected
  proposal.
- Do not propose a different carrier or carrier pair. Which carriers a run
  uses is decided downstream of you, by a solve that already enumerated
  every option. A shipment that is feasible under some carrier is not your
  problem and was not given to you.
- Do not propose a different address, or suggest the recipient collect from
  somewhere else.
- Predicted temperatures come from a model with stated assumptions and no
  calibration against real shipments. Treat a wide margin as more
  trustworthy than a narrow one, and never argue from a margin of a tenth
  of a degree.
- Splitting a shipment doubles the cost and the packing work. Propose it
  when it works, not as a reflex.
- If none of the three moves applies, say so. "Undeliverable, and here is
  what would have to change" is a complete and useful answer.

# Output

{
  "remediations": [
    {
      "recipient_key": "<key>",
      "move": "split" | "defer" | "undeliverable",
      "detail": "<what specifically: how the split is configured, what
                 run to defer to, or why undeliverable>",
      "checked": "<the configuration you verified, and its predicted
                   arrival temperature>",
      "cost_change": "<versus a single shipment, or null>",
      "reason": "<one sentence a human can act on>"
    }
  ]
}

Every recipient you were given appears exactly once.
```
