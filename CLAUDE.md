Full design: @docs/design.md

## Hard rules
- 4.4C arrival threshold is a constant in code. Never a flag, CLI arg, or env var.
- authority_ceiling lives in config/capabilities.yaml. LD can lower authority, never raise it.
- Max 2 carriers per run.
- No label purchase without explicit human approval.
- Ledger is append-only JSONL. SQLite is derived and rebuildable. Never write SQLite as source of truth.

## Boundaries
- LD holds agent instruction text and model params. Nothing else.
- Python holds all control flow, tool definitions, and tool execution.
- If a change would express control flow in LD config, stop and ask.

## Agents (4, independent, no handoff)
address-repair (B3) | infeasibility-remediation (C4)
manifest-verification (D1) | review-narrator (D2)

## Build order
See docs/design.md section 11. Currently on step N.
