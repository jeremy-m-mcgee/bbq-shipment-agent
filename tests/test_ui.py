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
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bbq_shipment_agent.agents.model import Completion
from bbq_shipment_agent.ledger import (
    AgentInvocationRecord,
    RunRecord,
    ShipmentRecord,
    iter_records,
)
from bbq_shipment_agent.ui import RunService, create_app
from bbq_shipment_agent.ui.app import _options_for
from bbq_shipment_agent.ui.modes import describe
from bbq_shipment_agent.ui.view import screenshot_catalogue
from bbq_shipment_agent.wiring import RunDepth, RunOptions


class ScriptedModel:
    """A `ConversingModel` that replays canned completions, for driving the D2
    review offline. Injected as the `conversing_model` factory so a review can
    hold a real conversation with no socket and no key -- the same shape as
    test_narrator.py's, kept here so the UI suite stands alone."""

    def __init__(self, *completions: object) -> None:
        self.completions = list(completions)
        self.calls = 0

    def converse(
        self,
        invocation: object,
        messages: object,
        tools: object = (),
        on_delta: object = None,
    ) -> object:
        self.calls += 1
        nxt = self.completions.pop(0) if self.completions else Completion(text="done")
        if isinstance(nxt, Exception):
            raise nxt
        # Emulate streaming: hand the reply back through the delta channel in a
        # couple of chunks, the way `AnthropicModel` forwards content deltas.
        if on_delta is not None and nxt.text:
            middle = max(1, len(nxt.text) // 2)
            on_delta(nxt.text[:middle])
            on_delta(nxt.text[middle:])
        return nxt


def _park(client: TestClient) -> str:
    """Start a whole roster run through HTTP and wait for it to park in review.

    The roster is one recipient, so the plan covers and the run reaches D2
    rather than finishing -- which is the state every review test starts from."""
    response = client.post(
        "/runs", data={"mode": "all", "no_screenshots": "1", "replay": "1"}
    )
    assert response.status_code == 200
    job_id = response.url.path.rsplit("/", 1)[-1]
    state = finish(client, job_id)
    assert state["state"] == "awaiting_review", state
    return job_id

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = FIXTURES / "shippo-quotes-sf-dc.json"
VALIDATIONS = FIXTURES / "shippo-addresses.json"
COMPLETIONS = FIXTURES / "d1-completions.json"
EXTRACTIONS = FIXTURES / "b1-extractions.json"
FIXTURE_SHOTS = FIXTURES / "screenshots"
SNAPSHOT = Path(__file__).parent.parent / "config" / "ld-snapshot.json"

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


POOL = """
operators:
  - key: dana
    department: ops
  - key: priya
    department: kitchen
"""


@pytest.fixture
def with_operators(options, workspace):
    """A client whose form offers a pool of two."""
    (workspace / "operators.yaml").write_text(POOL, encoding="utf-8")
    return TestClient(
        create_app(
            replace(options, operators=workspace / "operators.yaml"),
            screenshot_dir=workspace / "shots",
        )
    )


class TestTheOperatorField:
    """Who the run claims to be, as the `user` context kind.

    The department is deliberately not on the form. It is looked up in
    `config/operators.yaml`, so a posted one could disagree with the file a
    targeting rule was written against.
    """

    def test_the_form_offers_the_committed_pool(self, with_operators):
        body = with_operators.get("/").text
        assert 'value="dana"' in body and "kitchen" in body

    def test_the_driver_can_read_the_population_off_the_real_page(
        self, with_operators
    ):
        # The contract between this template and `drive.OPERATOR`. The driver
        # learns the pool from the page rather than the config file, so a
        # markup change that broke the parse would otherwise only show up in
        # a live session.
        from bbq_shipment_agent.drive import operators_in

        assert operators_in(with_operators.get("/").text) == ("dana", "priya")

    def test_the_department_is_not_a_form_field(self, with_operators):
        assert 'name="department"' not in with_operators.get("/").text

    def test_nobody_is_selected_by_default(self, with_operators):
        body = with_operators.get("/").text
        assert "none &mdash; no user context" in body or "no user context" in body

    def test_a_key_nobody_listed_is_refused_by_the_post(self, with_operators):
        # A 400 on the form that sent it, rather than a run that opens, spends
        # a vision call and then dies on an unknown key.
        response = with_operators.post(
            "/runs", data={"mode": "all", "depth": "plan", "operator": "mallory"}
        )
        assert response.status_code == 400
        assert "not an operator" in response.json()["detail"]

    def test_a_page_with_no_pool_offers_no_field(self, options, workspace):
        client = TestClient(
            create_app(
                replace(options, operators=workspace / "absent.yaml"),
                screenshot_dir=workspace / "shots",
            )
        )
        assert 'name="operator"' not in client.get("/").text


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
            "../recipients.yaml",
            "..%2Frecipients.yaml",
            "....//recipients.yaml",
            "/etc/passwd",
        ],
    )
    def test_nothing_outside_the_directory_can_be_reached(self, client, name):
        # The name is looked up in a listing of the directory, never joined
        # onto it. There is no reason to be within arm's reach of a traversal
        # bug to show seven PNGs -- and the sibling `recipients.yaml` holds real
        # home addresses.
        response = client.get(f"/screenshots/{name}")
        assert response.status_code in {404, 400, 405}
        assert b"Pennsylvania" not in response.content

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

    def test_the_operator_reaches_the_options_as_a_key(self, tmp_path):
        # A key and never a pair: `open_run` resolves it against the pool, so
        # the department cannot arrive from the browser.
        options = self.base(tmp_path, operator="priya")
        assert options.operator == "priya"
        assert not hasattr(options, "department")

    def test_naming_nobody_leaves_the_run_unattributed(self, tmp_path):
        assert self.base(tmp_path, operator="").operator is None

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
        assert "B1 could not read" in page.text
        assert "1 of\n  2 screenshot(s)" in page.text or "1 of 2 screenshot(s)" in page.text

    def test_the_two_kinds_of_gap_are_separate_headings(self, finished):
        """An unreadable image and a person with no address are different
        problems for different people. They shared one heading, in the error
        colour, headed with a ratio that only described the first."""
        _, _, page = finished
        assert "Read from a screenshot, no usable address" in page.text
        # The unresolved heading is not styled as a failure: B1 read the thread
        # correctly, and design 4 sends the person to a human to chase.
        unresolved = page.text.index("Read from a screenshot, no usable address")
        heading = page.text.rindex("<h2", 0, unresolved)
        assert 'class="bad"' not in page.text[heading:unresolved]

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

    def test_the_run_parks_in_review(self, client, finished):
        # A covering plan does not finish -- it parks in D2. Design 4 makes the
        # review the pipeline's last stage, and design 10 puts it in the browser
        # as a pane beside the manifest rather than a second manifest.
        job_id, _ = finished
        assert client.get(f"/runs/{job_id}/state").json()["state"] == "awaiting_review"

    def test_the_review_pane_is_on_the_page(self, finished):
        _, page = finished
        assert 'id="review-pane"' in page.text
        # Offline: no model, so the review is button-driven, and the page says so.
        assert "button-driven" in page.text
        # The approval and edit controls are present.
        assert "/review/approve" in page.text
        assert "/review/edit" in page.text

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

    def test_the_page_states_the_two_hard_constraints(self, finished):
        """Design 3's constraints, beside the numbers they produced.

        The page showed a 4.40C margin and a carrier set and stated neither
        rule, so a reader could not tell which numbers were policy."""
        _, page = finished
        assert "arrive at or below" in page.text
        assert "4.4C" in page.text
        assert "at most 2" in page.text

    def test_the_margin_column_names_what_it_is_a_margin_of(self, finished):
        _, page = finished
        assert "margin of 4.4C" in page.text

    def test_the_runner_up_table_says_what_it_is_a_subset_of(self, finished):
        # "Pairs" was wrong twice: one carrier can cover a run, and the table
        # only holds the subsets that covered. Two rows with no denominator
        # read as an arbitrary sample of design 4's six candidates.
        _, page = finished
        assert "carrier subsets" in page.text
        assert "quoted this run" in page.text
        assert "covered every recipient" in page.text

    def test_both_identifiers_are_on_the_page_and_labelled(self, client, finished):
        # The ledger knows one string, the browser knows another, and they had
        # never appeared together.
        job_id, page = finished
        assert "ledger id" in page.text
        assert f"/runs/{job_id}" in page.text

    def test_the_profile_is_named_as_a_targeting_label(self, finished):
        # "profile baseline" reads like a bundle of settings, and there is no
        # such bundle any more -- it is what LaunchDarkly targets on.
        _, page = finished
        assert "targeting" in page.text

    def test_a_mode_says_what_it_did_to_this_run(self, finished):
        # B2 ran, so its row says what it did. The planner and verification are
        # off offline, and an `off` mode already says nothing ran -- repeating
        # that as a this-run line is noise, so it is not there.
        _, page = finished
        assert "this run: 0 corrected" in page.text
        assert "this run: no repair was attempted" not in page.text

    def test_d1_is_rendered_above_the_review_controls(self, finished):
        """Design 4 has D1 critique the manifest before a human sees it.

        Below the Approve button is the one placement that cannot."""
        _, page = finished
        assert page.text.index("D1 verification") < page.text.index("/review/approve")

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

        class Parked:
            """A job parked in review, so the second POST always collides.

            `awaiting_review` is the case that matters: a parked review still
            holds the slot (`occupies_slot`) even though the worker thread is
            done, because until it reaches a terminal state the ledger and the
            lanes are still that run's. A fake keyed on `running` would have
            let a second run through the moment planning finished."""

            id = "held"
            review = None
            state = "awaiting_review"
            occupies_slot = True

        service._jobs["held"] = Parked()
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


class TestTheFormExplainsTheRunItConfigures:
    """The page's first screen, for someone who has not read the design doc.

    It opened straight into a picker captioned "B1 reads these" and priced only
    the vision calls -- and priced those only on a server launched with
    recordings, which is the one server where the price is zero.
    """

    def test_it_says_what_the_app_does_before_the_first_control(self, client):
        body = client.get("/").text
        assert "plans one batch of frozen barbecue packets" in body
        # The stage codes the panels and the design doc both use.
        for stage in ("B1", "B2", "D1", "D2"):
            assert f"<b>{stage}</b>" in body

    def test_it_says_no_label_is_bought(self, client):
        assert "no label is bought" in client.get("/").text

    def test_it_names_the_food_safety_threshold(self, client):
        # The gate every plan on the results page is measured against.
        assert "4.4C" in client.get("/").text

    def test_it_says_the_modes_are_not_this_forms_to_choose(self, client):
        assert "decided by LaunchDarkly when that stage runs" in client.get("/").text

    def test_it_says_what_the_roster_supplies_on_a_screenshot_run(self, client):
        body = client.get("/").text
        assert "the origin address, the candidate ship dates" in body

    def test_a_live_server_prices_the_whole_run_not_just_the_images(
        self, options, workspace
    ):
        """The cost warning used to live on the replay checkbox, which a live
        server does not render at all."""
        live = _options_for(
            options, directory=workspace / "shots", mode="all", names=[], count="",
            seed="", profile="", campaign="", offline=True, replay=False,
            no_screenshots=False,
        )
        client = TestClient(create_app(live, screenshot_dir=workspace / "shots"))
        body = client.get("/").text
        assert "replay recordings" not in body  # no recordings, so no control
        assert "rate quotes" in body
        assert "Shippo validation per recipient" in body

    def test_a_replaying_server_says_there_is_nothing_to_spend(self, client):
        # The fixture client is launched with recordings, so the box is ticked
        # and the page says so before the button rather than after the run.
        assert "no live calls, and nothing to spend" in client.get("/").text


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

    def test_the_remedy_covers_installs_that_are_not_uv(
        self, options, workspace, monkeypatch
    ):
        """`UV_ENV_FILE` is a uv setting, and nothing here reads `.env` itself.

        On a pip or poetry install the uv line changes nothing: the operator
        follows it, sees this banner again, and has no next step. Both remedies
        are named rather than detected."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SHIPPO_API_KEY", "")
        live = _options_for(
            options, directory=workspace / "shots", mode="all", names=[], count="",
            seed="", profile="", campaign="", offline=True, replay=False,
            no_screenshots=False,
        )
        client = TestClient(create_app(live, screenshot_dir=workspace / "shots"))
        body = client.get("/").text
        assert "UV_ENV_FILE" in body
        assert "source .env" in body

    def test_the_fixture_launch_does_warn_about_the_vision_stages(
        self, client, monkeypatch
    ):
        # The opposite case, pinned because it is the easy mistake: quotes,
        # validations and completions recorded looks like a free run, and it
        # is not -- B1 and B3 still call a model.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        body = client.get("/").text
        assert "B1 extraction" in body


class TestTheReviewIsButtonDrivenOffline:
    """D2 in the browser with no model: edits and approval through buttons.

    Offline there is no `ConversingModel`, so `conversing_model` raises and the
    review parks with no narrator -- the same fallback the CLI takes. Every
    decision is still `ReviewSession`'s; these drive it through the routes."""

    def test_a_pin_to_the_only_candidate_date_applies(self, client):
        job_id = _park(client)
        response = client.post(
            f"/runs/{job_id}/review/edit",
            data={"kind": "ship_date", "recipient_key": "ana", "ship_date": "2026-08-17"},
        )
        assert response.status_code == 200
        # The only ship date, so the plan does not move: applied, not pending.
        assert "Applied" in response.text

    def test_a_date_that_is_not_a_candidate_is_a_400_not_a_dead_review(self, client):
        # A bad edit comes back as a message, not a crash -- the review survives.
        job_id = _park(client)
        response = client.post(
            f"/runs/{job_id}/review/edit",
            data={"kind": "ship_date", "recipient_key": "ana", "ship_date": "2020-01-01"},
        )
        assert response.status_code == 400
        assert "candidate ship date" in response.json()["detail"]

    def test_an_edit_on_someone_not_in_the_run_is_a_400(self, client):
        job_id = _park(client)
        response = client.post(
            f"/runs/{job_id}/review/edit",
            data={"kind": "exclude", "recipient_key": "nobody"},
        )
        assert response.status_code == 400

    def test_excluding_the_only_recipient_empties_the_plan_without_a_500(self, client):
        # Regression: an exclusion that removes the last shipment leaves no
        # covering plan, so the re-solve's cost delta is None. `describe` used to
        # format None into a float and 500 -- found by clicking Exclude in a real
        # browser. The route must re-render the pane, not crash.
        job_id = _park(client)
        response = client.post(
            f"/runs/{job_id}/review/edit",
            data={"kind": "exclude", "recipient_key": "ana"},
        )
        assert response.status_code == 200
        # Emptying the run moves the carrier set to none, so it is proposed for
        # confirmation, and the banner says there is no plan left rather than
        # formatting a None cost.
        assert "Confirm this change" in response.text
        assert "no covering plan after this" in response.text

    def test_say_is_refused_when_the_review_is_button_driven(self, client):
        # There is no narrator to ask; the route says so rather than 500ing.
        job_id = _park(client)
        response = client.post(f"/runs/{job_id}/review/say", data={"message": "hi"})
        assert response.status_code == 409

    def test_approve_writes_the_shipments_and_frees_the_slot(self, client, workspace):
        job_id = _park(client)
        response = client.post(f"/runs/{job_id}/review/approve")
        assert response.status_code == 200
        assert "Approved" in response.text
        assert client.get(f"/runs/{job_id}/state").json()["state"] in {
            "approved",
            "approved_with_exclusions",
        }
        # E2: one shipment row per assignment reaches the ledger.
        ships = list(iter_records(workspace / "ledger", ShipmentRecord))
        assert any(s.recipient_key == "ana" for s in ships)
        # The slot is freed only now, at the terminal state -- a new run is taken.
        again = client.post(
            "/runs",
            data={"mode": "all", "no_screenshots": "1", "replay": "1"},
            follow_redirects=False,
        )
        assert again.status_code == 303

    def test_approving_keeps_the_approved_manifest_on_the_page(self, client):
        """The approved plan is the deliverable (design 1), so it stays put.

        The terminal branch used to replace the whole pane with a one-line
        notice, so the moment of approval was the moment the manifest -- the
        thing the operator buys labels from -- left the screen."""
        job_id = _park(client)
        client.post(f"/runs/{job_id}/review/approve")
        page = client.get(f"/runs/{job_id}")
        assert "Approved" in page.text
        assert "Ana Ruiz" in page.text
        assert "1600 Pennsylvania Ave NW" in page.text

    def test_the_approved_page_still_links_the_plain_text_manifest(self, client):
        job_id = _park(client)
        client.post(f"/runs/{job_id}/review/approve")
        page = client.get(f"/runs/{job_id}")
        assert f"/runs/{job_id}/manifest.txt" in page.text

    def test_a_parked_review_links_the_plain_text_manifest(self, client):
        # The link lived in the `elif m` arm, which never renders once a review
        # exists -- so on every covering run nothing on the page linked to it.
        job_id = _park(client)
        page = client.get(f"/runs/{job_id}")
        assert f"/runs/{job_id}/manifest.txt" in page.text
        assert client.get(f"/runs/{job_id}/manifest.txt").status_code == 200

    def test_the_text_manifest_is_the_reviews_not_the_planned_one(self, client):
        # A pin that re-solves must reach the export; the run row's copy was
        # rendered at plan time and cannot describe a plan the operator edited.
        job_id = _park(client)
        client.post(
            f"/runs/{job_id}/review/edit",
            data={"kind": "ship_date", "recipient_key": "ana", "ship_date": "2026-08-17"},
        )
        text = client.get(f"/runs/{job_id}/manifest.txt").text
        assert "Ana Ruiz" in text
        assert "2026-08-17" in text or "17 Aug" in text

    def test_reject_records_the_run_but_writes_no_shipments(self, client, workspace):
        job_id = _park(client)
        response = client.post(f"/runs/{job_id}/review/reject", data={"reason": "nope"})
        assert response.status_code == 200
        assert "Rejected" in response.text
        assert list(iter_records(workspace / "ledger", ShipmentRecord)) == []
        # The run was closed: a run record carries completed_at.
        assert any(
            r.completed_at for r in iter_records(workspace / "ledger", RunRecord)
        )

    def test_abandon_records_nothing_and_frees_the_slot(self, client, workspace):
        job_id = _park(client)
        before = len(list(iter_records(workspace / "ledger", RunRecord)))
        response = client.post(f"/runs/{job_id}/review/abandon")
        assert response.status_code == 200
        assert "abandoned" in response.text.lower()
        assert len(list(iter_records(workspace / "ledger", RunRecord))) == before
        assert list(iter_records(workspace / "ledger", ShipmentRecord)) == []
        again = client.post(
            "/runs",
            data={"mode": "all", "no_screenshots": "1", "replay": "1"},
            follow_redirects=False,
        )
        assert again.status_code == 303

    def test_a_parked_review_still_refuses_a_second_run(self, client):
        # `occupies_slot`, end to end: the worker thread is done but the review
        # is open, so the run still holds the ledger and the lanes.
        _park(client)
        again = client.post(
            "/runs",
            data={"mode": "all", "no_screenshots": "1", "replay": "1"},
            follow_redirects=False,
        )
        assert again.status_code == 409
        assert "one at a time" in again.json()["detail"].lower()

    def test_the_routes_refuse_once_the_review_has_ended(self, client):
        job_id = _park(client)
        client.post(f"/runs/{job_id}/review/reject")
        # A stale tab POSTing into a finished review is told, not crashed.
        response = client.post(
            f"/runs/{job_id}/review/edit",
            data={"kind": "exclude", "recipient_key": "ana"},
        )
        assert response.status_code == 409


class TestOneReviewTurnAtATime:
    @pytest.fixture
    def service(self):
        return RunService()

    @pytest.fixture
    def client(self, options, workspace, service):
        return TestClient(
            create_app(options, screenshot_dir=workspace / "shots", service=service)
        )

    def test_an_overlapping_turn_is_refused_not_interleaved(self, client, service):
        # Held lock stands in for a turn already running: a double-submit gets a
        # 409, not a second copy of the edit interleaved with the first.
        job_id = _park(client)
        controller = service._jobs[job_id].review
        assert controller.turn_lock.acquire(blocking=False)
        try:
            response = client.post(
                f"/runs/{job_id}/review/edit",
                data={"kind": "exclude", "recipient_key": "ana"},
            )
            assert response.status_code == 409
            assert "already in progress" in response.json()["detail"]
        finally:
            controller.turn_lock.release()


class TestTheConversationalReview:
    """D2 with a narrator, driven by an injected scripted model -- no socket."""

    @pytest.fixture
    def conv(self, options, workspace):
        # The narrator's config comes from the snapshot offline; the model is
        # injected. Together they give a real conversation with no network.
        (workspace / "snapshot.json").write_bytes(SNAPSHOT.read_bytes())
        model = ScriptedModel(
            Completion(text="USPS wins; the runners-up cost more."),
            Completion(text="The run costs about $55."),
        )
        service = RunService(conversing_model=lambda options: model)
        client = TestClient(
            create_app(options, screenshot_dir=workspace / "shots", service=service)
        )
        return client, model

    def test_the_opening_narration_is_on_the_first_render(self, conv):
        client, _ = conv
        job_id = _park(client)
        page = client.get(f"/runs/{job_id}").text
        # `narrator.open()` ran on the worker thread, so the pane already has it.
        assert "USPS wins" in page
        # And the chat box is offered, because a narrator is available.
        assert 'name="message"' in page

    def test_a_turn_shows_the_prompt_and_the_reply(self, conv):
        client, _ = conv
        job_id = _park(client)
        response = client.post(
            f"/runs/{job_id}/review/say", data={"message": "what does it cost?"}
        )
        assert response.status_code == 200
        assert "what does it cost?" in response.text  # the prompt, echoed
        assert "The run costs about $55." in response.text  # the reply

    def test_every_turn_is_one_narrator_invocation_in_the_ledger(self, conv, workspace):
        # Design 8's operator-edit metric needs a line per turn. `open` is one,
        # the say is a second.
        client, _ = conv
        job_id = _park(client)
        client.post(f"/runs/{job_id}/review/say", data={"message": "cost?"})
        records = [
            r
            for r in iter_records(workspace / "ledger", AgentInvocationRecord)
            if r.agent_key == "review-narrator"
        ]
        assert len(records) == 2

    def test_a_streamed_turn_delivers_deltas_then_the_final_pane(self, conv):
        # The SSE variant: the reply arrives as `delta` events, then a `done`
        # event carrying the re-rendered pane the client swaps in.
        client, _ = conv
        job_id = _park(client)
        body = client.post(
            f"/runs/{job_id}/review/say-stream", data={"message": "what does it cost?"}
        ).text
        assert '"delta"' in body  # streamed as it arrived
        assert '"done": true' in body  # final event
        # The reply text streamed, and the final pane carries the exchange.
        assert "The run costs about $55." in body
        assert "what does it cost?" in body

    def test_a_streamed_turn_is_still_one_ledger_invocation(self, conv, workspace):
        # Streaming is a side channel: the turn records exactly as the sync one
        # does. open() + one streamed say = two review-narrator invocations.
        client, _ = conv
        job_id = _park(client)
        client.post(
            f"/runs/{job_id}/review/say-stream", data={"message": "cost?"}
        ).read()
        records = [
            r
            for r in iter_records(workspace / "ledger", AgentInvocationRecord)
            if r.agent_key == "review-narrator"
        ]
        assert len(records) == 2


class TestALaunchDarklyModelSwap:
    """Changing which model `review-narrator` runs on is a console edit, not a
    redeploy or a restart. Two sequential runs against the same live server pick
    up a snapshot the swap edited between them -- design 6.1, model.py."""

    def test_the_next_run_picks_up_the_new_model_with_no_restart(
        self, options, workspace
    ):
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        snap_path = workspace / "snapshot.json"

        def serve_model(name):
            snapshot["agents"]["review-narrator"]["model"] = name
            snap_path.write_text(json.dumps(snapshot), encoding="utf-8")

        serve_model("claude-before-swap")
        service = RunService(
            conversing_model=lambda options: ScriptedModel(Completion(text="ok"))
        )
        # One server, started once. Everything below happens without touching it.
        client = TestClient(
            create_app(options, screenshot_dir=workspace / "shots", service=service)
        )

        def narrator_models():
            return {
                r.model
                for r in iter_records(workspace / "ledger", AgentInvocationRecord)
                if r.agent_key == "review-narrator"
            }

        job1 = _park(client)
        assert narrator_models() == {"claude-before-swap"}
        client.post(f"/runs/{job1}/review/approve")  # free the slot

        # The swap: a different model served, mid-session, no redeploy.
        serve_model("claude-after-swap")
        _park(client)
        assert "claude-after-swap" in narrator_models()


class TestNarrationMarkdown:
    """The narrator emits light markdown; the review pane renders it safely."""

    def test_bold_italic_and_code_render(self):
        from bbq_shipment_agent.ui.view import render_markdown

        out = str(render_markdown("**UPS** at **$55.56**, not *cheaper* — `extra_cost`"))
        assert "<strong>UPS</strong>" in out
        assert "<strong>$55.56</strong>" in out
        assert "<em>cheaper</em>" in out
        assert "<code>extra_cost</code>" in out

    def test_html_in_a_reply_is_escaped_not_executed(self):
        from bbq_shipment_agent.ui.view import render_markdown

        out = str(render_markdown("<script>alert(1)</script> **x**"))
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
        assert "<strong>x</strong>" in out  # the real markdown still renders


class TestTheReviewControllerClosesItsClientOnce:
    def test_close_and_abandon_are_idempotent(self):
        from bbq_shipment_agent.ui import ReviewController

        class Context:
            def __init__(self) -> None:
                self.client = self
                self.closes = 0

            def close(self) -> None:
                self.closes += 1

        context = Context()
        controller = ReviewController(context, session=None, narrator=None)
        controller.close()
        controller.close()
        controller.abandon()
        assert context.closes == 1


class TestModeDescriptor:
    """`modes.describe` is the one place the effect of a mode value is written.

    A pure function, so it is pinned without a browser or a run -- the same
    reason the view layer is plain data.
    """

    def test_it_names_the_stage_a_mode_gates(self):
        assert describe("validation", "standard")["stage"] == "B2"
        assert describe("planner", "off")["stage"] == "B3"
        assert describe("verification", "on")["stage"] == "D1"

    def test_it_says_what_the_value_does(self):
        assert "Escalates" in describe("validation", "strict")["effect"]
        assert "not applied" in describe("planner", "shadow")["effect"]

    def test_a_partial_state_reads_as_a_warning_not_an_on(self):
        # `shadow` runs but changes nothing, so it is neither off nor on.
        assert describe("planner", "shadow")["tone"] == "warn"
        assert describe("planner", "off")["tone"] == ""
        assert describe("validation", "standard")["tone"] == "on"

    def test_a_value_with_no_line_still_renders(self):
        # A capability added to the repo without an entry here is visible and
        # obviously undescribed, rather than dropping off the page.
        described = describe("planner", "experimental")
        assert described["value"] == "experimental"
        assert "default" in described["effect"]


class TestThereIsNoProfileControl:
    """The profile is only a LaunchDarkly targeting label, so the human form
    does not offer it. The modes it targets are shown on the run page, not
    chosen here."""

    def test_the_form_has_no_profile_control(self, client):
        body = client.get("/").text
        assert 'name="profile"' not in body
        # And none of the old option labels linger as dead markup.
        assert 'value="baseline"' not in body
        assert 'value="planner_trial"' not in body

    def test_the_post_still_accepts_a_profile_for_the_driver(self, client):
        # `drive` posts a profile per run as its rollout axis, so the endpoint
        # must keep taking one even though the browser form does not send it.
        response = client.post(
            "/runs",
            data={"mode": "all", "no_screenshots": "1", "replay": "1",
                  "profile": "planner_trial"},
        )
        assert response.status_code == 200  # accepted, not a 4xx


class TestTheModePanel:
    """The run page says what each mode did, not just its name."""

    @pytest.fixture
    def finished(self, client):
        response = client.post(
            "/runs", data={"mode": "all", "no_screenshots": "1", "replay": "1"}
        )
        assert response.status_code == 200
        job_id = response.url.path.rsplit("/", 1)[-1]
        finish(client, job_id)
        return client.get(f"/runs/{job_id}")

    def test_each_mode_is_labelled_with_the_stage_it_gates(self, finished):
        text = finished.text
        assert ">B2<" in text and ">B3<" in text and ">D1<" in text

    def test_it_spells_out_what_the_resolved_value_does(self, finished):
        # Offline resolves planner off, validation standard, verification off.
        # Substrings that dodge the apostrophe Jinja escapes to `&#39;`.
        text = finished.text
        assert "Applies the validator" in text  # validation standard
        assert "Never runs" in text  # planner off
        assert "reviewed by a human" in text  # verification off

    def test_it_says_the_values_belong_to_launchdarkly(self, finished):
        assert "decided by LaunchDarkly" in finished.text
