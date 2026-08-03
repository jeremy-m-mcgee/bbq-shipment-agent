Full design: @docs/design.md

## Environment
- uv only. Use `uv add <pkg>`, never `pip install`.
- Run commands with `uv run`, or rely on the venv already being on PATH.
- uv.lock is committed. Do not regenerate it casually.

## Hard rules
- 4.4C arrival threshold is a constant in code. Never a flag, CLI arg, or env var.
- authority_ceiling lives in config/capabilities.yaml. LD can lower authority, never raise it.
- Max 2 carriers per run.
- The system spends no money. Dispatch (E1/E3) was removed — design 9. Nothing consults `authority` today; it stays resolved, clamped and recorded so the mechanism is not retrofitted later.
- Ledger is append-only JSONL. DuckDB is derived and rebuildable. Never write DuckDB as source of truth.
- A ledger line is a partial update, not a row. To change a value, append another record with the same merge key. Never edit or delete a line.
- Ledger timestamps are UTC. Naive datetimes are rejected, not assumed.

## Secrets
- Secrets live in `.env` (gitignored). `.env.example` is the committed template and holds no real values.
- Never in `config/capabilities.yaml` — that file is committed on purpose so authority changes show up in `git log`.
- Never in LD agent instruction text. The run-start snapshot commits that text to the repo.
- Never in a ledger record, especially `cap_snapshot`. The ledger is committed and append-only, so a secret written there cannot be removed by a later append.
- `flag_payload` records the *parsed* capability overrides, never the raw LD payload. `resolve` rejects unknown keys and coerces every value through a `StrEnum`, so the recorded dict is structurally incapable of carrying free text. A test pins that: adding a capability whose values are not an enum breaks it rather than silently widening what reaches the ledger.
- `UV_ENV_FILE` (devcontainer) makes `uv run` load `.env`, and setup.sh seeds `.env` from `.env.example`. So an unconfigured key is present-but-empty, not absent: check `if not os.environ.get("LD_SDK_KEY")`, never `is None`. Treat empty as unconfigured and take the `baseline` offline fallback rather than handing `""` to the LD SDK.

## Boundaries
- LD holds agent instruction text and model params. Nothing else.
- Python holds all control flow, tool definitions, and tool execution.
- If a change would express control flow in LD config, stop and ask.

## LD-configured stages (4) — not all of them are agents
screenshot-extraction (B1, *not* an agent) | address-repair (B3)
manifest-verification (D1) | review-narrator (D2)

The registry is `LD_CONFIGURED_STAGES`, meaning "model + instructions come
from LD". B1 is in it because design 6.1 puts model choice in LD and B1 is the
only stage with a ground-truth answer key, so its metric is a real
measurement. It has no loop and no tools and never will — design 6.3.

C4 was the fourth and was demoted to ordinary Python: its only open-ended move
(splitting a shipment) is physically impossible, and what remained is
arithmetic. Design 2 keeps a model out of the path of a computable decision.

## Build order
See docs/design.md section 11. Steps 1, 2 and 3 are done. `run plan` runs
A1 → B2 → C1–C6 → D1 from `recipients.yaml` and prints a verified manifest.
Everything through C6 makes no model call; D1 is the one agent, gated by
`verification-enabled` and strictly downstream of the manifest. Steps 8 and 10
are done too: `run review` holds the D2 conversation and records a terminal
state, and B4 runs between B2 and C1. Step 4 is done in the
sense the step meant: `LumpedCapacitanceModel` is C3's default. Calibration
is not coming — E3 was removed, so design 5 now says the model will not be
calibrated, and the lane ambient in design 10 is the only thermal lever left.
All ten steps are built. B1 is `recipients/extraction.py`, B3 is
`recipients/repair.py`.

## Layout
- `src/bbq_shipment_agent/ledger/` — schema.py (records), writer.py (append-only JSONL), rebuild.py (DuckDB cache)
- `src/bbq_shipment_agent/plan.py` — the spine wired end to end: A1 → B2 → B4 → C1–C6 → D1
- Phase B works on `Recipient` (address + provenance + confidence); `to_shipments` makes the `Shipment` phase C wants at the end of B4. Provenance never reaches planning.
- `src/bbq_shipment_agent/agents/` — model.py (the model-call seam), metrics.py (LD AI metrics), tools.py (contract + registry), verification.py (D1), narrator.py (D2)
- `src/bbq_shipment_agent/recipients/` — record.py (`Recipient`, phase B's type), extraction.py (B1), roster.py (the run input file), validation.py (B2), repair.py (B3), dedupe.py (B4)
- `src/bbq_shipment_agent/planning/` — catalog, rates (Shippo seam), configurations (C2), thermal (C3), lanes (ambient), remediation (C4), solve (C5), manifest (C6)
- `src/bbq_shipment_agent/review.py` — D2 edit handling and terminal states
- `src/bbq_shipment_agent/capabilities.py` — config load, ceiling clamp, prerequisites, fingerprint
- `src/bbq_shipment_agent/context.py` — LD multi-context (run / stage / shipment), reason codes
- `src/bbq_shipment_agent/agent_configs.py` — AI Config retrieval, instruction hash, snapshot / offline cache
- `src/bbq_shipment_agent/hashing.py` — the one hashing convention. Everything that hashes routes through it.
- `src/bbq_shipment_agent/run.py` — A1 initialize_run, `CapabilityProvider` seam, LD client bootstrap
- `src/bbq_shipment_agent/wiring.py` — `RunOptions`, `Progress`, and the *only* place a live client is constructed. Both front-ends go through it.
- `src/bbq_shipment_agent/ui/` — the local web app: app.py (routes), service.py (worker thread + events), view.py (results as plain data), templates/
- `config/capabilities.yaml` — profiles + permission flags. Quote `off`/`on`: YAML 1.1 reads them as booleans.
- `config/lanes.yaml` — ambient per destination band + month. Stated assumptions, never measured; an unmapped state takes the *hottest* band on purpose.
- `config/ld-snapshot.json` — committed AI Config snapshot. Audit trail and offline cache in one file.
- `ledger/*.jsonl` — the committed source of truth. `ledger.duckdb` is derived and gitignored.
- `recipients.yaml` — the run input. Gitignored (home addresses); `recipients.example.yaml` is the template.
- `.cache/` — live Shippo answers, gitignored. A cache of an API, not a run artifact.
- `uv run pytest`, `uv run bbq-shipment-agent ledger verify|rebuild`, `uv run bbq-shipment-agent run init|plan|review`, `uv run bbq-shipment-agent ui`

## Front-ends
- Two: the CLI and `ui`. Neither sequences a stage. Both build a `RunOptions` and call `wiring.open_run` then `wiring.plan_with`, so an offline fallback or a cache path cannot drift between them.
- `wiring.py` is the only module that constructs something which opens a socket. If a new live client appears anywhere else, that claim is dead and the "no test can open a socket" property goes with it.
- The UI binds 127.0.0.1 and there is no host flag. It has no authentication because nothing off the machine can reach it, and it serves real home addresses and screenshots of private messages. Adding a host option changes that trade silently.
- The form configures a run. It does not configure the system: no control for the 4.4C threshold, the carrier cap, authority, or the ledger path. A field for the ledger is a way to append a real run to the wrong file.
- `/screenshots/{name}` serves from a dict built by globbing the resolved directory, keyed by exact filename. Never `dir / name`.
- One run at a time, refused rather than queued: two would append to the same ledger and quote the same lanes twice.
- Which images B1 read go on the run row (`evaluation_reasons["screenshots"]`). A seeded sample is reconstructible from its seed; a set picked by hand is reconstructible from nothing.
- Replay is all or nothing. `RunOptions.replaying` is what the page calls "no live calls", and B1/B3 need `--extractions`/`--repairs` for it to be true on a screenshot run.

## Capability rules
- A provider proposes; the repo decides. Order is fixed: profile → flag overrides → ceiling clamp → prerequisites.
- Never clamp before applying overrides, or a payload could survive the ceiling.
- Unmet prerequisites demote and record why. They never abort the run.
- LD serves `planner-mode`, `memory-mode`, `verification-enabled` and nothing else. `authority-level` is never asked for — see `CAPABILITY_FLAGS`.
- An absent flag proposes nothing. It is not an instruction to overwrite the profile with a default.
- Both A1 sources default to offline. Live LD is injected, never reached for, so no test can open a socket.
- Query nested ledger JSON with `json_extract_string(...)`, not `->>` — DuckDB mis-resolves that operator inside a compound predicate.

## Agent invocation
- A model parameter LD serves may be refused by the provider (`temperature` is deprecated for Sonnet 5). `AnthropicModel` drops it, retries once, and reports it on `Completion.dropped_parameters`. Nothing validates parameters at startup — design 10.
- LD supplies instructions, model name, and model parameters. Nothing in `agents/` hardcodes a prompt or a model.
- Identity for the ledger record and the LD metric both come from the config captured at A1. Never a fresh lookup at invocation time.
- The rendered template goes to the model; the un-rendered one is what gets hashed and snapshotted.
- An unknown model parameter from LD is dropped, not forwarded. A console typo must not become a TypeError mid-run.
- `verification-enabled` off is a normal run, not a degraded one. Skipping writes no invocation record; an invocation that *ran* is always recorded, including when its reply could not be parsed.
- LD metric success is about the invocation, not the manifest. An agent reporting six blockers succeeded.

## Agent configs
- Hash and snapshot the *un-rendered* template. Rendered text carries `ldctx` (recipient data) into committed files, and its hash differs every run, so it discriminates nothing.
- Snapshot on `available`, never on "no exception". A served-but-disabled config is a real answer from LD and must not fall through to stale cached text.
- An unreachable run never overwrites the snapshot. That cache is the fallback precisely when LD is down.
- Invocation records take their identity from the config captured at A1, never a fresh lookup.
- All four configs exist in LD and are snapshotted. Only the *config key* must match code (`AGENT_STAGES`); variation keys are free-form but must be non-empty, or `launchdarkly_metrics` silently drops to `NoMetrics`.
- Create AI Configs in **Agent mode**, even `screenshot-extraction`, which is not an agent. `_from_variation` reads `instructions`; a Completion-mode config carries `messages` and comes back `unavailable`.
- Declare a tool on an AI Config only in the same change that registers it in Python. Declaring fewer than Python offers is not drift; declaring one Python lacks is the failure 6.4 mitigation 1 exists to catch. All four currently declare none, and `manifest-verification` stays that way — it is read-only by design.
- Instruction text lives in LD, not in the repo. Drafting a change in a scratch file is fine; committing it is not. Two copies where one is unserved is the drift the snapshot exists to detect.
