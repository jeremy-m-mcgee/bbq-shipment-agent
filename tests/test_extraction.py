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
def result(run):
    recording = json.loads(REPLIES.read_text())
    model = RecordedVision(recording, tuple(p.name for p in IMAGES))
    return extract_from_images(run, IMAGES, model=model)


def normalize(value):
    return str(value or "").strip().lower()


def address_of(row):
    return tuple(normalize(row.get(k)) for k in ("street1", "city", "state", "zip"))


class TestItReadsEveryScreenshot:
    def test_one_call_per_image(self, run):
        # Design 6.3: single shot, no loop. Seven images, seven calls.
        recording = json.loads(REPLIES.read_text())
        model = RecordedVision(recording, tuple(p.name for p in IMAGES))
        extract_from_images(run, IMAGES, model=model)
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


class TestRefusals:
    def test_a_missing_config_is_an_error_not_an_empty_result(self, run):
        run.agent_configs.pop(CONFIG_KEY)
        with pytest.raises(ExtractionError, match="no config"):
            extract_from_images(run, IMAGES, model=RecordedVision({"replies": {}}, ()))

    def test_an_unparseable_reply_is_retried_then_recorded_unreadable(self, run):
        class Prose:
            calls = 0

            def converse(self, invocation, messages, tools=()):
                Prose.calls += 1
                return Completion(text="I could not read that screenshot, sorry.")

        result = extract_from_images(run, IMAGES[:1], model=Prose())
        assert result.unreadable == (IMAGES[0].name,)
        assert Prose.calls == 2
        assert result.recipients == ()
