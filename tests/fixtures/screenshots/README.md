# B1 extraction fixtures

Eight screenshots, twenty-five recipients, and `ground_truth.json` recording
what each image actually says.

The first seven were commissioned against a brief, now deleted — it is at
`git show 37bdce3^:docs/screenshot-fixtures.md` if you want what it asked for,
and the distribution it specified is still the one to aim at. They arrived as
binaries and cannot be regenerated: there is no spec for them, and the regions
in their answer-key entries were measured by eye.

Everything after them is built by `tools/make_screenshots.py` from a spec in
`tools/screenshot_fixtures/specs/`, which renders the PNG and derives its
`ground_truth.json` entry in one pass. The regions are then exact rather than
estimated, because the renderer records where it put the glyphs.

## A screenshot is not finished when the PNG exists

The tests glob this directory, so a new image is enrolled in the extraction and
repair suites the moment it lands — and both replay files raise on an input
they have never seen. Adding one therefore needs two live recordings:

1. B1's reply, into `../b1-extractions.json`. One vision call.
2. Every address it returns, through the live validator, into
   `../shippo-addresses.json`.

Neither can be hand-written, and the reason is the same both times: a fixture
that answers for anything is a rubber stamp. Writing the reply yourself means
authoring the question and the answer, and the answer key exists to ask whether
B1 read the image correctly. `08-imessage-cousins` is the worked example — its
`hard` case asks whether an obscured ZIP comes back transcribed or guessed, and
a hand-written recording would have asserted the very thing under test.

## `difficulty` is about reading, not about the address

The one thing to know before writing a test against these. `difficulty`
describes how hard the text is to *extract from the image*. It says nothing
about whether the resulting address is deliverable. Those are different axes
and the set contains every combination that matters:

| Recipient | `difficulty` | validator says | why |
|---|---|---|---|
| Cody Barnes | `hard` | `clean` | Dark-mode low-contrast text, but Union Station is a real, correct address |
| Gus Whitfield | `correctable` | `failed` | `Philadelpia` — a human fixes it instantly, the validator cannot resolve it at all |
| Hector Ramos | `clean` | `correctable` | Perfectly legible, and `St. Louis` is not USPS-standard for `SAINT LOUIS` |

So: do not assert that `difficulty: clean` implies a clean validation, and do
not treat a `hard` label as an expected failure. B1 is graded against
`ground_truth.json`; B2 and B3 are graded against what the validator actually
returned.

Gus Whitfield is the sharpest case in the set and worth keeping: the
validator returns `failed`, a human reads the misspelling in a second, and
the address is otherwise correct. That is precisely the input B3 exists for —
a repair the machine cannot make and a person can.

## Validator answers are recorded, not fetched

All twenty-two addresses were run through the live Shippo validator once and
the responses merged into `tests/fixtures/shippo-addresses.json`, which
`RecordedAddressValidator` replays. Tests downstream of B1 therefore run
offline against real answers rather than assumed ones.

Re-record rather than hand-edit if an address changes. An unrecorded address
raises instead of being assumed clean, which is what stops the fixture
quietly becoming a rubber stamp.

## Regions

Per recipient, not per image — several recipients share a screenshot, which
is the point of recording a region at all. They mark the *address* text, and
where someone corrected themselves the region points at the corrected
message: Hector Ramos sent `11 S 4th St` and then `11 N 4th St`, and the
region is on the second.

Spot-checked against the images and accurate. Nothing in the pipeline
requires them to be exact — see `docs/screenshot-fixtures.md` for why they
are worth having anyway.
