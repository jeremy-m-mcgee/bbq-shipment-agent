# Demo runbook

**Eight to ten minutes. Three flags. One roster, one commit, four different runs.**

The thing being demonstrated is not that the pipeline works. It is the boundary:
Python owns every decision that can be computed, LaunchDarkly owns the values
that could differ between two correct runs, and the ledger makes the difference
reconstructible months later. Three flags are enough to show all three claims.

| # | Flag | Type | Shows |
|---|---|---|---|
| 1 | `pipeline-kill-switch` | operational | Stopping the pipeline is a console action, and it fails **open** |
| 2 | `validation-mode` | operational | A three-valued dial, not a boolean, changing deterministic behaviour |
| 3 | `verification-enabled` | release | Gating an agent — and the model it runs on |

`planner-mode` is deliberately not toggled. It gates B3, which needs screenshots
and live vision calls, and its replay path is known to diverge from live
behaviour (design 10). It stays visible in every capability readout, so you can
point at it without spending it.

---

## Before the room

**The day before**, warm the quote cache so no segment waits on Shippo:

```bash
demo/demo.sh check
```

It confirms the three keys, reports the cache size, and does a live `run init`
so you find out then rather than on stage whether LaunchDarkly is reachable.
If `connection` says `offline`, every flag will serve its code default and
nothing you flip will move — fix that first.

**Ten minutes before**, set the starting state in the LaunchDarkly console:

| Flag | Set to |
|---|---|
| `pipeline-kill-switch` | **off** |
| `validation-mode` | **standard** |
| `verification-enabled` | **on** |

Have the console open in a second window, on the flag list, not on a detail
page. The audience should watch you flip a toggle and watch the terminal
change — the two windows side by side is the whole demo.

Runs land in `demo/ledger/`, not the committed `ledger/`, so rehearsing does
not append five runs to an append-only file you ship. Use `DEMO_LEDGER=ledger`
if you want them recorded for real.

---

## 0 — Baseline · 90s

```bash
demo/demo.sh 0
```

**Point at**, in order:

- `connection  launchdarkly` — this is live, not a fixture.
- The four agent configs. Variation key, revision, instruction hash, model.
  None of that text is in the repo.
- `capabilities` — three values, each with the reason LaunchDarkly gave.
- The manifest. Carrier pair, per-date grouping, thermal margin.
- The `D1` block at the bottom. Findings, model name, token count.

**Say:**

> Everything you see on that manifest is deterministic Python. The 4.4°C food
> safety gate is a constant in code — not a flag, not an environment variable.
> The carrier pair is brute force over six options. The costs are live carrier
> rates.
>
> What LaunchDarkly decided is those three capability lines, and which model
> wrote the findings at the bottom. That is the split: LaunchDarkly delivers
> values, Python owns control flow. If a change would put control flow in
> LaunchDarkly, that is a design error, not a feature.

---

## 1 — `pipeline-kill-switch` · 60s

```bash
demo/demo.sh 1
```

The script pauses. **Flip `pipeline-kill-switch` to ON** on screen, then press
enter.

```
error: pipeline-kill-switch is on in LaunchDarkly (launchdarkly:FALLTHROUGH). Turn it off to run.
```

**Point at:** the exit code, and the absence of anything else. No config fetch,
no ledger append.

**Say:**

> It stops at A1, before the first byte is written. An aborted run leaves no
> trace, which is what you want from a break-glass.
>
> The part worth arguing about is that it fails **open**. Pull the network and
> this run proceeds. That looks backwards until you notice the system spends no
> money — it produces a work package and stops, there is no label purchase
> anywhere in it. So a LaunchDarkly outage bricking a shipping run is the worse
> failure, and stopping is not a safety-critical open call.

The script pauses again — **flip it back to OFF** before continuing.

---

## 2 — `validation-mode` · 2min

```bash
demo/demo.sh 2
```

Runs twice, pausing for you to flip between `standard` and `strict`.

**On the first run, point at:**

- `B2 corrected 1 address(es).`
- Dev Okonkwo on the manifest, shipping.

**Say:**

> That address went in with ZIP 60631. The validator came back with 60613 — a
> different five-digit ZIP, which means a different neighbourhood and a
> different lane. Standard mode applied the correction and quoted the corrected
> lane.
>
> This matters more than it sounds. A carrier will happily price a parcel to a
> mistyped ZIP and hand back a real rate and a real transit estimate for the
> wrong destination. Every cost on a manifest built from unvalidated addresses
> rests on someone having typed them correctly.

**Flip `validation-mode` to `strict`.** Second run.

**Point at:** the `before → after` block the script prints. Four packets became
three, the total dropped by $16.85, and Dev Okonkwo is now on the escalation
list.

**Say:**

> Same code, same roster, same commit. One console change and that recipient is
> a human's problem instead of an assumption.
>
> Note it is not a boolean. The interesting question was never *whether*
> validation runs — it is who adjudicates an address the validator can fix but
> maybe should not fix silently. Strict for a first run against an unfamiliar
> list, standard once the data is trusted, and neither needs a deploy.

The script pauses — **flip back to `standard`**.

---

## 3 — `verification-enabled` · 2min

```bash
demo/demo.sh 3
```

**Flip `verification-enabled` to OFF.** First run.

**Point at:** the final line — `D1 did not run: verification-enabled is off` —
and then at what is *not* there.

**Say:**

> No invocation record was written. That is deliberate: a stage that was
> skipped writes nothing, and an invocation that actually ran is always
> recorded, including when its reply could not be parsed. Absent and empty are
> different facts and the ledger keeps them different.
>
> And this is a normal run, not a degraded one. The manifest is complete.

**Flip `verification-enabled` to ON.** Second run.

**Point at:** the finding, the model name, the iteration count, the token
counts.

**Say:**

> D1 is a read-only critique of the finished manifest. It sits strictly
> downstream and cannot change the plan — it reports, and Python decides what to
> do about it. Agency is a cost, so it is spent only where the input space is
> genuinely open.
>
> The model came from LaunchDarkly too. Swapping this from Haiku to Sonnet is a
> console change with no deploy, and five metrics go back per invocation —
> tokens, duration, time to first token, tool calls, success — attributed to the
> variation that served it. That is the per-variant attribution the whole split
> was bought for.

If you have 30 seconds spare, the honest caveat lands well: at roughly fifty
runs a year, a per-run flag accumulates about twenty-five samples an arm. Coarse
effects declare themselves over a quarter. Anyone calling a winner from three
weeks of this data is reading noise.

---

## 4 — The ledger · 90s

```bash
demo/demo.sh 4
```

**Point at:** the per-evaluation lines, then the folded per-run rows.

**Say:**

> One line per live evaluation: which run, which stage the flag was evaluated
> under, the value LaunchDarkly served, and whether LaunchDarkly or the offline
> fallback served it.
>
> This is the part that pays for itself later. The values used to live in a
> committed YAML file, so a change showed up in `git log`. They live in a
> console now, and that is a real trade — what buys it back is that every run
> records what it actually operated under. The fingerprint names the set; every
> shipment row points at it. A surprising run six months from now is
> diagnosable from this file and nothing else.
>
> And the values are coerced through an enum before they land here, so a
> console placeholder cannot write free text into an append-only committed
> file. A value that is not an enum member is discarded in favour of the
> default and only its rejection reason is recorded.

---

## Reset

```bash
demo/demo.sh reset
```

Prints the three settings to restore and re-checks the kill switch live.

Also worth knowing: a live run rewrites `config/ld-snapshot.json` when
LaunchDarkly has changed. After a demo that is expected — `git diff` it, and
commit it if the change is real.

---

## If it goes wrong

**`connection offline` mid-demo.** Keep going and make it the point. Every flag
falls back to its code default — planner off, validation standard, verification
off — and the run completes on the deterministic spine with a manually reviewed
manifest. The system gets less helpful and does not get less correct. That is a
better demo than the one you planned.

**A quote call hangs.** The cache is warm, so this means a genuinely new lane.
Ctrl-C and rerun; the partial cache persists. Do not edit the roster mid-demo —
`dev-okonkwo`'s wrong ZIP is load-bearing for segment 2.

**A flag flip does not appear to take.** Values are cached once per run, not per
stage, so a mid-run flip lands on the *next* run. Rerun the segment.

**The room asks why `planner-mode` never moved.** Good question to get. It gates
the B3 repair loop, which reads screenshots of private message threads — the one
stage worth a rollout, because it is the only one with a ground-truth answer key
to score against. It is not in this demo because it costs a vision call per
image per run and its offline replay does not reproduce live behaviour.

---

## Running a segment on its own

```bash
demo/demo.sh check       # preflight
demo/demo.sh 0           # baseline
demo/demo.sh 1           # kill switch
demo/demo.sh 2           # validation-mode
demo/demo.sh 3           # verification-enabled
demo/demo.sh 4           # ledger
demo/demo.sh all         # all of it, with pauses
demo/demo.sh reset
```

Overrides: `DEMO_LEDGER`, `DEMO_ROSTER`, `DEMO_OPERATOR` (default `jeremy`;
any key from `config/operators.yaml`). Saved output lands in `demo/.out/` so
you can diff two runs after the fact.
