# Plan: stop hand-rolling what `track_metrics_of` already does

Status: proposed
Branch: `refactor-metrics-track-metrics-of`

## Context

`SdkMetrics` (`agents/metrics.py:121`) wraps an `LDAIConfigTracker` and calls
each metric method by hand: it times duration with its own `perf_counter`,
builds `TokenUsage` itself, and calls `track_success` / `track_error` at the
right moments.

LaunchDarkly's guides teach the other half of their integration pattern
alongside the direct provider call: wrap the call in
`tracker.track_metrics_of(extractor, func)`, which "records duration,
success/error, and token usage automatically". Their AgentControl best-practices
page documents the same split — a generic extractor-based method, or the
individual `track_*` methods for manual recording.

We are on the manual path for work the generic path covers. This was found while
planning the D2 scope guard (`narrator-scope-guard.md`), which was about to
inherit the same pattern into a new component.

**This is not a claim that the direct provider call is wrong.** LD's own OpenAI
guide teaches config-in / call-it-yourself / track, on the provider that *does*
ship a runner. `AnthropicModel` stays. Only the tracking half changes.

## What gets stripped

Four things in `SdkMetrics`, all replaced by `track_metrics_of`:

| Lines | Removed | Replaced by |
|---|---|---|
| `:135`, `:153-159` | `self._started = perf_counter()` and `track_duration()` computing elapsed ms | `track_metrics_of` times the call |
| `:140-151` | `track_tokens` building `TokenUsage` by hand | the extractor's `LDAIMetrics.tokens` |
| `:172-179` | `track_tools_called` → `track_tool_calls` | the extractor's `LDAIMetrics.tool_calls` |
| `:181-186` | `track_success` / `track_error` | `track_metrics_of` tracks both, including on exception |

What replaces them is one small extractor: `Completion` → `LDAIMetrics(success,
tokens, tool_calls)`. That is the same shape LD's Anthropic guide passes as
`anthropic_metrics`.

## What stays, and why

- **`track_time_to_first_token` (`:161-171`) and its at-most-once dedup.** TTFT
  is a first-class LaunchDarkly metric — it is in their metrics list and
  `track_time_to_first_token` exists on the tracker (`tracker.py:246`) — but
  **nothing computes it for you**. `LDAIMetrics` has four fields and TTFT is not
  one, so no extractor can carry it, and LD's own description of
  `track_metrics_of` covers duration, tokens and success/error only. Someone has
  to time the first delta and pass the milliseconds, which is why
  `AnthropicModel` streams.

  **This is the failure mode to design against.** Swapping wholesale to
  `track_metrics_of` and deleting the individual calls would stop TTFT being
  reported with no error — a metric that just goes flat in the console. The
  regression test below exists for exactly this.

- **`_safely` (`:188-190`).** Telemetry must never fail a run; `track_metrics_of`
  re-raises whatever the wrapped call raised, so the guard still belongs around
  reporting.
- **`NoMetrics`.** Unchanged offline no-op.

## The part that is not a swap

`track_metrics_of` wraps **exactly one callable**.

- **B1 and D1 are single-shot** — a straight swap around the one model call.
- **B3 and D2 span up to eight model calls per invocation.** The wrap has to go
  around the whole loop, with an extractor that aggregates tokens across
  iterations and reads `tools_called` off the finished turn. That means
  restructuring `Narrator.say` (`narrator.py:139-200`) and B3's loop so the loop
  body is a callable, rather than swapping method calls in place. TTFT is still
  reported separately from inside the loop, on the first iteration that has one.

Do B1 and D1 first; they are independent and prove the extractor shape before
the loop restructure touches the two stages with tool calls.

## Files

- `agents/metrics.py` — `SdkMetrics` shrinks to the extractor plumbing, the TTFT
  call, and `_safely`. `InvocationMetrics` (`:87-104`) loses four of its six
  methods, so every implementation and caller changes with it.
- `agents/verification.py` (D1), `recipients/extraction.py` (B1) — single-shot
  swap.
- `agents/narrator.py`, `recipients/repair.py` — loop restructure.
- `tests/test_metrics.py` — `FakeTracker` (`:42-73`) currently records the
  individual calls; it needs to record `track_metrics_of` invocations instead.

## Verification

- `uv run pytest`.
- **A test that TTFT is still reported.** Drive a streamed invocation through a
  `FakeTracker` and assert `track_time_to_first_token` was called with a
  non-`None` value. This is the regression the change is most likely to cause
  and the least likely to notice.
- A test that a raising model still reports an error and does not propagate the
  telemetry failure — the `_safely` property.
- A test that a replayed completion reports **no** TTFT rather than zero
  (design 8: an unmeasured latency is not a fast one).
- Live check against the LaunchDarkly console after one real run: tokens,
  duration, success and TTFT all still land for `review-narrator`. The point of
  the change is that the first three arrive from the SDK instead of from us; if
  any of the four is missing, the swap is incomplete.

## Not in scope

Two other pieces of SDK duplication were found alongside this one and are
deliberately left alone:

- **`_from_variation` hand-parses LD's wire format** (`_ldMeta`, `model.name`,
  `model.parameters`, `instructions`, `tools`) — work `agent_config()` already
  does. The repo calls both because the SDK does not expose `variation_key` or
  `version`, which design 6.4 mitigation 2 requires on every ledger line. The
  identity half is forced; whether the parsing half can go is a separate
  question.
- **The snapshot's offline-fallback half** overlaps
  `agent_config(key, ctx, default=AIAgentConfigDefault(...))`. The audit-trail
  half is not duplication — 6.4.3 needs committed bytes — so any change here has
  to keep that.
