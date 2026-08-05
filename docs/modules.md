# Module map

What each module in `src/bbq_shipment_agent/` is for. Stage labels (A1, B2, C5…)
refer to the pipeline in [design.md](design.md) section 4.

## Root

| Module | Purpose |
|---|---|
| `__init__.py` | Package entry point. Exposes `main` for the `bbq-shipment-agent` console script. |
| `cli.py` | The command line front-end: `ledger verify\|rebuild`, `run init\|plan\|review`, and `ui`. It builds a `RunOptions` and calls `wiring`, so it sequences no stage itself. |
| `wiring.py` | The only module that constructs anything which opens a socket — LD client, Shippo quoter, Anthropic model. Both front-ends assemble their live or replayed paths here, so an offline fallback or cache path cannot drift between them. |
| `run.py` | A1: mint a run ID, check the kill switch, resolve capabilities against the run context, and open the run record. Also holds the `CapabilityProvider` seam and the LD client bootstrap. |
| `capabilities.py` | Loads `config/capabilities.yaml` and resolves a capability set in fixed order: profile, then flag overrides, then prerequisites. Every capability it knows about is read by a stage — `authority` and `memory` were removed for gating nothing (design 6.5), taking the ceiling clamp with them. The kill switch is read only from the committed file, never from a flag or environment variable. |
| `context.py` | The only place a LaunchDarkly evaluation context is built (`run`, `stage` and `image` kinds), and the only caller of `Context.from_dict`. Attributes are declared per kind and an undeclared one is refused rather than forwarded, because a context is sent to LD's servers. |
| `agent_configs.py` | Retrieves the four LD-configured stages' instructions and model parameters at A1, hashes the *un-rendered* template, and writes the run-start snapshot to `config/ld-snapshot.json`. That snapshot is both the audit trail and the offline instruction cache. |
| `hashing.py` | One hashing convention — algorithm, encoding and truncation — shared by the flag payload and the instruction template. Everything that hashes routes through here so hashes recorded months apart still compare. |
| `plan.py` | The spine wired end to end: A1 → B2 → B3 → B4 → C1–C6 → D1. Every stage existed before this module; none had a caller. |
| `review.py` | D2's mechanism: edit handling, the re-solve and pair comparison, and the terminal states (approved, approved with exclusions, rejected). Owns every decision in the review; `agents/narrator.py` is only its voice. |

## `ledger/` — the append-only source of truth

| Module | Purpose |
|---|---|
| `__init__.py` | Re-exports the record types, writer and rebuild entry points. |
| `schema.py` | The three record streams — run, shipment, agent invocation — one JSONL file each, with their DuckDB column types. A line is a partial update keyed by a merge key, not a row. |
| `writer.py` | Appends JSONL records and assigns the per-file monotonic `seq` a query can order by. Never edits or deletes a line, and rejects naive datetimes rather than assuming a zone. |
| `rebuild.py` | Rebuilds the derived DuckDB cache from the JSONL, dropping it in full every time. An incremental rebuild would let the database hold state the JSONL does not, at which point it has stopped being derived. |

## `recipients/` — phase B, deciding *who* is shipped to

| Module | Purpose |
|---|---|
| `__init__.py` | Phase B's public surface. |
| `record.py` | `Recipient`, phase B's unit, carrying the address plus B1's provenance pointer and confidence. `to_shipments` converts to phase C's `Shipment` at the end of B4 and drops both, so a downstream stage cannot re-read a screenshot because it is not holding one. |
| `roster.py` | Parses `recipients.yaml`, the run input: who is being shipped to, from where, and on which dates. A file rather than CLI arguments because ~22 recipients with optional lane and date pins is not something anyone types, and a file diffs between runs. |
| `extraction.py` | B1: screenshots in, structured recipient records out, via one vision call per image. Not an agent — no loop and no tools — but its model and instructions come from LaunchDarkly, and it records one invocation per image keyed on the image's content hash. |
| `validation.py` | B2: each address through Shippo validation, with a three-way outcome of clean, correctable or failed. `validation-mode` decides who adjudicates a correctable one; advisory messages on clean addresses are kept and surfaced on the manifest. |
| `repair.py` | B3: the first tool-using agent, run only on the correctable and failed set. It re-reads the source image region, proposes a correction and re-validates through Shippo within a bounded budget; what it read is reported separately from what it proposes, and escalation is a normal successful outcome. |
| `dedupe.py` | B4: within-run duplicates, then same-address consolidation, with an explicit suppression reason for everyone excluded. Reads no ledger and no clock, so the same recipient list always produces the same eligible set. |

## `planning/` — phase C, deciding *how*

Zero model calls. Every decision here is ordinary Python.

| Module | Purpose |
|---|---|
| `__init__.py` | The deterministic planning spine's public surface. |
| `catalog.py` | The physical and carrier domain we own: two box sizes, seven gel pack counts, three ship days, four carriers. Small enough to enumerate exhaustively, which is what removes the need for a solver. |
| `shipment.py` | `Shipment`, phase C's input unit — recipient, validated address, lane and any required ship date. A plain frozen dataclass, because planning runs entirely in memory and only the chosen plan is written. |
| `load.py` | C1: packet contents to weight and dimensions per shipment, currently uniform. Holds the product mass, initial temperature and specific heat the thermal model reads. |
| `configurations.py` | C2: the cross product of gel pack count, ship date and carrier service, in the smallest box the load fits. Box size is a fit check rather than an axis, because the larger box is dominated on cost and thermal margin at once. |
| `rates.py` | The Shippo seam: what services exist for a parcel on a lane and what they cost. A protocol with live, cached and recorded implementations, so a test can quote without a network call. |
| `lanes.py` | Ambient temperature per destination band and month, read from `config/lanes.yaml`. Stated assumption rather than measurement, and an unmapped state deliberately takes the hottest band so an unstated assumption is never the optimistic one. |
| `thermal.py` | C3: the 4.4C arrival gate and the lumped-capacitance model behind it. The threshold is a Python constant and a permission-level constraint, not a model parameter — it does not move when the model changes. |
| `remediation.py` | C4: what to do where C3 left a shipment with nothing feasible under any carrier — defer to a cooler month, or declare it undeliverable. Ordinary Python, not an agent, because splitting turned out to be physically impossible and what remained is arithmetic. |
| `solve.py` | C5: brute force over the six carrier pairs, assigning each shipment its cheapest feasible configuration. Computes total cost, coverage, the stranded list, whether a Saturday requirement forced the pair, and the minimum thermal margin. |
| `manifest.py` | C6: the top-ranked plan in full, grouped by ship date because that is the shape of the physical work. Carries the runner-up comparison, the suppression and escalation lists, and every field D2's narration is asked to make a claim about. |

## `agents/` — the model-call plumbing, and two stages

| Module | Purpose |
|---|---|
| `__init__.py` | The agents package surface. |
| `model.py` | The seam a model call sits behind, plus the Anthropic implementation and a recorded one for replay. Drops a model parameter the provider refuses, retries once, and reports it on `Completion.dropped_parameters`. |
| `tools.py` | The tool contract and registry: what each agent is allowed to do, and the A1 startup check that an agent's declared tools are ones Python actually offers. LD decides what an agent is told; this decides what it can do. |
| `metrics.py` | Reports invocation outcomes to LaunchDarkly through the SDK tracker, for per-variation attribution. Success is a property of the invocation, not the manifest — an agent reporting six blockers succeeded. |
| `verification.py` | D1: a read-only critique of the manifest before a human sees it, gated by `verification-enabled`. Off is a normal run, not a degraded one. |
| `narrator.py` | D2's voice: drives `review-narrator`, runs whatever tools it calls, and hands the answers back to `review.py`. Every claim it makes must trace to a computed field on the manifest, because it explains the C5 solve and does not participate in it. |

## `ui/` — the local web front-end

Binds `127.0.0.1` with no host flag and no authentication, because it serves real
home addresses and screenshots of private messages.

| Module | Purpose |
|---|---|
| `__init__.py` | Exposes `create_app`. |
| `app.py` | The routes: pick screenshots, start a run, watch it, read the manifest. Goes through `wiring` exactly as the CLI does, and the form configures a run rather than the system — no control for the threshold, the carrier cap, the kill switch or the ledger path. |
| `service.py` | Runs a plan on a worker thread and emits the same stage events the CLI prints, because a browser request cannot block for minutes. One run at a time, refused rather than queued. |
| `view.py` | Turns a `PlanResult` into the plain dictionaries a template renders, so no template reaches into the pipeline's shape. Keeps its row model the same as `ReviewSession.manifest`. |
