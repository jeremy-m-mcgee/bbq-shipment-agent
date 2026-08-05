"""B1, against real replies from the configured model.

Graded on `tests/fixtures/screenshots/ground_truth.json`, which is the only
answer key in this system. Design 8 says nothing here reaches significance at
22 packets a few times a year; this is the exception, because the same seven
images can be re-scored offline as often as you like.
"""

import json
import textwrap
from pathlib import Path

import pytest

from bbq_shipment_agent.agent_configs import SnapshotAgentConfigs
from bbq_shipment_agent.context import ImageIdentity
from bbq_shipment_agent.agents.model import Completion, ModelUnavailable
from bbq_shipment_agent.recipients import ExtractionError, extract_from_images
from bbq_shipment_agent.recipients.extraction import CONFIG_KEY
from bbq_shipment_agent.run import initialize_run

FIXTURES = Path(__file__).parent / "fixtures"
SHOTS = FIXTURES / "screenshots"
REPLIES = FIXTURES / "b1-extractions.json"
SNAPSHOT = Path(__file__).parent.parent / "config" / "ld-snapshot.json"
IMAGES = tuple(sorted(SHOTS.glob("*.png")))

CONFIG = """
    profiles:
      baseline:
        planner: "off"
        memory: "off"
        validation: "off"
        verification: "off"
        authority: "propose_only"
    default_profile: "baseline"
    authority_ceiling: "propose_only"
    kill_switch: false
"""


class RecordedVision:
    """Replays the reply captured for whichever image is in the message.

    Keyed on the image rather than on the instruction hash, because B1 makes
    one call per screenshot with one invocation. An unrecorded image raises,
    for the same reason `RecordedQuoter` does: a fixture that answers for
    anything is a rubber stamp.
    """

    def __init__(self, recording: dict, order: tuple[str, ...]):
        self._replies = recording["replies"]
        self._order = list(order)
        self.calls = 0

    def converse(self, invocation, messages, tools=()):
        self.calls += 1
        name = self._order.pop(0)
        row = self._replies.get(name)
        if row is None:
            raise ModelUnavailable(f"no recorded extraction for {name}")
        return Completion(
            text=row["text"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            model=row.get("model"),
        )


@pytest.fixture(scope="module")
def truth():
    gt = json.loads((SHOTS / "ground_truth.json").read_text())
    return [(s["file"], r) for s in gt["screenshots"] for r in s["recipients"]]


@pytest.fixture
def run(tmp_path):
    (tmp_path / "capabilities.yaml").write_text(
        textwrap.dedent(CONFIG), encoding="utf-8"
    )
    return initialize_run(
        ledger_root=tmp_path / "ledger",
        config_path=tmp_path / "capabilities.yaml",
        agent_source=SnapshotAgentConfigs(SNAPSHOT),
        snapshot_path=tmp_path / "snap.json",
    )


@pytest.fixture
def result(run, tmp_path):
    recording = json.loads(REPLIES.read_text())
    model = RecordedVision(recording, tuple(p.name for p in IMAGES))
    return extract_from_images(
        run, IMAGES, model=model, ledger_root=tmp_path / "ledger"
    )


def normalize(value):
    return str(value or "").strip().lower()


def address_of(row):
    return tuple(normalize(row.get(k)) for k in ("street1", "city", "state", "zip"))


class TestItReadsEveryScreenshot:
    def test_one_call_per_image(self, run, tmp_path):
        # Design 6.3: single shot, no loop. Seven images, seven calls.
        recording = json.loads(REPLIES.read_text())
        model = RecordedVision(recording, tuple(p.name for p in IMAGES))
        extract_from_images(
            run, IMAGES, model=model, ledger_root=tmp_path / "ledger"
        )
        assert model.calls == len(IMAGES)

    def test_nothing_was_unreadable(self, result):
        assert result.unreadable == ()

    def test_every_person_in_the_answer_key_is_accounted_for(self, result, truth):
        # 22 people: 21 with an address, one who never gave one.
        assert len(result.recipients) + len(result.unresolved) == len(truth)


class TestAgainstTheAnswerKey:
    def test_every_address_matches(self, result, truth):
        # Matched on address rather than on name. The key records full names
        # the image does not always show -- the thread says "this is Hector
        # btw", and B1 correctly returns "Hector". Grading on name would fail
        # the extractor for obeying its instructions.
        expected = {address_of(r) for _, r in truth if r.get("street1")}
        actual = {address_of({
            "street1": r.address.street1, "city": r.address.city,
            "state": r.address.state, "zip": r.address.zip,
        }) for r in result.recipients}
        assert actual == expected

    def test_the_person_with_no_address_is_unresolved_not_dropped(self, result, truth):
        impossible = [r for _, r in truth if r["difficulty"] == "impossible"]
        assert len(result.unresolved) == len(impossible) == 1
        assert result.unresolved[0].note

    def test_an_unresolved_person_becomes_an_escalation(self, result):
        # Design 4 sends them to a human queue rather than dropping them.
        excluded = result.escalations()
        assert len(excluded) == 1
        assert "no usable address" in excluded[0].reason


class TestItReadsRatherThanCorrects:
    def test_obscured_digits_survive(self, result):
        # The load-bearing rule. B2 must see what the image said, and B3 must
        # have something to repair. An address B1 silently fixed is one nobody
        # downstream can check.
        zips = {r.address.zip for r in result.recipients}
        assert any("?" in z for z in zips), zips

    def test_a_damaged_field_lowers_confidence(self, result):
        damaged = [r for r in result.recipients if "?" in r.address.zip]
        assert damaged
        for r in damaged:
            assert r.confidence is not None and r.confidence <= 0.6

    def test_a_clean_read_is_confident(self, result):
        clean = [r for r in result.recipients if "?" not in r.address.zip]
        assert all(r.confidence is None or r.confidence >= 0.8 for r in clean)

    def test_a_self_correction_yields_the_corrected_address(self, result):
        # The thread sends "11 S 4th St" then "ugh sorry NORTH not south".
        streets = {r.address.street1 for r in result.recipients}
        assert "11 N 4th St" in streets
        assert "11 S 4th St" not in streets


class TestProvenance:
    def test_every_recipient_knows_which_image_it_came_from(self, result):
        names = {p.name for p in IMAGES}
        for r in result.recipients:
            assert r.provenance is not None
            assert r.provenance.source_image in names

    def test_regions_land_on_the_recorded_text(self, result, truth):
        # Not pixel-exact and not required to be: the region exists so B3 can
        # crop back and re-read, so overlapping the right text is the bar.
        by_address = {address_of(r): r for _, r in truth if r.get("street1")}
        overlaps = []
        for got in result.recipients:
            want = by_address[address_of({
                "street1": got.address.street1, "city": got.address.city,
                "state": got.address.state, "zip": got.address.zip,
            })]
            if not (got.provenance.region and want.get("region")):
                continue
            a, b = got.provenance.region, want["region"]
            ix = max(0, min(a.x + a.width, b["x"] + b["width"]) - max(a.x, b["x"]))
            iy = max(0, min(a.y + a.height, b["y"] + b["height"]) - max(a.y, b["y"]))
            overlaps.append(ix * iy > 0)
        assert overlaps
        assert sum(overlaps) / len(overlaps) >= 0.9

    def test_the_extracted_flag_distinguishes_these_from_a_roster(self, result):
        assert all(r.extracted for r in result.recipients)

    def test_provenance_does_not_survive_into_planning(self, result):
        # The seam. A stage with no business re-reading a screenshot cannot,
        # because it is not holding one.
        from bbq_shipment_agent.recipients import to_shipments

        shipments = to_shipments(result.recipients)
        assert shipments
        assert not any(hasattr(s, "provenance") for s in shipments)


class TestDuplicatesReachB4:
    def test_one_person_across_two_images_gets_one_key(self, result):
        # The fixture puts Priya Nandakumar in two screenshots deliberately.
        # B1 must not de-duplicate -- that is B4's job, and B4 needs to see
        # both to report the suppression.
        keys = [r.key for r in result.recipients]
        assert len(keys) != len(set(keys))

    def test_and_b4_collapses_them(self, result):
        from bbq_shipment_agent.recipients import dedupe_recipients

        report = dedupe_recipients(result.recipients)
        assert len(report.eligible) < len(result.recipients)
        assert report.suppressed


class TestPerImageRetrieval:
    """Design 6.2 makes B1 the one stage whose variation can be scored against
    an answer key, so its config is retrieved per image rather than per run.
    That is only worth anything if the ledger can say which image got which."""

    @pytest.fixture
    def run_with_images(self, tmp_path):
        (tmp_path / "capabilities.yaml").write_text(
            textwrap.dedent(CONFIG), encoding="utf-8"
        )
        return initialize_run(
            ledger_root=tmp_path / "ledger",
            config_path=tmp_path / "capabilities.yaml",
            agent_source=SnapshotAgentConfigs(SNAPSHOT),
            snapshot_path=tmp_path / "snap.json",
            images=tuple(ImageIdentity.of(p) for p in IMAGES),
        )

    def invocations(self, ledger_root):
        path = Path(ledger_root) / "agent_invocations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def test_a_config_is_held_for_every_image(self, run_with_images):
        assert set(run_with_images.image_configs) == {
            ImageIdentity.of(p).key for p in IMAGES
        }

    def test_one_invocation_is_recorded_per_image(self, run_with_images, tmp_path):
        recording = json.loads(REPLIES.read_text())
        model = RecordedVision(recording, tuple(p.name for p in IMAGES))
        extract_from_images(
            run_with_images, IMAGES, model=model, ledger_root=tmp_path / "ledger"
        )
        rows = [
            r
            for r in self.invocations(tmp_path / "ledger")
            if r["agent_key"] == CONFIG_KEY
        ]
        # One per image, not one per batch: a batch record could not express a
        # run where a rollout served two variations.
        assert len(rows) == len(IMAGES)
        assert {r["image_key"] for r in rows} == {ImageIdentity.of(p).key for p in IMAGES}

    def test_the_image_key_joins_to_the_run_row(self, run_with_images, tmp_path):
        # LD is given a hash and never a filename, so this is the only thing
        # that can say which file a served variation actually read.
        from bbq_shipment_agent.wiring import extraction_reasons

        mapping = extraction_reasons(IMAGES, None)["screenshot_keys"]
        assert set(mapping.values()) == set(run_with_images.image_configs)
        assert set(mapping) == {p.name for p in IMAGES}

    def test_a_roster_run_holds_no_image_configs(self, run):
        # No screenshots, so B1 never runs and there is nothing to retrieve.
        assert run.image_configs == {}


class TestRefusals:
    def test_a_missing_config_is_an_error_not_an_empty_result(self, run, tmp_path):
        run.agent_configs.pop(CONFIG_KEY)
        with pytest.raises(ExtractionError, match="no config"):
            extract_from_images(
                run, IMAGES, model=RecordedVision({"replies": {}}, ()),
                ledger_root=tmp_path / "ledger",
            )

    def test_an_unparseable_reply_is_retried_then_recorded_unreadable(self, run, tmp_path):
        class Prose:
            calls = 0

            def converse(self, invocation, messages, tools=()):
                Prose.calls += 1
                return Completion(text="I could not read that screenshot, sorry.")

        result = extract_from_images(run, IMAGES[:1], model=Prose(), ledger_root=tmp_path / "ledger")
        assert [u.name for u in result.unreadable] == [IMAGES[0].name]
        assert Prose.calls == 2
        assert result.recipients == ()

    def test_the_reason_a_reply_would_not_parse_is_kept(self, run, tmp_path):
        # `_parse` diagnoses the failure and the retry nudge consumed it; the
        # ledger recorded a bare "unreadable". Design 2 wants a surprising run
        # diagnosable from the committed JSONL, and B1 is the stage whose
        # variations are meant to be compared -- "prose instead of JSON" is a
        # console edit, "invalid JSON" is a model choice, and they read the
        # same without the reason.
        class Prose:
            def converse(self, invocation, messages, tools=()):
                return Completion(text="I could not read that screenshot, sorry.")

        ledger = tmp_path / "ledger"
        result = extract_from_images(run, IMAGES[:1], model=Prose(), ledger_root=ledger)
        assert result.unreadable[0].reason == "no JSON object in the reply"

        rows = [
            json.loads(line)
            for line in (ledger / "agent_invocations.jsonl").read_text().splitlines()
            if line
        ]
        outcomes = [r["outcome"] for r in rows if r["agent_key"] == CONFIG_KEY]
        assert outcomes == ["unreadable: no JSON object in the reply"]

    def test_a_reply_that_is_json_but_the_wrong_shape_says_so(self, run, tmp_path):
        # The failure an instruction edit actually produces: valid JSON, no
        # `recipients` list. Distinguishable from prose only by the reason.
        class WrongShape:
            def converse(self, invocation, messages, tools=()):
                return Completion(text='{"people": []}')

        result = extract_from_images(
            run, IMAGES[:1], model=WrongShape(), ledger_root=tmp_path / "ledger"
        )
        assert result.unreadable[0].reason == "the JSON object has no `recipients` list"


#: What `screenshot-extraction` really returned for `05-email.png` under the
#: `sonnet-3-5-output-block` variation, captured live. Abridged to two of the
#: three recipients and otherwise the bytes as they arrived: a fenced block
#: around a bare array, the field names that variation's output spec asks for,
#: and a region in corner rather than origin-and-size form.
CAPTURED_VAGUE_REPLY = """```json
[
  {
    "name": "Owen Doyle",
    "street": "1000 Jefferson Dr",
    "city": "Washington",
    "state": "DC",
    "zip9": "20560",
    "confidence": 0.95,
    "note": "Building, apartment side entrance, easiest to leave it with the desk",
    "region": {"x1": 33, "y1": 607, "x2": 680, "y2": 775}
  },
  {
    "name": "Lena Ford",
    "street": "1060 W Addison St",
    "city": "Chicago",
    "state": "IL",
    "zip9": "60631",
    "confidence": 0.95,
    "note": null,
    "region": {"x1": 33, "y1": 818, "x2": 610, "y2": 895}
  }
]
```"""


class TestTheContainerIsNotTheContents:
    """A vague output spec varies the wrapper, and that used to lose the run.

    Design 6.1 puts instruction text in LaunchDarkly, so which container a
    reply arrives in is not something Python can insist on. What a recipient
    row must contain still is, and these tests hold those apart: the reply
    below parses, and the people in it are still not recipients.
    """

    def _read(self, run, tmp_path, text):
        class Fixed:
            def converse(self, invocation, messages, tools=()):
                return Completion(text=text)

        return extract_from_images(
            run, IMAGES[:1], model=Fixed(), ledger_root=tmp_path / "ledger"
        )

    def test_the_captured_reply_is_no_longer_unreadable(self, run, tmp_path):
        # It was valid JSON all along. `_JSON_BLOCK` spans the first brace to
        # the last, so on a bare array it stripped the brackets and produced
        # `{...}, {...}` -- "invalid JSON (Extra data)" on eleven ledger rows,
        # manufactured by the extractor rather than returned by the model.
        result = self._read(run, tmp_path, CAPTURED_VAGUE_REPLY)
        assert result.unreadable == ()

    def test_a_bare_array_is_the_recipients_list(self, run, tmp_path):
        result = self._read(
            run, tmp_path,
            '[{"name": "Ana", "street1": "1600 Pennsylvania Ave NW", '
            '"city": "Washington", "state": "DC", "zip": "20500"}]',
        )
        assert [r.name for r in result.recipients] == ["Ana"]

    def test_a_fence_is_read_rather_than_tolerated(self, run, tmp_path):
        result = self._read(
            run, tmp_path,
            '```json\n{"recipients": [{"name": "Ana", '
            '"street1": "1600 Pennsylvania Ave NW", "city": "Washington", '
            '"state": "DC", "zip": "20500"}]}\n```',
        )
        assert [r.name for r in result.recipients] == ["Ana"]

    def test_prose_with_no_json_still_fails(self, run, tmp_path):
        # Tolerance about the container is not tolerance about everything.
        result = self._read(run, tmp_path, "I could not read that screenshot.")
        assert result.unreadable[0].reason == "no JSON object in the reply"

    def test_objects_with_no_container_still_fail(self, run, tmp_path):
        result = self._read(run, tmp_path, '{"name": "Ana"}\n{"name": "Marcus"}')
        assert result.unreadable != ()

    def test_the_wrong_field_names_are_unresolved_not_recipients(self, run, tmp_path):
        # The half deliberately left alone. `street`/`zip9` parse and then
        # fail the completeness check, so these people are reported as having
        # given no address -- which is wrong about *why*, and is the open
        # question on #14 rather than something to paper over here.
        result = self._read(run, tmp_path, CAPTURED_VAGUE_REPLY)
        assert result.recipients == ()
        assert [u.name for u in result.unresolved] == ["Owen Doyle", "Lena Ford"]

    def test_a_corner_region_is_dropped_rather_than_misread(self, run, tmp_path):
        # `{x1,y1,x2,y2}` is not `{x,y,width,height}`, and reading one as the
        # other would hand B3 a crop box pointing somewhere plausible and
        # wrong. None is the honest answer: B3 re-reads the whole screenshot.
        result = self._read(run, tmp_path, CAPTURED_VAGUE_REPLY)
        assert all(u.provenance.region is None for u in result.unresolved)
