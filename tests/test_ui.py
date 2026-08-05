"""The web front-end, driven in-process.

`TestClient` speaks ASGI directly, so nothing here opens a socket -- the same
property the rest of the suite has, and worth keeping now that there is a
server in the package.

The end-to-end case matters more than the route coverage. A second front-end
is a second chance to cross a pipeline seam untranslated, which is exactly how
`run review` shipped a `Recipient` where a `Shipment` was wanted and died the
first time anyone drove it end to end. So one test starts a real run through
the HTTP layer, against recorded answers, and reads the manifest off the page.
"""

import json
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bbq_shipment_agent.ui import RunService, create_app
from bbq_shipment_agent.ui.app import _options_for
from bbq_shipment_agent.ui.view import screenshot_catalogue
from bbq_shipment_agent.wiring import RunDepth, RunOptions

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = FIXTURES / "shippo-quotes-sf-dc.json"
VALIDATIONS = FIXTURES / "shippo-addresses.json"
COMPLETIONS = FIXTURES / "d1-completions.json"
EXTRACTIONS = FIXTURES / "b1-extractions.json"
FIXTURE_SHOTS = FIXTURES / "screenshots"
SNAPSHOT = Path(__file__).parent.parent / "config" / "ld-snapshot.json"

CONFIG = """
    profiles:
      baseline:
        planner: "off"
        validation: "standard"
        verification: "off"
    default_profile: "baseline"
    kill_switch: false
"""

ROSTER = """
origin:
  name: BBQ HQ
  street1: 64 Divisadero St
  city: San Francisco
  state: CA
  zip: "94117"
ship_dates:
  - 2026-08-17
recipients:
  - key: ana
    name: Ana Ruiz
    street1: 1600 Pennsylvania Ave NW
    city: Washington
    state: DC
    zip: "20500"
"""


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "capabilities.yaml").write_text(
        textwrap.dedent(CONFIG), encoding="utf-8"
    )
    (tmp_path / "recipients.yaml").write_text(ROSTER, encoding="utf-8")
    shots = tmp_path / "shots"
    shots.mkdir()
    for name in ("01-imessage.png", "02-whatsapp.png", "03-email.png"):
        (shots / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    return tmp_path


@pytest.fixture
def options(workspace):
    # Offline and fully recorded: this run reaches LaunchDarkly, Shippo and
    # Anthropic exactly never.
    return RunOptions(
        ledger=workspace / "ledger",
        config=workspace / "capabilities.yaml",
        snapshot=workspace / "snapshot.json",
        lanes=workspace / "lanes.yaml",
        cache=workspace / "cache",
        recipients=workspace / "recipients.yaml",
        offline=True,
        screenshots=workspace / "shots",
        quotes=QUOTES,
        validations=VALIDATIONS,
        completions=COMPLETIONS,
    )


@pytest.fixture
def client(options, workspace):
    return TestClient(create_app(options, screenshot_dir=workspace / "shots"))


def finish(client, job_id, timeout=30.0):
    """Wait for the worker thread, then return the finished page."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/runs/{job_id}/state").json()
        if state["state"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError(f"run {job_id} did not finish within {timeout}s")


class TestThePicker:
    def test_the_setup_page_lists_every_screenshot(self, client):
        body = client.get("/").text
        assert "01-imessage.png" in body
        assert "03-email.png" in body

    def test_each_one_is_a_checkbox_the_run_will_receive(self, client):
        body = client.get("/").text
        assert 'name="screenshot" value="01-imessage.png"' in body

    def test_nothing_is_selected_by_default(self, client):
        # Every ticked image is a live vision call. A picker that arrives
        # pre-armed turns "look at the page" into "spend seven calls" on one
        # click, which is exactly how it went the first time it was used.
        body = client.get("/").text
        # The tag itself, not the word: `checked` also appears in the script
        # that keeps the count and the button in step.
        assert 'value="01-imessage.png" checked' not in body
        assert 'name="screenshot"' in body and 'class="shot on"' not in body

    def test_the_button_starts_disabled_with_nothing_picked(self, client):
        # Belt and braces: the POST already refuses an empty explicit
        # selection. This stops the click rather than explaining it after.
        body = client.get("/").text
        assert 'id="go"' in body
        assert "Nothing selected" in body or "go.disabled" in body

    def test_the_images_themselves_are_served(self, client):
        response = client.get("/screenshots/01-imessage.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    def test_an_unknown_name_is_refused(self, client):
        assert client.get("/screenshots/nope.png").status_code == 404

    @pytest.mark.parametrize(
        "name",
        [
            "../capabilities.yaml",
            "..%2Fcapabilities.yaml",
            "....//capabilities.yaml",
            "/etc/passwd",
        ],
    )
    def test_nothing_outside_the_directory_can_be_reached(self, client, name):
        # The name is looked up in a listing of the directory, never joined
        # onto it. There is no reason to be within arm's reach of a traversal
        # bug to show seven PNGs.
        response = client.get(f"/screenshots/{name}")
        assert response.status_code in {404, 400, 405}
        assert b"kill_switch" not in response.content

    def test_a_directory_with_no_images_says_so(self, options, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        client = TestClient(create_app(options, screenshot_dir=empty))
        body = client.get("/").text
        assert "No <code>.png</code> files" in body
        # And the run it offers falls back to the roster file rather than
        # failing, which is what a user without screenshots wants.
        assert 'name="no_screenshots"' in body


class TestTheCatalogue:
    def test_it_reads_the_answer_key_when_there_is_one(self, tmp_path):
        # `ground_truth.json` turns the picker into a control you can aim --
        # "read the two hard ones" -- rather than a list of filenames.
        import json

        (tmp_path / "a.png").write_bytes(b"")
        (tmp_path / "ground_truth.json").write_text(
            json.dumps(
                {
                    "screenshots": [
                        {
                            "file": "a.png",
                            "source_type": "imessage",
                            "recipients": [
                                {"difficulty": "clean"},
                                {"difficulty": "hard"},
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        rows = screenshot_catalogue(tmp_path, (tmp_path / "a.png",))
        assert rows[0]["expected"] == 2
        assert rows[0]["difficulties"] == ["clean", "hard"]
        assert rows[0]["source_type"] == "imessage"
        assert rows[0]["decoys"] == 0

    def test_the_caption_counts_the_people_who_are_not_recipients(self, tmp_path):
        # `09-whatsapp-neighbours` is two clean addresses and two people who
        # must not come back. Captioned only as "2 recipients, clean" it looks
        # like the easiest image in the set, and the picker exists to be aimed.
        import json

        (tmp_path / "a.png").write_bytes(b"")
        (tmp_path / "ground_truth.json").write_text(
            json.dumps(
                {
                    "screenshots": [
                        {
                            "file": "a.png",
                            "recipients": [{"difficulty": "clean"}],
                            "non_recipients": [
                                {"name": "Wes", "reason": "declined"},
                                {"name": "Marisol", "reason": "not a request"},
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert screenshot_catalogue(tmp_path, (tmp_path / "a.png",))[0]["decoys"] == 2

    def test_no_answer_key_is_not_an_error(self, tmp_path):
        # A directory of real screenshots will never have one.
        (tmp_path / "a.png").write_bytes(b"")
        rows = screenshot_catalogue(tmp_path, (tmp_path / "a.png",))
        assert rows[0]["expected"] is None

    def test_a_damaged_answer_key_is_not_an_error_either(self, tmp_path):
        (tmp_path / "a.png").write_bytes(b"")
        (tmp_path / "ground_truth.json").write_text("{not json", encoding="utf-8")
        assert screenshot_catalogue(tmp_path, (tmp_path / "a.png",))[0]["expected"] is None


class TestTheFormBecomesOptions:
    """Every branch in `_options_for` is a user-visible choice."""

    def base(self, tmp_path, **kwargs):
        defaults = dict(
            directory=tmp_path,
            mode="all",
            names=[],
            count="",
            seed="",
            profile="",
            campaign="",
            offline=False,
            replay=True,
            no_screenshots=False,
        )
        return _options_for(RunOptions(), **{**defaults, **kwargs})

    def test_picked_images_become_an_explicit_selection(self, tmp_path):
        options = self.base(tmp_path, mode="explicit", names=["a.png", "b.png"])
        assert options.selection.explicit == ("a.png", "b.png")

    def test_picking_nothing_is_refused_rather_than_read_as_everything(self, tmp_path):
        with pytest.raises(ValueError, match="no screenshots were selected"):
            self.base(tmp_path, mode="explicit", names=[])

    def test_a_sample_becomes_a_count(self, tmp_path):
        options = self.base(tmp_path, mode="sample", count="3", seed="7")
        assert (options.selection.count, options.selection.seed) == (3, 7)

    def test_a_sample_without_a_seed_leaves_one_to_be_generated(self, tmp_path):
        assert self.base(tmp_path, mode="sample", count="3").selection.seed is None

    def test_a_sample_needs_a_number(self, tmp_path):
        with pytest.raises(ValueError, match="not a number"):
            self.base(tmp_path, mode="sample", count="lots")

    def test_all_means_no_selection_at_all(self, tmp_path):
        assert self.base(tmp_path, mode="all").selection is None

    def test_turning_replay_off_clears_every_recording(self, tmp_path):
        # This is the whole difference between a free run and a run that
        # spends money. `wiring` builds a live client for any path with no
        # recording, so a half-cleared set would be worse than either.
        options = _options_for(
            RunOptions(quotes=Path("q"), validations=Path("v"), completions=Path("c")),
            directory=tmp_path,
            mode="all",
            names=[],
            count="",
            seed="",
            profile="",
            campaign="",
            offline=False,
            replay=False,
            no_screenshots=False,
        )
        assert not any(
            (options.quotes, options.validations, options.completions,
             options.extractions, options.repairs)
        )

    def test_the_ledger_is_not_something_the_form_can_move(self, tmp_path):
        # A field for it is a way to append a real run to the wrong file.
        base = RunOptions(ledger=Path("real-ledger"))
        options = _options_for(
            base, directory=tmp_path, mode="all", names=[], count="", seed="",
            profile="", campaign="", offline=False, replay=True, no_screenshots=False,
        )
        assert options.ledger == Path("real-ledger")


class TestDepth:
    """Where the run stops, as a form field."""

    def test_the_default_is_the_full_plan(self, tmp_path):
        options = _options_for(
            RunOptions(), directory=tmp_path, mode="all", names=[], count="", seed="",
            profile="", campaign="", offline=False, replay=True, no_screenshots=False,
        )
        assert options.depth is RunDepth.PLAN

    def test_extract_only_reaches_the_options(self, tmp_path):
        options = _options_for(
            RunOptions(), directory=tmp_path, mode="all", names=[], count="", seed="",
            profile="", campaign="", offline=False, replay=True, no_screenshots=False,
            depth="extract",
        )
        assert options.depth is RunDepth.EXTRACT

    def test_an_unknown_depth_is_refused_rather_than_defaulted(self, tmp_path):
        with pytest.raises(ValueError, match="not a depth"):
            _options_for(
                RunOptions(), directory=tmp_path, mode="all", names=[], count="",
                seed="", profile="", campaign="", offline=False, replay=True,
                no_screenshots=False, depth="everything",
            )

    def test_extracting_from_no_screenshots_is_refused(self, tmp_path):
        # The roster file is already structured. An extract-only run over it
        # would open a run record and stop, having read nothing.
        with pytest.raises(ValueError, match="needs screenshots"):
            _options_for(
                RunOptions(), directory=tmp_path, mode="all", names=[], count="",
                seed="", profile="", campaign="", offline=False, replay=True,
                no_screenshots=True, depth="extract",
            )


class TestAnExtractOnlyRun:
    """A1 and B1 through HTTP, and nothing downstream.

    This is the first *fully* offline screenshot run the app has. Design 10
    records why a replayed screenshot plan cannot be: `shippo-quotes-sf-dc.json`
    holds one lane and the fixture screenshots hold twenty-odd destinations, so
    C2 stops. Stopping after B1 never reaches C2, so the recordings that do
    exist are the only ones it needs.
    """

    @pytest.fixture
    def client(self, options, workspace):
        # B1 needs an AI Config, and offline it comes from the snapshot. The
        # committed one is copied into the workspace rather than pointed at,
        # so nothing a run does can touch a file the repo tracks.
        (workspace / "snapshot.json").write_bytes(SNAPSHOT.read_bytes())
        chosen = replace(
            options, screenshots=FIXTURE_SHOTS, extractions=EXTRACTIONS, quotes=None
        )
        return TestClient(create_app(chosen, screenshot_dir=FIXTURE_SHOTS))

    @pytest.fixture
    def finished(self, client):
        response = client.post(
            "/runs",
            data={
                "mode": "explicit",
                "screenshot": ["01-imessage-thread.png"],
                "replay": "1",
                "depth": "extract",
            },
        )
        assert response.status_code == 200
        job_id = response.url.path.rsplit("/", 1)[-1]
        state = finish(client, job_id)
        return job_id, state, client.get(f"/runs/{job_id}")

    def test_the_run_finishes(self, finished):
        _, state, _ = finished
        assert state["state"] == "finished", state["error"]

    def test_the_outcome_is_extracted_not_no_manifest(self, finished):
        # There was never going to be a manifest. Reporting the absence of one
        # would read as a disappointing run rather than a normal one.
        _, state, _ = finished
        assert state["outcome"] == "extracted"

    def test_the_page_shows_who_was_read(self, finished):
        _, _, page = finished
        assert "Ana Ruiz" in page.text
        assert "recipient(s) read" in page.text

    def test_the_page_says_which_variation_read_which_image(self, finished):
        # The only question an extraction run exists to answer, per design 6.6.
        _, _, page = finished
        assert "01-imessage-thread.png" in page.text
        assert "image key" in page.text

    def test_nothing_downstream_of_b1_ran(self, finished):
        _, _, page = finished
        assert "Manifest" not in page.text
        assert "No manifest" not in page.text

    def test_the_run_still_reaches_the_ledger(self, finished, options):
        # A1 really did open a run, and the ledger is append-only.
        job_id, _, _ = finished
        runs = (options.ledger / "runs.jsonl").read_text(encoding="utf-8")
        assert "screenshot_keys" in runs


class TestARunThatCouldNotReadEverything:
    """The case the result page used to swallow.

    A rollout served a variation whose output spec was deliberately vague, four
    of seven screenshots produced nothing parseable, and the page reported "8
    recipient(s) read" and said nothing else. A run that lost 13 of 22 people
    was indistinguishable from a healthy run of a short list.

    The reply here is synthetic and only has to be unparseable: what is under
    test is whether the summary reports the failure, not how the failure
    arises. The shape B1 actually returned under that variation is in
    `test_extraction.py`, with the parsing it needs.

    The progress log did carry it, which is why this is about the summary
    rather than about surfacing it at all: a log scrolls, and the count is what
    a reader takes away.
    """

    @pytest.fixture
    def client(self, options, workspace):
        (workspace / "snapshot.json").write_bytes(SNAPSHOT.read_bytes())
        # Two objects with no wrapper: unparseable by construction, and it
        # stays unparseable however tolerant `_parse` becomes, which is what
        # this test wants of it.
        recording = json.loads(EXTRACTIONS.read_text(encoding="utf-8"))
        recording["replies"]["01-imessage-thread.png"]["text"] = (
            '{"name": "Ana Ruiz", "street1": "1600 Pennsylvania Ave NW"}\n'
            '{"name": "Marcus Feld", "street1": "233 S Wacker Dr"}'
        )
        broken = workspace / "b1-sibling-objects.json"
        broken.write_text(json.dumps(recording), encoding="utf-8")
        chosen = replace(
            options, screenshots=FIXTURE_SHOTS, extractions=broken, quotes=None
        )
        return TestClient(create_app(chosen, screenshot_dir=FIXTURE_SHOTS))

    @pytest.fixture
    def finished(self, client):
        response = client.post(
            "/runs",
            data={
                "mode": "explicit",
                # One image that cannot be read, one that reads and contains
                # someone who gave no address. Both halves on one page.
                "screenshot": ["01-imessage-thread.png", "07-whatsapp-group.png"],
                "replay": "1",
                "depth": "extract",
            },
        )
        assert response.status_code == 200
        job_id = response.url.path.rsplit("/", 1)[-1]
        state = finish(client, job_id)
        return job_id, state, client.get(f"/runs/{job_id}")

    def test_an_unreadable_screenshot_does_not_fail_the_run(self, finished):
        # It is a partial result, not an error: the images that did read are
        # still worth showing.
        _, state, _ = finished
        assert state["state"] == "finished", state["error"]

    def test_the_page_says_how_many_screenshots_were_read(self, finished):
        _, _, page = finished
        assert "B1 read 1 of 2 screenshot(s)" in page.text

    def test_the_page_names_the_screenshot_and_why_it_failed(self, finished):
        # The reason separates a console edit from a model choice. Without it
        # every failure reads the same.
        _, _, page = finished
        assert "01-imessage-thread.png" in page.text
        assert "Extra data" in page.text

    def test_nobody_from_the_unreadable_screenshot_is_claimed_as_read(self, finished):
        # Ana and Marcus are named in the reply that could not be parsed. A
        # page that listed them would be inventing recipients.
        _, _, page = finished
        assert "Ana Ruiz" not in page.text
        assert "Marcus Feld" not in page.text

    def test_someone_who_gave_no_address_is_shown_rather_than_dropped(self, finished):
        # Design 4: a person a human needs to chase, not a failure.
        _, _, page = finished
        assert "jules_g" in page.text


class TestAWholeRun:
    """A roster in, a manifest on the page, through HTTP and a worker thread."""

    @pytest.fixture
    def finished(self, client):
        response = client.post(
            "/runs", data={"mode": "all", "no_screenshots": "1", "replay": "1"}
        )
        assert response.status_code == 200  # redirect followed
        job_id = response.url.path.rsplit("/", 1)[-1]
        finish(client, job_id)
        return job_id, client.get(f"/runs/{job_id}")

    def test_the_run_finishes(self, client, finished):
        job_id, _ = finished
        assert client.get(f"/runs/{job_id}/state").json()["state"] == "finished"

    def test_the_manifest_reaches_the_page(self, finished):
        _, page = finished
        assert "Ana Ruiz" in page.text
        assert "1600 Pennsylvania Ave NW" in page.text

    def test_the_page_shows_what_the_run_was_allowed_to_do(self, finished):
        # The capability header is the most useful thing here and the easiest
        # to lose: in the CLI it scrolls past above the manifest.
        _, page = finished
        assert "baseline" in page.text
        # Every capability the run resolved, by name -- the table is built
        # from `to_mapping`, so a capability removed from the repo drops off
        # the page without the template knowing anything changed.
        assert "planner" in page.text
        assert "validation" in page.text
        assert "verification" in page.text

    def test_the_plain_text_manifest_is_the_same_artifact(self, client, finished):
        job_id, _ = finished
        text = client.get(f"/runs/{job_id}/manifest.txt").text
        assert "Ana Ruiz" in text
        assert "total cost" in text

    def test_the_run_reached_the_ledger(self, client, finished, workspace):
        # A run that planned and recorded nothing would look identical on the
        # page. Design 7: the JSONL is the source of truth, not this process.
        from bbq_shipment_agent.ledger import RunRecord, iter_records

        records = list(iter_records(workspace / "ledger", RunRecord))
        assert any(r.packet_count == 1 for r in records)

    def test_the_events_are_replayable_after_the_fact(self, client, finished):
        job_id, _ = finished
        body = client.get(f"/runs/{job_id}/events").text
        assert "roster" in body
        assert '"kind": "end"' in body


class TestOneRunAtATime:
    def test_a_second_run_is_refused_rather_than_queued(self, options, workspace):
        # Two runs would append to the same append-only ledger and quote the
        # same lanes twice, and the operator asking has almost always
        # double-clicked. Queueing would look like nothing happened.
        service = RunService()
        app = create_app(options, screenshot_dir=workspace / "shots", service=service)
        client = TestClient(app)

        class Never:
            """A job that never finishes, so the second POST always collides."""

            id = "held"
            running = True
            state = "running"

        service._jobs["held"] = Never()
        service._order.append("held")

        response = client.post(
            "/runs",
            data={"mode": "all", "no_screenshots": "1", "replay": "1"},
            follow_redirects=False,
        )
        assert response.status_code == 409
        assert "one at a time" in response.json()["detail"].lower()


class TestFailuresAreShownNotSwallowed:
    def test_a_broken_roster_becomes_a_failed_run_with_a_reason(
        self, options, workspace
    ):
        # The worker runs on a background thread. An exception there would
        # otherwise go to a terminal nobody is reading while the page sat on
        # a spinner forever.
        (workspace / "recipients.yaml").write_text("origin: {}", encoding="utf-8")
        client = TestClient(create_app(options, screenshot_dir=workspace / "shots"))
        response = client.post(
            "/runs", data={"mode": "all", "no_screenshots": "1", "replay": "1"}
        )
        job_id = response.url.path.rsplit("/", 1)[-1]
        state = finish(client, job_id)
        assert state["state"] == "failed"
        assert state["error"]
        assert "stopped" in client.get(f"/runs/{job_id}").text

    def test_an_unknown_run_is_a_404_not_a_crash(self, client):
        assert client.get("/runs/nope").status_code == 404
        assert client.get("/runs/nope/events").status_code == 404


class TestTheMissingKeyBanner:
    """A server that starts cleanly implies it is ready to run."""

    def test_the_page_says_which_key_is_missing(self, options, workspace, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SHIPPO_API_KEY", "")
        live = _options_for(
            options, directory=workspace / "shots", mode="all", names=[], count="",
            seed="", profile="", campaign="", offline=True, replay=False,
            no_screenshots=False,
        )
        client = TestClient(create_app(live, screenshot_dir=workspace / "shots"))
        body = client.get("/").text
        assert "A live run will fail" in body
        assert "ANTHROPIC_API_KEY" in body
        # And says the thing that is actually wrong nine times out of ten.
        assert "UV_ENV_FILE" in body

    def test_no_banner_when_everything_is_replayed(self, options, workspace, monkeypatch):
        # Every live path has a recording, so no key is reached for and there
        # is nothing to warn about. Note the launch options alone are not
        # enough: B1 and B3 need --extractions and --repairs too, which is
        # exactly what `replaying` insists on.
        import json

        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SHIPPO_API_KEY", "")
        (workspace / "b1.json").write_text(json.dumps({"replies": {}}))
        (workspace / "b3.json").write_text(json.dumps({"turns": []}))
        replayed = replace(
            options,
            extractions=workspace / "b1.json",
            repairs=workspace / "b3.json",
        )
        client = TestClient(create_app(replayed, screenshot_dir=workspace / "shots"))
        assert "A live run will fail" not in client.get("/").text

    def test_the_fixture_launch_does_warn_about_the_vision_stages(
        self, client, monkeypatch
    ):
        # The opposite case, pinned because it is the easy mistake: quotes,
        # validations and completions recorded looks like a free run, and it
        # is not -- B1 and B3 still call a model.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        body = client.get("/").text
        assert "B1 extraction" in body
