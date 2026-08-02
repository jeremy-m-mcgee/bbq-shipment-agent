# Agent instruction drafts

**These are staging documents, not the source of truth.**

Under the medium split (design 6.1) LaunchDarkly holds the instruction text
for all four agents. A file here exists only so text can be drafted, reviewed
and argued about in a pull request before it is pasted into an AI Config.

Once the config exists in LaunchDarkly, `config/ld-snapshot.json` carries the
authoritative text, and the draft here is **deleted**. That already happened
for `manifest-verification`: the draft was removed in `30cc115` once the
snapshot superseded it. Two copies of an instruction set, one of which is not
served to anything, is exactly the drift design 6.4 exists to prevent.

## Creating a config in LaunchDarkly

Each agent is a separate AI Config in Agent mode, keyed exactly as Python
expects — the keys are in `agent_configs.AGENT_STAGES` and must match
character for character:

| Config key | Stage | Build order step |
|---|---|---|
| `manifest-verification` | D1 | done |
| `address-repair` | B3 | 7 |
| `review-narrator` | D2 | 8 |
| `infeasibility-remediation` | C4 | 9 |

For each, set: the instruction text, the model, and (optionally) model
parameters. Python reads all three off the config and holds none of its own.

## Variation keys are yours to name

Only the *config key* has to match anything. The variation key is pass-through
everywhere it appears — parsed from `_ldMeta.variationKey`, stored on the
config, written to the snapshot, recorded as `instruction_variation_key` on
the ledger line, and handed to the metrics tracker. Nothing compares it to a
literal, and `standard-prompt` is a name the first config happened to get, not
a convention the code enforces.

It must be non-empty: `launchdarkly_metrics` returns `NoMetrics()` when the
variation key or version is missing, so an unnamed variation silently loses
console-side attribution.

Prefer names that describe the *difference*. Two `address-repair` variations
called `strict-escalation` and `lenient-escalation` tell you something in a
metrics comparison; `standard-prompt` and `variation-2` do not. Per-variant
attribution without a code change is one of the reasons design 6.1 gives for
putting instruction text in LaunchDarkly at all, and it is only worth
anything if the names carry meaning.

## Leave the tools list empty for now

Every draft here names the tools its agent expects, because the instructions
have to talk about them. **Do not declare those tools on the AI Config yet.**

Design 6.4 mitigation 1 asserts the declared set against the tools Python
actually registers, and Python registers none. Declaring fewer tools than
Python offers is not drift and never aborts a run; declaring one Python does
not offer is precisely the failure the assertion exists to catch. Add the
declarations in the same change that registers the tools in Python — that is
the guarantee the medium split gave up and this buys back.

`manifest-verification` is the exception that will stay empty forever: it is
read-only by design (6.2), and D1 already refuses to run if its config
declares any tool.

## After creating one

Run `bbq-shipment-agent run init` (or `run plan`). It pulls every config,
writes `config/ld-snapshot.json`, and prints what LaunchDarkly served. Commit
the snapshot — it is the audit trail and the offline cache in one file
(6.4 mitigation 3, 6.10 step 1). Then delete the draft.
