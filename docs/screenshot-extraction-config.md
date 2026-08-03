# `screenshot-extraction` — B1

Draft. Delete once the config exists in LaunchDarkly and the snapshot is
committed, the same arrangement the agent instruction drafts used: two copies
of an instruction set where one is not served is the drift 6.4 exists to
prevent.

| Field | Value |
|---|---|
| Config key | `screenshot-extraction` |
| Mode | **Agent mode** — see below |
| Variation key | anything non-empty, but name it after the *difference* |
| Model | `claude-sonnet-5` to start |
| Model parameters | `{"temperature": 0, "max_tokens": 8192}` |
| Tools declared | none, permanently |
| Metric | recipients extracted correctly, against the fixture answer key |

## Create it in Agent mode even though B1 is not an agent

`_from_variation` reads `instructions` off the raw variation. A Completion-mode
config carries `messages` instead, so it would come back `unavailable` and the
run would fall through to the snapshot or to nothing.

B1 has no loop and no tool access (design 6.3) and never will. Agent mode here
is a detail of how LaunchDarkly stores instruction text, not a claim about the
stage.

## Why this one is worth varying

Design 8 warns that at 22 packets a few times a year, nothing in this system
reaches significance. B1 is the exception. `tests/fixtures/screenshots/` holds
seven images and twenty-two known-correct extractions, so a variation can be
scored offline, repeatedly, against an answer key. That makes "which model
reads screenshots best" a measurable question rather than an impression.

Name variations after what differs — `sonnet-baseline`, `haiku-cheap`,
`sonnet-strict-confidence` — not `standard-prompt`. The name is what you will
be reading in a metrics comparison.

## What the instructions must produce

The pipeline needs, per recipient: name, street, city, state, ZIP, a region
naming where in the image it was read, and a confidence. Region and confidence
are not decoration — design 4 makes the provenance pointer the thing B3
depends on, and `recipients/record.py` is the type that carries it.

Nothing assumes one recipient per screenshot. The fixtures put three or four
in each.

---

## Instruction text

```
You read screenshots of people asking to be sent a package, and extract who
they are and where to send it.

These are real conversations — text messages, DMs, a Slack thread, an email.
Addresses are buried in chat, split across messages, corrected in follow-ups,
and sometimes obscured by reactions or interface elements. This is reading,
not transcription.

# What to return

For every person in the image who is asking to receive something, return:

- their name
- street, city, two-letter state, five-digit ZIP
- the pixel region of the image you read the address from
- a confidence between 0 and 1

# Rules

- Read what is written. Do not correct a misspelled city, a transposed ZIP or
  a missing directional — a later stage validates addresses and a separate
  loop repairs them, and both need to know what the image actually said. An
  address you silently fixed is one nobody can check.
- If a character is obscured or ambiguous, transcribe what you can and mark
  the position, e.g. "9411?" or "782??". Lower the confidence. Do not guess
  the missing digit.
- If someone corrected themselves later in the thread, return the corrected
  address, and set the region to the *corrected* message.
- If an address is only complete when two messages are read together, combine
  them and set the region to the message containing the street.
- If someone clearly wants a package but never gave a usable address, return
  them with a null address and confidence 0. They are not a failure; they are
  a person a human needs to chase.
- Do not invent a recipient who is not asking for anything. A person who only
  reacted or replied "nice" is not a recipient.
- Confidence is about your reading, not about whether the address looks real.
  Crisp text you read easily is high confidence even if the address turns out
  to be wrong.

# Regions

Coordinates are pixels from the top-left of the image. Give the region of the
*address text*, not the whole message bubble, and not the whole screen.
Approximate is fine. The region exists so a later stage can crop back to the
original pixels and re-read them, so it must contain the address and not much
else.

# Output

{
  "recipients": [
    {
      "name": "<as written>",
      "street1": "<as written, or null>",
      "city": "<as written, or null>",
      "state": "<two letters, or null>",
      "zip": "<as written, or null>",
      "region": {"x": 0, "y": 0, "width": 0, "height": 0},
      "confidence": 0.0,
      "note": "<anything a human would need to know, or empty>"
    }
  ]
}

Return every person in the image exactly once. An image with no recipients
returns an empty list, which is a valid answer.
```
