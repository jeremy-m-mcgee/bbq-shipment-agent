"""Choosing which screenshots B1 reads, and saying which ones it read.

The property that matters is not that a choice is random but that it is
*named*: extraction accuracy is per-image, so a run that read three of seven
is only comparable to another run if you can tell which three. A seeded sample
is reconstructible from its seed; a set picked by hand in a browser is
reconstructible from nothing, which is why the filenames go on the run row.

These tests moved here from `test_cli.py` when the wiring left the CLI. The
sampling cases are unchanged; the explicit ones are new, because before the UI
there was no way to name a file.
"""

import pytest

from bbq_shipment_agent.recipients import ExtractionError
from bbq_shipment_agent.wiring import (
    RunOptions,
    ScreenshotSelection,
    extraction_reasons,
    resolve_screenshots,
)


@pytest.fixture
def images(tmp_path):
    for i in range(7):
        (tmp_path / f"{i:02d}-shot.png").write_bytes(b"")
    (tmp_path / "README.md").write_text("not a screenshot", encoding="utf-8")
    return tmp_path


def options(directory, **selection):
    chosen = ScreenshotSelection(**selection) if selection else None
    return RunOptions(screenshots=directory, selection=chosen)


class TestReadingTheWholeDirectory:
    def test_no_selection_reads_everything(self, images):
        assert len(resolve_screenshots(options(images))) == 7

    def test_only_png_files_are_candidates(self, images):
        assert all(p.suffix == ".png" for p in resolve_screenshots(options(images)))

    def test_an_empty_directory_is_refused(self, tmp_path):
        with pytest.raises(ExtractionError, match="no .png"):
            resolve_screenshots(options(tmp_path))

    def test_no_directory_at_all_is_refused(self):
        with pytest.raises(ExtractionError, match="no screenshot directory"):
            resolve_screenshots(RunOptions())


class TestSampling:
    def test_a_count_reads_exactly_that_many(self, images):
        assert len(resolve_screenshots(options(images, count=3, seed=1))) == 3

    def test_the_sample_comes_from_the_directory(self, images):
        sample = resolve_screenshots(options(images, count=3, seed=1))
        assert set(sample) <= set(images.glob("*.png"))
        assert len(set(sample)) == 3  # no image read twice

    def test_the_same_seed_reads_the_same_sample(self, images):
        first = resolve_screenshots(options(images, count=3, seed=42))
        second = resolve_screenshots(options(images, count=3, seed=42))
        assert first == second

    def test_the_sample_is_ordered_regardless_of_the_draw(self, images):
        # B1 reads in a stable order, so two runs that drew the same images
        # cannot differ in the sequence they were handed to the model.
        sample = resolve_screenshots(options(images, count=4, seed=7))
        assert list(sample) == sorted(sample)

    def test_a_seed_is_generated_and_reported_when_none_is_given(self, images, capsys):
        from bbq_shipment_agent.wiring import PrintProgress

        resolve_screenshots(options(images, count=2), PrintProgress())
        assert "seed" in capsys.readouterr().out

    def test_the_chosen_files_are_named_in_the_output(self, images, capsys):
        from bbq_shipment_agent.wiring import PrintProgress

        sample = resolve_screenshots(options(images, count=3, seed=5), PrintProgress())
        printed = capsys.readouterr().out
        assert all(p.name in printed for p in sample)

    def test_asking_for_more_than_exist_is_refused(self, images):
        # Not clamped: a run silently reading 7 when told 10 looks like a run
        # that got what it asked for.
        with pytest.raises(ExtractionError, match="exceeds"):
            resolve_screenshots(options(images, count=10))

    def test_asking_for_none_is_refused(self, images):
        with pytest.raises(ExtractionError, match="at least 1"):
            resolve_screenshots(options(images, count=0))


class TestNamingFilesDirectly:
    """What the UI added: choosing by eye rather than by count."""

    def test_the_named_files_are_the_ones_read(self, images):
        chosen = resolve_screenshots(
            options(images, explicit=("01-shot.png", "04-shot.png"))
        )
        assert [p.name for p in chosen] == ["01-shot.png", "04-shot.png"]

    def test_the_order_is_stable_however_they_were_named(self, images):
        chosen = resolve_screenshots(
            options(images, explicit=("04-shot.png", "01-shot.png"))
        )
        assert list(chosen) == sorted(chosen)

    def test_naming_one_file_twice_reads_it_once(self, images):
        chosen = resolve_screenshots(
            options(images, explicit=("01-shot.png", "01-shot.png"))
        )
        assert len(chosen) == 1

    def test_a_file_that_is_not_there_is_refused(self, images):
        # Silently skipping it would produce a run that read fewer images than
        # it was told to, which is the same failure as clamping a count.
        with pytest.raises(ExtractionError, match="not in"):
            resolve_screenshots(options(images, explicit=("99-nope.png",)))

    def test_a_path_cannot_escape_the_directory(self, images, tmp_path):
        # The name is matched against the directory listing, never joined onto
        # it. `dir / name` with a caller-supplied name is a traversal bug.
        outside = tmp_path.parent / "elsewhere.png"
        outside.write_bytes(b"")
        with pytest.raises(ExtractionError, match="not in"):
            resolve_screenshots(options(images, explicit=("../elsewhere.png",)))

    def test_naming_nothing_is_refused(self, images):
        with pytest.raises(ExtractionError, match="no screenshots were selected"):
            resolve_screenshots(options(images, explicit=()))


class TestSelectionIsOneThingOrTheOther:
    def test_explicit_and_a_count_together_is_refused(self):
        # A front-end that sent both has a bug, and picking one silently
        # would hide it.
        with pytest.raises(ExtractionError, match="not both"):
            ScreenshotSelection(explicit=("a.png",), count=2)

    def test_a_seed_with_nothing_to_seed_is_refused(self):
        with pytest.raises(ExtractionError, match="no count was given"):
            ScreenshotSelection(seed=7)


class TestWhatReachesTheRunRow:
    """Design 7: a surprising run has to be diagnosable from the ledger."""

    def test_the_filenames_are_recorded(self, images):
        chosen = resolve_screenshots(options(images, explicit=("02-shot.png",)))
        reasons = extraction_reasons(chosen, ScreenshotSelection(explicit=("02-shot.png",)))
        assert reasons["screenshots"] == ["02-shot.png"]

    def test_a_seed_is_recorded_when_there_was_one(self, images):
        selection = ScreenshotSelection(count=3, seed=11)
        chosen = resolve_screenshots(options(images, count=3, seed=11))
        assert extraction_reasons(chosen, selection)["screenshot_seed"] == 11

    def test_no_seed_is_recorded_when_there_was_none(self, images):
        # An explicit choice has no seed. Recording a null one would imply the
        # sample could be reconstructed, and it cannot -- only the names can.
        selection = ScreenshotSelection(explicit=("02-shot.png",))
        chosen = resolve_screenshots(options(images, explicit=("02-shot.png",)))
        assert "screenshot_seed" not in extraction_reasons(chosen, selection)


class TestReplayIsAllOrNothing:
    """`replaying` is what the UI shows as "no live calls". It must not lie."""

    def test_a_roster_run_needs_three_recordings(self, tmp_path):
        options = RunOptions(
            quotes=tmp_path / "q", validations=tmp_path / "v", completions=tmp_path / "c"
        )
        assert options.replaying

    def test_a_screenshot_run_needs_the_model_recordings_too(self, tmp_path):
        # B1 and B3 only run when there are screenshots, and both call a
        # model. Claiming replay without them would promise a free run that
        # then makes vision calls.
        options = RunOptions(
            screenshots=tmp_path,
            quotes=tmp_path / "q",
            validations=tmp_path / "v",
            completions=tmp_path / "c",
        )
        assert not options.replaying
        assert options.__class__(
            **{
                **options.__dict__,
                "extractions": tmp_path / "e",
                "repairs": tmp_path / "r",
            }
        ).replaying

    def test_nothing_configured_is_not_replaying(self):
        assert not RunOptions().replaying


class TestReplayingTheModelCalls:
    """B1 and B3 had no replay path until the UI needed one.

    Both fixtures existed and both were replayed by a class living in a test,
    which meant the screenshot route could not be exercised outside the suite
    without paying for vision calls.
    """

    @pytest.fixture
    def shots(self, tmp_path):
        for name, body in (("a.png", b"AAA"), ("b.png", b"BBB")):
            (tmp_path / name).write_bytes(body)
        return tmp_path

    def message_for(self, path):
        from bbq_shipment_agent.recipients.extraction import image_block

        return [{"role": "user", "content": [image_block(path)]}]

    def recording(self):
        return {
            "replies": {
                "a.png": {"text": '{"recipients": [], "note": "A"}'},
                "b.png": {"text": '{"recipients": [], "note": "B"}'},
            }
        }

    def test_the_reply_matches_the_image_that_was_sent(self, shots):
        from bbq_shipment_agent.agents import RecordedVision

        model = RecordedVision(self.recording(), shots.glob("*.png"))
        reply = model.converse(None, self.message_for(shots / "b.png"))
        assert '"B"' in reply.text

    def test_reading_the_second_image_first_still_replays_correctly(self, shots):
        # The obvious implementation keys on call order, which a picker breaks
        # the moment you read image five without reading one to four.
        from bbq_shipment_agent.agents import RecordedVision

        model = RecordedVision(self.recording(), shots.glob("*.png"))
        assert '"B"' in model.converse(None, self.message_for(shots / "b.png")).text
        assert '"A"' in model.converse(None, self.message_for(shots / "a.png")).text

    def test_an_unrecorded_image_raises(self, shots, tmp_path):
        # A fixture that answers for anything is a rubber stamp.
        from bbq_shipment_agent.agents import RecordedVision
        from bbq_shipment_agent.agents.model import ModelUnavailable

        other = tmp_path / "elsewhere"
        other.mkdir()
        (other / "c.png").write_bytes(b"CCC")
        model = RecordedVision(self.recording(), shots.glob("*.png"))
        with pytest.raises(ModelUnavailable, match="no recorded extraction"):
            model.converse(None, self.message_for(other / "c.png"))

    def test_a_conversation_replays_its_turns_in_order(self):
        from bbq_shipment_agent.agents import RecordedConversation

        model = RecordedConversation(
            {
                "turns": [
                    {"text": "first", "tool_calls": [
                        {"id": "1", "name": "validate_address", "arguments": {}}
                    ]},
                    {"text": "second", "tool_calls": []},
                ]
            }
        )
        first = model.converse(None, [])
        assert first.text == "first"
        assert first.tool_calls[0].name == "validate_address"
        assert model.converse(None, []).text == "second"

    def test_running_out_of_turns_ends_the_loop_rather_than_raising(self):
        # The recording is finished and the agent has nothing further to
        # claim, so whoever is left stays escalated -- a normal successful
        # outcome, and the safe direction for an unverified address.
        from bbq_shipment_agent.agents import RecordedConversation

        model = RecordedConversation({"turns": []})
        assert model.converse(None, []).text == '{"repairs": [], "escalations": []}'

    def test_the_options_pick_the_right_replay_class(self, shots, tmp_path):
        import json

        from bbq_shipment_agent.agents import RecordedConversation, RecordedVision
        from bbq_shipment_agent.capabilities import PlannerMode
        from bbq_shipment_agent.wiring import extraction_model, repair_model

        (tmp_path / "b1.json").write_text(json.dumps(self.recording()))
        (tmp_path / "b3.json").write_text(json.dumps({"turns": []}))
        options = RunOptions(
            screenshots=shots,
            extractions=tmp_path / "b1.json",
            repairs=tmp_path / "b3.json",
        )
        images = tuple(sorted(shots.glob("*.png")))
        assert isinstance(extraction_model(options, images), RecordedVision)
        assert isinstance(
            repair_model(options, PlannerMode.ON), RecordedConversation
        )


class TestSayingWhichKeysAreMissing:
    """The most common failure in this repo is a `.env` that was never loaded.

    `uv run` reads it only when `UV_ENV_FILE` points at it, and a key that is
    present-but-unloaded looks identical to one that was never set. The UI
    made this worse than the CLI had it: a server starts cleanly and the first
    sign of trouble is a failed run several clicks later.
    """

    def test_a_live_run_with_no_keys_names_both(self, monkeypatch, tmp_path):
        from bbq_shipment_agent.wiring import missing_credentials

        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SHIPPO_API_KEY", "")
        missing = missing_credentials(RunOptions(screenshots=tmp_path))
        assert set(missing) == {"ANTHROPIC_API_KEY", "SHIPPO_API_KEY"}
        assert "B1 extraction" in missing["ANTHROPIC_API_KEY"]
        assert "C2 rate quotes" in missing["SHIPPO_API_KEY"]

    def test_an_empty_key_counts_as_unset(self, monkeypatch):
        # setup.sh seeds .env from .env.example, so an unconfigured key is
        # present-but-empty rather than absent.
        from bbq_shipment_agent.wiring import missing_credentials

        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SHIPPO_API_KEY", "x")
        assert "ANTHROPIC_API_KEY" in missing_credentials(RunOptions())

    def test_no_screenshots_means_no_vision_stages_are_named(self, monkeypatch):
        # B1 and B3 only run when there are images, so naming them on a roster
        # run would be a warning about something that cannot happen.
        from bbq_shipment_agent.wiring import missing_credentials

        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SHIPPO_API_KEY", "x")
        stages = missing_credentials(RunOptions())["ANTHROPIC_API_KEY"]
        assert stages == ("D1 verification",)

    def test_a_recorded_path_reaches_for_nothing(self, monkeypatch, tmp_path):
        from bbq_shipment_agent.wiring import missing_credentials

        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SHIPPO_API_KEY", "")
        options = RunOptions(
            screenshots=tmp_path,
            quotes=tmp_path / "q",
            validations=tmp_path / "v",
            completions=tmp_path / "c",
            extractions=tmp_path / "e",
            repairs=tmp_path / "r",
        )
        assert missing_credentials(options) == {}

    def test_keys_that_are_set_are_not_reported(self, monkeypatch, tmp_path):
        from bbq_shipment_agent.wiring import missing_credentials

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-something")
        monkeypatch.setenv("SHIPPO_API_KEY", "shippo_something")
        assert missing_credentials(RunOptions(screenshots=tmp_path)) == {}
