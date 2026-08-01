# Ledger

The source of truth. Append-only JSONL, committed.

| File | Contents |
|---|---|
| `runs.jsonl` | one entity per run |
| `shipments.jsonl` | one entity per (run, recipient) |
| `agent_invocations.jsonl` | one event per agent invocation, never folded |

Each line is a *partial update*, not a row. To change or complete a value,
append another line with the same merge key and only the fields you know;
the last non-null value in `seq` order wins. Never edit or delete a line.

`ledger.duckdb` at the repo root is derived from these files, gitignored, and
rebuilt from scratch on demand:

```
uv run bbq-shipment-agent ledger verify    # parse-check the JSONL, no database
uv run bbq-shipment-agent ledger rebuild   # drop and rebuild the cache
```

Query `<stream>` for current entities and `<stream>_log` for every append.

See docs/design.md section 7.
