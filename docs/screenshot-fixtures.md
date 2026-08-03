# Brief: test fixtures for B1 extraction

**Staging document.** This is a brief to hand to whoever produces the
fixtures, not a description of the system. Delete it once
`tests/fixtures/screenshots/` exists and the extraction tests run against it —
the same arrangement the agent instruction drafts used, and for the same
reason: two descriptions of one thing drift.

Build order step 5 is blocked on these, and step 7 (the repair loop) is
blocked on step 5.

---

## Context for whoever is doing this

A pipeline plans frozen-food shipments to a list of recipients. The first
stage, B1, reads screenshots of people asking for a packet — text messages,
DMs, an email — and extracts structured records: name, street address, city,
state, ZIP. A vision model does the reading. Design section 4 is explicit
that this is extraction, not OCR: the addresses are buried in conversation,
not laid out in a form.

Everything downstream depends on B1 being wrong in *realistic* ways. There is
a validation stage that classifies each address `clean`, `correctable` or
`failed`, and a repair loop that re-reads the original image when validation
fails. Fixtures that are all clean and legible test none of it.

---

## Deliverable

A folder of PNGs plus one `ground-truth.json`. Both halves are required.

**About 22 recipients across 6–8 screenshots** — one real run's worth.
**Several recipients per image**, deliberately: nothing in the pipeline
assumes one address per screenshot, a region is recorded per recipient rather
than per image, and the region pointers only earn their keep when there is
more than one thing in the picture. Phone-screenshot dimensions, e.g.
1170×2532.

### Ground truth is not optional

For every recipient visible in every image, record:

- the name and address **exactly as written**, including damage — if a ZIP is
  obscured to `9411?`, write `9411?`
- the pixel region it occupies, `{x, y, width, height}`
- a `difficulty` label: `clean` | `correctable` | `hard` | `impossible`
- free-text `notes` for anything a reader would need to know

```json
{
  "screenshots": [
    {
      "file": "01-imessage-thread.png",
      "source_type": "imessage",
      "recipients": [
        {
          "name": "Ana Ruiz",
          "street1": "1600 Pennsylvania Ave NW",
          "city": "Washington",
          "state": "DC",
          "zip": "20500",
          "region": {"x": 84, "y": 612, "width": 902, "height": 148},
          "difficulty": "clean",
          "notes": ""
        }
      ]
    }
  ]
}
```

**On the regions.** Approximate bounding boxes are fine; pixel-perfect is not
required and should not hold up delivery. They are worth asking for because
they separate "the extractor pointed at the wrong place" from "the extractor
pointed at the right place and misread it" — two failures with different
fixes, and one number that tells them apart. If regions are genuinely
impossible, deliver without them: the repair loop can fall back to re-reading
the whole screenshot, which is less precise and still works.

---

## Addresses: real buildings, invented people

Use **real, publicly known commercial or civic addresses** — office towers,
museums, stadiums, government buildings. Pair them with **invented recipient
names**.

Do not use any real person's home address, and do not invent addresses that
might resolve to someone's home. These fixtures get committed to the
repository.

They must be real buildings rather than made-up streets for a specific
reason: the addresses go through a live address validator, and the set is
only useful if that validator returns its genuine verdict. So it needs a mix
of addresses that really are deliverable, addresses that are plausible but
wrong (right street, wrong number or ZIP), and a couple that are nonsense.

---

## Difficulty distribution

- **~12 clean.** Fully legible, correctly formatted, really deliverable.
- **~6 correctable.** One field damaged in a way a human could resolve from
  context: a transposed ZIP, a dropped directional (`233 Wacker Dr` for
  `233 S Wacker Dr`), a misspelled city, an abbreviation collision.
- **~3 hard.** Genuinely ambiguous. A digit under an emoji reaction, text
  running off the edge of the screen, low contrast in a dark-mode thread, a
  numeral that could read 3 or 8.
- **~1 impossible.** Someone who clearly wants a packet and never gave a
  usable address: *"send it to my place, you know where I am"*.

## Cases to include deliberately

These exercise stages that are built but have only ever seen hand-written
input:

- **Two people at one address**, different names — a couple or housemates.
- **One person appearing twice** across two screenshots, the second time with
  a slightly different spelling of the same address.
- **One address that is only complete if two messages are read together**,
  flagged as such in `notes`.

## Realism

Mix the sources: iMessage, WhatsApp, Instagram DM, a Slack thread, one email.
They should look like messages people sent, not like a form.

Include what makes real extraction hard — an address split across two
bubbles, an address buried mid-paragraph, timestamps and reactions and typing
indicators overlapping the text, someone correcting their own address in a
follow-up message.

---

## Where it lands

`tests/fixtures/screenshots/` in this repository, images and
`ground-truth.json` together.
