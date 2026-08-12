# Contributing

This repo has strong opinions and most of them are written down. Before a
change of any size, read [`docs/design.md`](docs/design.md) — it records not
just what the system does but which alternatives were tried and rejected, and
several things that look like omissions are decisions with an argument behind
them.

## Setup

**uv is the supported installer.** `uv.lock` is committed, so it is the only
path that installs the exact versions this project is tested against. pip and
poetry both work and the [README](README.md#setup) documents them — they
resolve their own versions, which is why they are documented as working rather
than as equivalent. CI runs uv.

```bash
uv run pytest                  # 803 tests, ~13s, no API keys needed
uv run ruff check .
```

`uv run` installs from `uv.lock` on first use, so there is no separate sync
step.

**Add a dependency with `uv add <pkg>`, never by hand-editing `pyproject.toml`
or by `pip install`ing into an environment.** That is the rule the lockfile
depends on: anything else leaves `uv.lock` describing a set nobody is running.
Don't regenerate the lock casually either — CI runs `uv sync --locked` and
fails if it has drifted from `pyproject.toml`.

If you develop on pip or poetry, that is fine, but the dependency change still
has to land as a `uv add` so the lock moves with it. Poetry writes a
`poetry.lock` of its own resolution; it is gitignored deliberately, because a
second lockfile in the tree is a second answer to the same question.

Nothing in the test suite makes a network call. Live clients are injected
rather than reached for, so the suite passes with `LD_SDK_KEY`,
`SHIPPO_API_KEY` and `ANTHROPIC_API_KEY` all unset. If a change makes a test
need a key, that change has put a socket somewhere it doesn't belong.

## Pull requests

`main` requires a pull request and a green CI run. Branch, push, open a PR.

Write the commit message for someone reading `git log` in a year. This repo's
history explains *why*, and that convention is worth keeping.

## Things that are not open to change

These are constraints, not defaults, and a PR that relaxes one will be
declined regardless of how it is implemented:

- **The 4.4C arrival threshold** is a constant in code. Not a flag, not a CLI
  argument, not an environment variable. It is a food safety limit.
- **At most two carriers per run.** Operational simplicity at drop-off.
- **The system spends no money.** No stage buys a label, or makes any
  irreversible external call. This is the guarantee that replaces a
  permission system — see design 6.5 and 9. A stage that acts on the world
  would need its own permission model and is a design conversation, not a PR.
- **The ledger is append-only.** A line is a partial update, not a row. To
  change a value, append another record with the same merge key. Never edit
  or delete a line. DuckDB is derived and rebuildable, never the source of
  truth.

## Things that are easy to get wrong

- **Agent instruction text lives in LaunchDarkly, not in this repo.**
  Drafting a change in a scratch file is fine; committing it is not. Two
  copies where one is unserved is exactly the drift the run-start snapshot
  exists to detect.
- **LaunchDarkly holds values, never control flow.** If a change would
  express stage sequencing, retries or error handling as LD config, stop and
  open an issue instead.
- **No secrets in a ledger record, ever.** The ledger is committed and
  append-only, so a key written there cannot be removed by a later append.
  The same goes for LD instruction text, which the run-start snapshot commits
  to the repo.
- **No third-party personal data in anything committed.** `recipients.yaml`
  is gitignored because it holds home addresses; `recipients.example.yaml` is
  the template. Screenshot fixtures are synthetic, rendered from
  `tools/screenshot_fixtures/specs/`. LaunchDarkly is never given a filename
  or a recipient — contexts are keyed on content hashes and operator
  usernames.
- **Every capability must be read by a stage.** A flag nothing consults is
  decorative and a test pins the set. Two capabilities were removed for
  failing that check (design 6.5).

## Reporting something

Issues are welcome, including "this doc claims something the code doesn't do"
— several open issues are exactly that, and they're among the more useful
ones.
