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

import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bbq_shipment_agent.ui import RunService, create_app
from bbq_shipment_agent.ui.app import _options_for
from bbq_shipment_agent.ui.view import screenshot_catalogue
from bbq_shipment_agent.wiring import RunOptions

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = FIXTURES / "shippo-quotes-sf-dc.json"
VALIDATIONS = FIXTURES / "shippo-addresses.json"
COMPLETIONS = FIXTURES / "d1-completions.json"

CONFIG = """
    profiles:
      baseline:
        planner: "off"
        memory: "off"
        validation: "standard"
        verification: "off"
        authority: "propose_only"
    default_profile: "baseline"
    authority_ceiling: "propose_only"
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
        assert "propose_only" in page.text
        assert "baseline" in page.text

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
