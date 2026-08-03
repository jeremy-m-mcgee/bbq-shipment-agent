# bbq-shipment-agent

Plans the assembly and shipment of frozen barbecue packets to a recipient list,
producing a reviewable work package that a human approves. It spends no money
and buys no labels — the operator does that by hand from an approved manifest.

Full design: [`docs/design.md`](docs/design.md). Working notes: [`CLAUDE.md`](CLAUDE.md).

## Setup

```bash
cp .env.example .env        # then fill in LD_SDK_KEY, ANTHROPIC_API_KEY, SHIPPO_API_KEY
cp recipients.example.yaml recipients.yaml
uv run pytest
```

`UV_ENV_FILE` is set by `.devcontainer/devcontainer.json` so `uv run` loads
`.env`. If your shell does not have it — some non-interactive shells do not —
prefix commands with `UV_ENV_FILE=$PWD/.env` or nothing will be configured.

## Commands

```bash
uv run bbq-shipment-agent run init      # A1 only: resolve capabilities, snapshot AI Configs
uv run bbq-shipment-agent run plan      # A1 -> B -> C -> D1, prints a verified manifest
uv run bbq-shipment-agent run review    # the above, then the D2 conversation
uv run bbq-shipment-agent ledger verify # parse every JSONL line, no database
uv run bbq-shipment-agent ledger rebuild
```

### Reviewing a plan, with recipients from screenshots

This is the D2 conversational flow. It plans a run, then opens a conversation
over the manifest where you can ask *why* the plan is what it is.

```bash
UV_ENV_FILE=$PWD/.env uv run bbq-shipment-agent run review --screenshots tests/fixtures/screenshots --screenshot-count 2 --screenshot-seed 1 --ledger /tmp/review-ledger
```

`--screenshots` makes B1 extract the recipients, so `recipients.yaml` supplies
only the origin, the candidate ship dates and any lane declarations.
`--screenshot-count` reads a random sample of that many images and prints both
the sample and its seed; `--screenshot-seed` re-reads the same sample.
`--ledger` points somewhere disposable so a trial approval does not append to
the committed `ledger/`.

At the `>` prompt:

```
approve | reject | exclude <key> | pin <key> <YYYY-MM-DD> | show | quit
```

**Anything else goes to `review-narrator`.** That is the "why" channel:

- Which constraint is doing the most work?
- Why is one shipment $80 and another $18?
- Why does one packet need 6 gel packs and another 4?
- What would we give up by dropping USPS?

Every claim it makes is supposed to trace to a computed field on the manifest.
Design 4 records what happens when one cannot.

## Offline and replay

Nothing in the library opens a socket — the CLI is the only place live paths
are assembled. To run without touching Shippo, LaunchDarkly or a model:

```bash
uv run bbq-shipment-agent run plan --offline \
  --quotes tests/fixtures/shippo-quotes-sf-dc.json \
  --validations tests/fixtures/shippo-addresses.json \
  --completions tests/fixtures/d1-completions.json
```

## Layout

| path | what |
|---|---|
| `src/bbq_shipment_agent/plan.py` | the spine: A1 → B2 → B4 → C1–C6 → D1 |
| `src/bbq_shipment_agent/recipients/` | phase B — extraction, validation, repair, dedupe |
| `src/bbq_shipment_agent/planning/` | phase C — catalog, rates, configurations, thermal, solve, manifest |
| `src/bbq_shipment_agent/review.py` | D2 edit handling and terminal states |
| `src/bbq_shipment_agent/agents/` | model seam, tools, D1 verification, D2 narrator |
| `src/bbq_shipment_agent/ledger/` | append-only JSONL, DuckDB rebuild |
| `config/capabilities.yaml` | profiles, permission flags, authority ceiling |
| `config/lanes.yaml` | ambient assumptions per destination band and month |
| `ledger/*.jsonl` | committed source of truth; `ledger.duckdb` is derived |
