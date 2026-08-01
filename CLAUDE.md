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
- Never in a ledger record, especially `cap_snapshot`. The ledger is committed and append-only, so a secret written there cannot be removed by a later append. Snapshot resolved capability values and the payload hash, not the raw LD payload.
- `UV_ENV_FILE` (devcontainer) makes `uv run` load `.env`, and setup.sh seeds `.env` from `.env.example`. So an unconfigured key is present-but-empty, not absent: check `if not os.environ.get("LD_SDK_KEY")`, never `is None`. Treat empty as unconfigured and take the `baseline` offline fallback rather than handing `""` to the LD SDK.

## Boundaries
- LD holds agent instruction text and model params. Nothing else.
- Python holds all control flow, tool definitions, and tool execution.
- If a change would express control flow in LD config, stop and ask.

## Agents (4, independent, no handoff)
address-repair (B3) | infeasibility-remediation (C4)
manifest-verification (D1) | review-narrator (D2)

## Build order
See docs/design.md section 11. Step 1 (ledger) is done. Next up is step 2.

## Layout
- `src/bbq_shipment_agent/ledger/` — schema.py (records), writer.py (append-only JSONL), rebuild.py (DuckDB cache)
- `ledger/*.jsonl` — the committed source of truth. `ledger.duckdb` is derived and gitignored.
- `uv run pytest`, `uv run bbq-shipment-agent ledger verify|rebuild`
