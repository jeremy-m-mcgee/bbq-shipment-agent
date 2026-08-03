# B1 extraction fixtures

Seven screenshots, twenty-two recipients, and `ground_truth.json` recording
what each image actually says. Produced against the brief in
`docs/screenshot-fixtures.md`.

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
