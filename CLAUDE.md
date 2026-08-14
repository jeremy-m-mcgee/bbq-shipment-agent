Full design: @docs/design.md

## Environment
- uv only. Use `uv add <pkg>`, never `pip install`.
- Run commands with `uv run`, or rely on the venv already being on PATH.
- uv.lock is committed. Do not regenerate it casually.

## Hard rules
- 4.4C arrival threshold is a constant in code. Never a flag, CLI arg, or env var.
- Max 2 carriers per run.
- The system spends no money. Dispatch (E1/E3) was removed — design 9. `authority-level`, `authority_ceiling` and the clamp were removed too, once it was clear they gated nothing — design 6.5. An acting stage added later brings its own permission with it; do not reintroduce one ahead of the stage.
- Every capability in `CAPABILITY_TYPES` is read by a stage: `planner` by B3, `validation` by B2, `verification` by D1. A test pins the set. A capability nothing consults is decorative — that is why `authority` and `memory` are gone.
- `guard` (`narrator-guard-mode`, D2's scope guard) is a `Capability` evaluated through the same gate but deliberately **not** in `CAPABILITIES`, like the kill switch. It is read at D2, after `_record_planning` has folded the registered set, so registering it would leave every run row with a null `cap_snapshot` and no error to say why — and it would move a *shipment's* `cap_fingerprint` because the chatbot got a guard. It reaches `capability_evaluations` like everything else; it is not in `cap_snapshot`.
- LaunchDarkly is the sole source of truth for the capability flags, evaluated live, each under its own stage context (`planner`→`address_repair`, `validation`→`address_validation`, `verification`→`manifest_verification`). There is no committed capability config — no profiles, no prerequisites, no `resolve` proposal layer. Offline, each falls back to a code default (planner off, validation standard, verification off), which is what `baseline` now means. `profile` survives only as a targeting label sent to LD.
- The kill switch is `pipeline-kill-switch`, an LD flag evaluated at A1 under `stage: run_init`. It fails **open** (default off) when LD is unreachable, so an outage does not brick an offline run — the system spends no money either way, so stopping is not a safety-critical open call.
- Ledger is append-only JSONL. DuckDB is derived and rebuildable. Never write DuckDB as source of truth.
- A ledger line is a partial update, not a row. To change a value, append another record with the same merge key. Never edit or delete a line.
- Ledger timestamps are UTC. Naive datetimes are rejected, not assumed.

## Secrets
- Secrets live in `.env` (gitignored). `.env.example` is the committed template and holds no real values.
- On Claude Code on the web / iPad, `.env` is not present — the container is cloned fresh. Set `LD_SDK_KEY`, `SHIPPO_API_KEY` and `ANTHROPIC_API_KEY` as environment variables on the Claude Code environment (env config in the web app), never in chat. See https://code.claude.com/docs/en/claude-code-on-the-web.
- Never in LD agent instruction text. The run-start snapshot commits that text to the repo.
- Never in a ledger record, especially `cap_snapshot` or a `capability_evaluations` line. The ledger is committed and append-only, so a secret written there cannot be removed by a later append.
- `flag_payload`, `cap_snapshot` and every `capability_evaluations.value` record the *coerced* capability value, never the raw LD payload. `_coerce` maps every served value through a `StrEnum`, so the recorded dict is structurally incapable of carrying free text — a value that is not an enum member is discarded in favour of the default and only its rejection reason is recorded. A test pins that: adding a capability whose values are not an enum breaks it rather than silently widening what reaches the ledger.
- `UV_ENV_FILE` (devcontainer) makes `uv run` load `.env`, and setup.sh seeds `.env` from `.env.example`. So an unconfigured key is present-but-empty, not absent: check `if not os.environ.get("LD_SDK_KEY")`, never `is None`. Treat empty as unconfigured and take the offline gate (the per-capability defaults) rather than handing `""` to the LD SDK.

## Boundaries
- LD holds agent instruction text, model params, the capability flag values, and the kill switch. Nothing else — no control flow.
- Python holds all control flow, tool definitions, tool execution, and how a served flag value is coerced, defaulted, cached and recorded.
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
- `src/bbq_shipment_agent/agents/` — model.py (the model-call seam), metrics.py (LD AI metrics), tools.py (contract + registry), verification.py (D1), narrator.py (D2), guard.py (D2's scope guard)
- `src/bbq_shipment_agent/recipients/` — record.py (`Recipient`, phase B's type), extraction.py (B1), roster.py (the run input file), validation.py (B2), repair.py (B3), dedupe.py (B4)
- `src/bbq_shipment_agent/planning/` — catalog, rates (Shippo seam), configurations (C2), thermal (C3), lanes (ambient), remediation (C4), solve (C5), manifest (C6)
- `src/bbq_shipment_agent/review.py` — D2 edit handling and terminal states
- `src/bbq_shipment_agent/capabilities.py` — the capability enums, the `CAPABILITIES` registry (name→flag key→enum→stage→default), the `FlagGate` seam (`LaunchDarklyGate`/`OfflineGate`), `evaluate_capability`/`evaluate_kill_switch`, the value coercion, and `CapabilitySet` (snapshot + fingerprint)
- `src/bbq_shipment_agent/context.py` — `ContextBuilder`, the only place a LD context is constructed (run / stage / image / user kinds), `ImageIdentity`, reason codes
- `src/bbq_shipment_agent/operators.py` — `OperatorPool`, the only source of a user/department pair. A key is an input; a department never is.
- `src/bbq_shipment_agent/agent_configs.py` — AI Config retrieval, instruction hash, snapshot / offline cache
- `src/bbq_shipment_agent/hashing.py` — the one hashing convention. Everything that hashes routes through it.
- `src/bbq_shipment_agent/run.py` — A1 initialize_run, the `Run` (which evaluates capabilities live per stage through its gate and caches them), LD client bootstrap
- `src/bbq_shipment_agent/wiring.py` — `RunOptions`, `Progress`, and the *only* place a live client is constructed. Both front-ends go through it.
- `src/bbq_shipment_agent/ui/` — the local web app: app.py (routes), service.py (worker thread + events), view.py (results as plain data), templates/
- `src/bbq_shipment_agent/drive.py` — `bbq-shipment-agent drive`: an HTTP client that posts runs at a serving `ui` on an interval, varying the screenshot subset and the operator. Live testing, not a pipeline path.
- `config/capabilities.yaml` — **removed.** Capability flags and the kill switch live in LaunchDarkly now; there is no committed capability config. Named "profiles" move into LD targeting rules keyed on the `profile` attribute.
- `config/operators.yaml` — the user/department pool the `user` context kind is keyed on. Committed: who a run claims to be changes what LD serves it. No recipients, ever — these keys go to LD.
- `config/lanes.yaml` — ambient per destination band + month. Stated assumptions, never measured; an unmapped state takes the *hottest* band on purpose.
- `config/ld-snapshot.json` — committed AI Config snapshot. Audit trail and offline cache in one file.
- `ledger/*.jsonl` — the committed source of truth. `ledger.duckdb` is derived and gitignored.
- `recipients.yaml` — the run input. Gitignored (home addresses); `recipients.example.yaml` is the template.
- `.cache/` — live Shippo answers, gitignored. A cache of an API, not a run artifact.
- `uv run pytest`, `uv run bbq-shipment-agent ledger verify|rebuild|tools`, `uv run bbq-shipment-agent run init|plan|review`, `uv run bbq-shipment-agent ui`, `uv run bbq-shipment-agent drive`

## Front-ends
- Two: the CLI and `ui`. Neither sequences a stage. Both build a `RunOptions` and call `wiring.open_run` then `wiring.plan_with`, so an offline fallback or a cache path cannot drift between them.
- `wiring.py` is the only module that constructs something which opens a socket. If a new live client appears anywhere else, that claim is dead and the "no test can open a socket" property goes with it.
- The UI binds 127.0.0.1 and there is no host flag. It has no authentication because nothing off the machine can reach it, and it serves real home addresses and screenshots of private messages. Adding a host option changes that trade silently.
- The form configures a run. It does not configure the system: no control for the 4.4C threshold, the carrier cap, or the ledger path. The kill switch is an LD flag now, not a form field or a repo toggle. A field for the ledger is a way to append a real run to the wrong file.
- `/screenshots/{name}` serves from a dict built by globbing the resolved directory, keyed by exact filename. Never `dir / name`.
- One run at a time, refused rather than queued: two would append to the same ledger and quote the same lanes twice.
- Which images B1 read go on the run row (`evaluation_reasons["screenshots"]`). A seeded sample is reconstructible from its seed; a set picked by hand is reconstructible from nothing.
- Replay is all or nothing. `RunOptions.replaying` is what the page calls "no live calls", and B1/B3 need `--extractions`/`--repairs` for it to be true on a screenshot run.
- Depth is `RunOptions.depth`, and `wiring.run_with` is the *only* thing that reads it — a test pins that. The CLI says it with the `run extract` subcommand and the form with a field; both arrive at `extract_with`, so neither front-end owns its own idea of where a run stops. A browser copy of that path is a copy that forgets `record_run_reasons` and orphans every B1 hash.
- `RunDepth.EXTRACT` stops after B1 — inside `build_roster`, the one place the pipeline naturally ends. It never reaches C2, which is what makes a replayed screenshot run fully offline: the lane limit in design 10 is a fact about the quoter, and extract never calls it.
- `drive --depth extract` is the cheap session: one vision call per image, no quote, no D1. B1 is the only stage a rollout can bucket on, so depth and screenshot subset are the two axes worth varying.
- `drive` is a *client* of the UI, not a third front-end: it posts the form and the app decides what it means. It builds no `RunOptions` and touches no stage, which is what keeps "two front-ends" true. `HttpTransport` is the only socket it opens and everything else takes a transport injected, so its tests drive a whole session against a fake.
- What `drive` varies is the screenshot subset, because design 6.6 makes the image the only unit a rollout can bucket on. Runs differing only in start time measure nothing. `--every` is a floor on starts: the server refuses a concurrent run, so the driver waits and reports what it was refused.

## Capability rules
- LD is the sole source of truth for `planner-mode`, `validation-mode`, `verification-enabled` — see `CAPABILITY_FLAGS`. Each is evaluated live through the `FlagGate` (`LaunchDarklyGate` online, `OfflineGate` offline), under its own stage context, at the point the stage that reads it runs. There is no profile file, no `resolve` proposal layer, no prerequisites.
- The run caches the first evaluation per capability and appends a `capability_evaluations` ledger line; a second ask returns the cache. So one run folds exactly one value per capability into one `cap_snapshot` and one `cap_fingerprint`, even though evaluation is per stage.
- Offline (no SDK key, or LD unreachable) the gate serves the code default: planner off, validation standard, verification off. That is what `baseline` means now — a set of defaults, not a committed profile.
- A served value that is not an enum member is discarded in favour of the default; the reason records `FLAG_VALUE_INVALID:<value>` so a console placeholder gets fixed rather than aborting a shipping run. `_coerce` also maps a JSON boolean onto `on`/`off`.
- `cap_fingerprint`, `cap_snapshot` and `flag_payload` land on the run row at **planning** time, not A1 — A1 no longer knows the capabilities, since each is evaluated when its stage runs. A1 opens the row with `started_at` and `profile` only. `_record_planning` appends the folded set. Shipment rows carry `cap_fingerprint` pointing at the run.
- The kill switch is `pipeline-kill-switch`, evaluated at A1 under `run_init` through the same gate, fail-open (default off) offline. An engaged switch aborts before any config fetch or ledger append, so an aborted run writes nothing.
- B1's config is retrieved once per screenshot, under an `image` context keyed on the file's content hash — the only unit a rollout can mean anything on, since `run.key` is a fresh UUID. Screenshots are therefore resolved before A1, not in `build_roster`. B1 records one invocation per image, carrying `image_key`.
- The `user` kind is keyed on a username with `department` as its one attribute, and it is on *every* evaluation in the run — a rule targeting it has to reach D2, not just A1. It exists because `run.key` is a fresh UUID and a username is not: it is the second key in the system a rollout can bucket on, and the only other one is the image hash. `department` is an attribute, not a kind, because nothing buckets on a department.
- A department is looked up in `config/operators.yaml`, never supplied. The CLI, the form and the driver all name a key; an unknown one is an error. Two front-ends posting different departments for one username is how a `department is "kitchen"` rule ends up describing whatever was typed last.
- `drive` learns the operator pool by parsing the form (`data-operator`), not by reading the config file — it is a client of the UI, and a pool it read itself could offer a key the running app would refuse.
- LaunchDarkly is never given a filename. The `image` kind is key-only and the key is a content hash; `evaluation_reasons["screenshot_keys"]` on the run row is what resolves it locally.
- A variation the canonical snapshot entry does not hold is archived under `agent-key#variation-key`, so every `instruction_hash` in the ledger has bytes committed in the repo. The offline reader looks up bare agent keys and cannot serve one back.
- `ContextBuilder` is the only thing that constructs an evaluation context, and `context.py` the only module calling `Context.from_dict`. A second construction site is how the run and stage contexts drift apart, which is what a percentage rollout cannot survive. Context attributes are declared per kind in `_ATTRIBUTES`; an undeclared one is refused, not forwarded, because a context is sent to LD's servers.
- Both A1 sources default to offline. Live LD is injected, never reached for, so no test can open a socket.
- Query nested ledger JSON with `json_extract_string(...)`, not `->>` — DuckDB mis-resolves that operator inside a compound predicate.

## The D2 scope guard
- `narration-scope` is a LaunchDarkly **judge** config, not an agent config. It is not in `LD_CONFIGURED_STAGES`, needs no `TOOL_NAMES` entry, and is fetched with `create_judge`, not `variation_detail`.
- The SDK does the scoring. `judge.evaluate(history, reply)` owns the model call, the prompt framing, the output schema and the 0.0–1.0 validation. Do not reimplement any of it — an earlier draft did, and that is the duplication this repo keeps having to remove.
- It judges the **response**, not the question. An in-scope question can still produce a wandering answer, and only checking the output catches both.
- Only one path suppresses: judge ran, returned a score, score below `THRESHOLD`, mode `enforce`. Unavailable judge, sampled-out, errored, missing score — all pass and are recorded. A guard that blanks a legitimate narration is worse than the answer it prevents.
- The threshold is a Python constant. A threshold is control flow; the mode, model and rubric are LD's.
- `enforce` turns off streaming. A reply that must be judged before the operator sees it cannot already be on their screen, so the pane drops `data-stream` and posts to the sync route.
- A suppressed turn rolls back **whole** — prompt and answer both leave `self.messages`. There is no input-side gate, so the prompt may itself be off-topic, and keeping it re-primes the model into suppressing forever.
- `open()` is never judged. `OPENING_PROMPT` is Python's text asking for the manifest narration, so it is in scope by construction.
- The score goes in the ledger; the reasoning never does. Model-authored free text in an append-only committed file is what the capability coercion exists to prevent.

## Agent invocation
- A model parameter LD serves may be refused by the provider (`temperature` is deprecated for Sonnet 5). `AnthropicModel` drops it, retries once, and reports it on `Completion.dropped_parameters`. Nothing validates parameters at startup — design 10.
- LD supplies instructions, model name, and model parameters. Nothing in `agents/` hardcodes a prompt or a model.
- Identity for the ledger record and the LD metric both come from the config captured at A1. Never a fresh lookup at invocation time.
- The rendered template goes to the model; the un-rendered one is what gets hashed and snapshotted.
- An unknown model parameter from LD is dropped, not forwarded. A console typo must not become a TypeError mid-run.
- `verification-enabled` off is a normal run, not a degraded one. Skipping writes no invocation record; an invocation that *ran* is always recorded, including when its reply could not be parsed.
- LD metric success is about the invocation, not the manifest. An agent reporting six blockers succeeded.
- An invocation with a tool loop records `tools_offered` and `tools_called`; one without records neither. Offered comes from the loop that ran, never from `TOOL_NAMES` — B3 is offered less on a run with no screenshots, and a line claiming otherwise turns a tool that was absent into one the model declined. `ledger tools` reads them back.
- Five metrics reach LD per invocation: tokens, duration, time to first token, tool calls, and success/error. Four of them come from the SDK: `metrics.record(work, outcome)` wraps the whole invocation in `tracker.track_metrics_of`, which times it and reports tokens, tool calls and success/error from the returned `Outcome`. Do not hand-roll those again — that is what this file already did once. Time to first token is the exception and is measured by hand, because `LDAIMetrics` has no field for it and no extractor can carry it; `SdkMetrics` dedups that one so a tool loop's later iterations do not make the tracker warn. An empty `tools_called` is dropped before the SDK sees it: the SDK reports whatever list it is handed, and B1/D1 have no tool loop to report.
- Duration is the *whole* invocation, tool round trips included, because that is what the operator waits for and design 6.2 wants it attributable per variant. `metrics_for` starts the clock, so call it at the top of the invocation, not just once inside it.
- Time to first token is why `AnthropicModel` streams; a non-streamed call has no first token to time. Measured at the first `content_block_delta`, never at `message_start`, which carries no content. Replayed completions report `None` and send nothing — an unmeasured latency is not a fast one.
- Tool calls reported to LD are `tools_called`, never the offered set, and they keep order and repeats like the ledger line. B1 and D1 report nothing rather than an empty list.
- `track_feedback` stays unwired on purpose. D2's terminal state judges the *plan* C5 computed, not the narration, and design 8 already names the right metric for `review-narrator` — operator edit count, which is a custom metric.

## Agent configs
- Hash and snapshot the *un-rendered* template. Rendered text carries `ldctx` (recipient data) into committed files, and its hash differs every run, so it discriminates nothing.
- Snapshot on `available`, never on "no exception". A served-but-disabled config is a real answer from LD and must not fall through to stale cached text.
- An unreachable run never overwrites the snapshot. That cache is the fallback precisely when LD is down.
- Invocation records take their identity from the config captured at A1, never a fresh lookup.
- All four configs exist in LD and are snapshotted. Only the *config key* must match code (`AGENT_STAGES`); variation keys are free-form but must be non-empty, or `launchdarkly_metrics` silently drops to `NoMetrics`.
- Create AI Configs in **Agent mode**, even `screenshot-extraction`, which is not an agent. `_from_variation` reads `instructions`; a Completion-mode config carries `messages` and comes back `unavailable`.
- Declare a tool on an AI Config only in the same change that registers it in Python. Declaring fewer than Python offers is not drift; declaring one Python lacks is the failure 6.4 mitigation 1 exists to catch. All four currently declare none, and `manifest-verification` stays that way — it is read-only by design.
- Instruction text lives in LD, not in the repo. Drafting a change in a scratch file is fine; committing it is not. Two copies where one is unserved is the drift the snapshot exists to detect.
