Full design: @docs/design.md

## Environment
- uv only. Use `uv add <pkg>`, never `pip install`.
- Run commands with `uv run`, or rely on the venv already being on PATH.
- uv.lock is committed. Do not regenerate it casually.

## Hard rules
- 4.4C arrival threshold is a constant in code. Never a flag, CLI arg, or env var.
- authority_ceiling lives in config/capabilities.yaml. LD can lower authority, never raise it.
- Max 2 carriers per run.
- No label purchase without explicit human approval.
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

## Agents (4, independent, no handoff)
address-repair (B3) | infeasibility-remediation (C4)
manifest-verification (D1) | review-narrator (D2)

## Build order
See docs/design.md section 11. Step 1 (ledger) done. Step 2 done bar the
invocation itself — capabilities, clamp, prerequisites, context, A1, live LD
provider, AI Config retrieval, instruction hash, run-start snapshot.
`record_agent_invocation` is written and tested but has no caller: nothing
invokes `manifest-verification` until step 3's spine produces a manifest to
check. Step 3's stages are all built — B2, B4, C1–C6. What is left is the
wiring: one entry point that runs A1 → B2 → B4 → C1–C6 from a recipient file
and writes the manifest.

## Layout
- `src/bbq_shipment_agent/ledger/` — schema.py (records), writer.py (append-only JSONL), rebuild.py (DuckDB cache)
- `src/bbq_shipment_agent/recipients/` — validation.py (B2), dedupe.py (B4)
- `src/bbq_shipment_agent/planning/` — catalog, rates (Shippo seam), configurations (C2), thermal (C3), solve (C5), manifest (C6)
- `src/bbq_shipment_agent/capabilities.py` — config load, ceiling clamp, prerequisites, fingerprint
- `src/bbq_shipment_agent/context.py` — LD multi-context (run / stage / shipment), reason codes
- `src/bbq_shipment_agent/agent_configs.py` — AI Config retrieval, instruction hash, snapshot / offline cache
- `src/bbq_shipment_agent/hashing.py` — the one hashing convention. Everything that hashes routes through it.
- `src/bbq_shipment_agent/run.py` — A1 initialize_run, `CapabilityProvider` seam, LD client bootstrap
- `config/capabilities.yaml` — profiles + permission flags. Quote `off`/`on`: YAML 1.1 reads them as booleans.
- `config/ld-snapshot.json` — committed AI Config snapshot. Audit trail and offline cache in one file.
- `ledger/*.jsonl` — the committed source of truth. `ledger.duckdb` is derived and gitignored.
- `uv run pytest`, `uv run bbq-shipment-agent ledger verify|rebuild`

## Capability rules
- A provider proposes; the repo decides. Order is fixed: profile → flag overrides → ceiling clamp → prerequisites.
- Never clamp before applying overrides, or a payload could survive the ceiling.
- Unmet prerequisites demote and record why. They never abort the run.
- LD serves `planner-mode`, `memory-mode`, `verification-enabled` and nothing else. `authority-level` is never asked for — see `CAPABILITY_FLAGS`.
- An absent flag proposes nothing. It is not an instruction to overwrite the profile with a default.
- Both A1 sources default to offline. Live LD is injected, never reached for, so no test can open a socket.
- Query nested ledger JSON with `json_extract_string(...)`, not `->>` — DuckDB mis-resolves that operator inside a compound predicate.

## Agent configs
- Hash and snapshot the *un-rendered* template. Rendered text carries `ldctx` (recipient data) into committed files, and its hash differs every run, so it discriminates nothing.
- Snapshot on `available`, never on "no exception". A served-but-disabled config is a real answer from LD and must not fall through to stale cached text.
- An unreachable run never overwrites the snapshot. That cache is the fallback precisely when LD is down.
- Invocation records take their identity from the config captured at A1, never a fresh lookup.
