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

### Load the keys — do this once per shell

```bash
export UV_ENV_FILE=$PWD/.env
```

**Nothing that touches a key works without this.** `uv run` reads `.env` only
when `UV_ENV_FILE` points at it. `.devcontainer/devcontainer.json` sets it via
`remoteEnv`, but plenty of shells never see that — a new terminal, a
non-interactive shell, `nohup`, a task runner. Skip it and every key reads as
empty: LaunchDarkly quietly falls back to the `baseline` profile, while Shippo
and Anthropic fail outright.

Confirm it took:

```bash
uv run python -c "import os; print({k: bool(os.environ.get(k)) for k in ('LD_SDK_KEY','ANTHROPIC_API_KEY','SHIPPO_API_KEY')})"
# {'LD_SDK_KEY': True, 'ANTHROPIC_API_KEY': True, 'SHIPPO_API_KEY': True}
```

Three `False` values mean the file was not loaded, not that the keys are
missing. If you would rather not export, prefix each command instead:
`UV_ENV_FILE=$PWD/.env uv run ...`.

## Commands

Run these from the repo root, in a shell where the export above has been done.

```bash
uv run bbq-shipment-agent run init      # A1 only: resolve capabilities, snapshot AI Configs
uv run bbq-shipment-agent run plan      # A1 -> B -> C -> D1, prints a verified manifest
uv run bbq-shipment-agent run review    # the above, then the D2 conversation
uv run bbq-shipment-agent ui            # the same thing in a browser, with a screenshot picker
uv run bbq-shipment-agent drive         # fire runs at a running `ui`, one every 30s
uv run bbq-shipment-agent ledger verify # parse every JSONL line, no database
uv run bbq-shipment-agent ledger rebuild
```

`run plan`, `run review` and `ui` all default `--ledger` to the committed
`ledger/`, so a trial run appends a real row to it. Point them at
`--ledger /tmp/scratch` while you are experimenting. Paths are relative to the
working directory, so the repo root is also where `recipients.yaml`,
`config/` and `tests/fixtures/screenshots` resolve from.

## The web UI

```bash
export UV_ENV_FILE=$PWD/.env                                    # once per shell
uv run bbq-shipment-agent ui --ledger /tmp/ui-ledger            # http://127.0.0.1:8765
```

Both parts of that matter, and the UI is where forgetting either one hurts
most:

- **Without `UV_ENV_FILE`** the server starts perfectly and the page renders,
  and you only find out there are no keys after picking images and clicking.
  It now says so at launch and in a banner next to the button — but the fix is
  to load the file.
- **Without `--ledger`** a trial run appends to the committed `ledger/`. A
  button feels cheaper to press than a command is to type, and the append is
  just as real.

**A live run costs real calls**: one vision call per selected screenshot, plus
a Shippo validation per address and a few hundred rate quotes. The picker
therefore starts with **nothing selected** and the button disabled, and it
shows the call count as you tick — because the number you are about to spend
should be on screen before you press it, not in a log afterwards.

Pick which screenshots B1 reads by looking at them, then generate a manifest.
That is the one thing a terminal cannot do: `--screenshot-count 3` takes three
of seven and prints which three, but you cannot *choose* three without seeing
them. The picker captions each image from `ground_truth.json` where there is
one — how many recipients it holds, and how hard they are to read — so you can
aim a run at the awkward cases rather than sampling blind.

The page shows what the CLI prints, in the shape it should have been in all
along: the capability header and the served AI Configs stay at the top instead
of scrolling past, the manifest is grouped by ship date, and the runner-up
carrier pairs sit beside the chosen one. Stage progress streams while the run
goes, because a real run makes a vision call per image and a few hundred rate
quotes.

It stops where `run plan` stops. There is no approval button — D2 is still the
`run review` conversation — and no control for the 4.4C threshold, the carrier
cap or the authority level, because those are a Python constant, a Python
constant and a committed config file.

It binds `127.0.0.1` with no host option and has no authentication. That is the
trade: nothing off this machine can reach it, and it serves real home addresses
and screenshots of people's private messages.

To drive it without spending anything — no keys needed at all, so the export
above is irrelevant here — launch it with the recordings:

```bash
uv run bbq-shipment-agent ui --offline --ledger /tmp/ui-ledger \
  --recipients tests/fixtures/roster-sf-dc.yaml \
  --quotes tests/fixtures/shippo-quotes-sf-dc.json \
  --validations tests/fixtures/shippo-addresses.json \
  --completions tests/fixtures/d1-completions.json \
  --extractions tests/fixtures/b1-extractions.json \
  --repairs tests/fixtures/b3-repairs.json
```

The **replay recordings** checkbox is then on by default; unchecking it makes
the same run live.

One honest limit. `--extractions` and `--repairs` replay B1 and B3 for any
subset of the fixture screenshots — the recording is matched on image content,
so reading two of seven replays the right two. But
`shippo-quotes-sf-dc.json` holds one lane, San Francisco to Washington, and the
22 people in those screenshots live in twenty-odd other places. A replayed
screenshot run therefore gets as far as C2 and stops, because `RecordedQuoter`
refuses to invent a rate it never recorded — which is the behaviour you want.
Fully offline works today for the **roster** path (`--recipients
tests/fixtures/roster-sf-dc.yaml`, then tick "all" with no screenshots).
Screenshots plus recorded quotes needs those lanes recorded first.

### Driving it: many runs, over time

`drive` is a client of a UI that is already serving. It posts the same form a
browser posts, so a driven run and a clicked one are the same run — nothing
about the pipeline is reachable from the driver.

```bash
# terminal 1
UV_ENV_FILE=$PWD/.env uv run bbq-shipment-agent ui --screenshots tests/fixtures/screenshots --ledger /tmp/drive-ledger

# terminal 2
uv run bbq-shipment-agent drive --every 30 --runs 20 --vary sample --campaign aug-load
```

That starts a run every 30 seconds, each reading a different random subset of
the screenshots. The subset is the point: design 6.6 makes the **image** the
only unit a LaunchDarkly rollout can bucket on in this system, so runs that
differ only in wall-clock time exercise the server and measure nothing.

| what varies | flag |
|---|---|
| a random sample, with the seed printed | `--vary sample` *(default)* |
| a random named subset | `--vary explicit` |
| the whole directory, every time | `--vary all` |
| no images at all, roster only | `--vary roster` |
| the capability profile, cycled one per run | `--profiles baseline,planner_trial` |
| a numbered campaign on the run context | `--campaign aug-load` |

`--every` is a **floor on starts, not a promise**. The app runs one at a time
and refuses the second, so the driver waits for the run in flight and fires
when the interval has elapsed — which for a live screenshot run is usually
later than the interval. The tally at the end says how many were refused.
`--seed` makes a whole session repeatable, and `--replay` uses whatever
recordings the server was launched with, which is the free way to test the
driver rather than the pipeline.

Each of these runs costs real money by default: a vision call per screenshot,
Shippo quotes, and a D1 call. Twenty of them is twenty times that.

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
| `src/bbq_shipment_agent/wiring.py` | `RunOptions` and the only place a live client is built |
| `src/bbq_shipment_agent/ui/` | the local web app: routes, worker thread, view model, templates |
| `src/bbq_shipment_agent/recipients/` | phase B — extraction, validation, repair, dedupe |
| `src/bbq_shipment_agent/planning/` | phase C — catalog, rates, configurations, thermal, solve, manifest |
| `src/bbq_shipment_agent/review.py` | D2 edit handling and terminal states |
| `src/bbq_shipment_agent/agents/` | model seam, tools, D1 verification, D2 narrator |
| `src/bbq_shipment_agent/ledger/` | append-only JSONL, DuckDB rebuild |
| `config/capabilities.yaml` | profiles, permission flags, authority ceiling |
| `config/lanes.yaml` | ambient assumptions per destination band and month |
| `ledger/*.jsonl` | committed source of truth; `ledger.duckdb` is derived |
