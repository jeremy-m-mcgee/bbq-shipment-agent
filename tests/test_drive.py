"""The load driver, against a fake app and a fake clock.

Two things are worth testing here and they are not the HTTP. One is *what
makes the runs different from each other*, which is the entire reason the
command exists -- a driver that quietly sent the same form twenty times would
look identical from the outside and measure nothing. The other is the pacing,
which has to respect a server that refuses a second concurrent run: the
interesting case is a run that outlives the interval, and it only gets
asserted rather than described because `sleep` and `clock` are parameters.

`FakeApp` speaks the same two verbs `HttpTransport` does, so nothing here
opens a socket -- the same property the rest of the suite has.
"""

import json
import random
import re

import pytest

from bbq_shipment_agent.drive import (
    DriveError,
    DriveOptions,
    Response,
    catalogue_of,
    drive,
    operators_of,
    plan_request,
)

IMAGES = ("one.png", "two.png", "three.png", "four.png")
PEOPLE = ("dana", "priya", "marcus")


class Clock:
    """Time as a number the test moves. `sleep` is the only thing that moves it."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeApp:
    """Enough of the UI to drive: a picker page, a start, and a state poll.

    `duration` is how long a run takes, in the clock's units, and it is what
    makes the one-at-a-time rule observable -- a POST arriving while a job is
    still running gets the 409 the real `RunService` would send.
    """

    def __init__(self, images=IMAGES, duration=0.0, operators=()):
        self.images = images
        self.operators = operators
        self.duration = duration
        self.clock = Clock()
        self.posts: list[list[tuple[str, str]]] = []
        self.jobs: dict[str, float] = {}
        self.starts: list[float] = []
        self.state = "finished"
        self.page_status = 200

    def get(self, path: str) -> Response:
        if path == "/":
            body = "".join(
                f'<input type="checkbox" name="screenshot" value="{name}">'
                for name in self.images
            ) + "".join(
                f'<option value="{key}" data-operator="{key}">{key}</option>'
                for key in self.operators
            )
            return Response(self.page_status, body, "/")
        match = re.match(r"/runs/([0-9a-f]+)/state$", path)
        if match is None:
            return Response(404, "", path)
        ends = self.jobs[match.group(1)]
        running = self.clock.now < ends
        return Response(
            200,
            json.dumps(
                {
                    "id": match.group(1),
                    "state": "running" if running else self.state,
                    "error": None,
                    "outcome": None if running else "manifest",
                }
            ),
            path,
        )

    def post(self, path: str, fields: list[tuple[str, str]]) -> Response:
        assert path == "/runs"
        if any(ends > self.clock.now for ends in self.jobs.values()):
            return Response(409, json.dumps({"detail": "one at a time"}), path)
        job_id = f"{len(self.jobs) + 1:012x}"
        self.jobs[job_id] = self.clock.now + self.duration
        self.starts.append(self.clock.now)
        self.posts.append(fields)
        return Response(200, "", f"http://127.0.0.1:8765/runs/{job_id}")

    # Convenience for the assertions below.
    def sent(self, index: int) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key, value in self.posts[index]:
            out.setdefault(key, []).append(value)
        return out


def run(app: FakeApp, **kwargs):
    options = DriveOptions(seed=7, poll=1.0, **kwargs)
    return drive(
        options,
        app,
        say=lambda _: None,
        sleep=app.clock.sleep,
        clock=lambda: app.clock.now,
    )


class TestTheCatalogue:
    """Which images to pick from, read off the picker's own page."""

    def test_the_checkboxes_are_the_catalogue(self):
        assert catalogue_of(FakeApp()) == IMAGES

    def test_a_page_that_is_not_the_app_says_so(self):
        app = FakeApp()
        app.page_status = 500
        with pytest.raises(DriveError, match="front page"):
            catalogue_of(app)

    def test_a_directory_with_no_images_is_not_an_error(self):
        assert catalogue_of(FakeApp(images=())) == ()


class TestWhatVaries:
    """The substance: consecutive runs must not be the same request."""

    def test_a_sample_names_a_count_and_a_seed(self):
        app = FakeApp()
        run(app, runs=1, every=0)
        sent = app.sent(0)
        assert sent["mode"] == ["sample"]
        assert 1 <= int(sent["count"][0]) <= len(IMAGES)
        assert sent["seed"]  # printed and posted, so the sample is repeatable

    def test_consecutive_samples_differ(self):
        app = FakeApp()
        run(app, runs=8, every=0)
        seeds = {app.sent(i)["seed"][0] for i in range(8)}
        assert len(seeds) == 8

    def test_explicit_picks_are_files_the_app_offered(self):
        app = FakeApp()
        run(app, runs=4, every=0, vary="explicit")
        for i in range(4):
            picked = app.sent(i)["screenshot"]
            assert picked and set(picked) <= set(IMAGES)
            assert app.sent(i)["mode"] == ["explicit"]

    def test_a_fixed_count_stops_the_size_varying(self):
        app = FakeApp()
        run(app, runs=3, every=0, count=2)
        assert [app.sent(i)["count"] for i in range(3)] == [["2"]] * 3

    def test_a_count_of_zero_is_refused_rather_than_read_as_unset(self):
        # It used to fall through to a random size, so a sweep over 0 1 2 3
        # would have had its first run quietly do something else.
        with pytest.raises(DriveError, match="reads nothing"):
            run(FakeApp(), runs=1, every=0, count=0)

    def test_a_count_larger_than_the_catalogue_is_clamped(self):
        app = FakeApp()
        run(app, runs=1, every=0, count=99, vary="explicit")
        assert len(app.sent(0)["screenshot"]) == len(IMAGES)

    def test_all_reads_everything_every_time(self):
        app = FakeApp()
        run(app, runs=2, every=0, vary="all")
        assert app.sent(0) == {"mode": ["all"], "depth": ["plan"]}

    def test_roster_asks_for_no_screenshots_at_all(self):
        app = FakeApp()
        run(app, runs=1, every=0, vary="roster")
        assert app.sent(0)["no_screenshots"] == ["1"]

    def test_no_images_offered_falls_back_to_the_roster_form(self):
        # The server was launched at a directory with no .png files. That is a
        # form it can still submit, not a reason to refuse to drive.
        app = FakeApp(images=())
        run(app, runs=1, every=0)
        assert app.sent(0)["no_screenshots"] == ["1"]

    def test_profiles_cycle_one_per_run(self):
        app = FakeApp()
        run(app, runs=4, every=0, profiles=("baseline", "planner_trial"))
        got = [app.sent(i)["profile"][0] for i in range(4)]
        assert got == ["baseline", "planner_trial", "baseline", "planner_trial"]

    def test_the_campaign_is_numbered_so_the_run_context_differs_too(self):
        app = FakeApp()
        run(app, runs=3, every=0, campaign="aug-load")
        got = [app.sent(i)["campaign"][0] for i in range(3)]
        assert got == ["aug-load-001", "aug-load-002", "aug-load-003"]

    def test_a_session_is_reconstructible_from_its_seed(self):
        first, second = FakeApp(), FakeApp()
        run(first, runs=5, every=0)
        run(second, runs=5, every=0)
        assert first.posts == second.posts

    def test_replay_and_offline_are_passed_through(self):
        app = FakeApp()
        run(app, runs=1, every=0, replay=True, offline=True)
        assert app.sent(0)["replay"] == ["1"] and app.sent(0)["offline"] == ["1"]

    def test_an_unknown_varying_rule_is_refused(self):
        with pytest.raises(DriveError, match="not a way to vary"):
            plan_request(DriveOptions(vary="sideways"), IMAGES, 0, random.Random(1))


class TestTheOperator:
    """Who each run claims to be, drawn from what the page offers.

    The second axis worth varying, and for the same reason the image subset is
    the first: a username is a context key that is stable across runs, so a
    percentage rollout on the `user` kind splits a population rather than
    re-rolling every run the way one bucketed on the run UUID does.
    """

    def test_the_select_is_the_population(self):
        assert operators_of(FakeApp(operators=PEOPLE)) == PEOPLE

    def test_a_page_offering_nobody_is_not_an_error(self):
        # `operators.yaml` may be missing or empty, and driving a run that
        # carries no user context is a legitimate session.
        assert operators_of(FakeApp()) == ()

    def test_each_run_names_one_of_them(self):
        app = FakeApp(operators=PEOPLE)
        run(app, runs=6, every=0)
        named = [app.sent(i)["operator"][0] for i in range(6)]
        assert set(named) <= set(PEOPLE)
        assert len(set(named)) > 1  # varying, not pinned by accident

    def test_the_department_is_never_posted(self):
        # The pool is the only source of one. A driver that posted a
        # department would be inventing a fact the app would have to trust.
        app = FakeApp(operators=PEOPLE)
        run(app, runs=2, every=0)
        assert "department" not in app.sent(0)

    def test_a_named_subset_is_the_only_one_used(self):
        app = FakeApp(operators=PEOPLE)
        run(app, runs=4, every=0, operators=("priya",))
        assert [app.sent(i)["operator"] for i in range(4)] == [["priya"]] * 4

    def test_a_key_the_app_does_not_offer_is_refused_before_the_session(self):
        # Otherwise it is a 400 on every run, discovered with the whole
        # session's pacing already underway.
        with pytest.raises(DriveError, match="no operator called mallory"):
            run(FakeApp(operators=PEOPLE), runs=1, every=0, operators=("mallory",))

    def test_opting_out_names_nobody(self):
        app = FakeApp(operators=PEOPLE)
        run(app, runs=1, every=0, pick_operator=False)
        assert "operator" not in app.sent(0)

    def test_the_sequence_is_reconstructible_from_the_seed(self):
        first, second = FakeApp(operators=PEOPLE), FakeApp(operators=PEOPLE)
        run(first, runs=5, every=0)
        run(second, runs=5, every=0)
        assert first.posts == second.posts


class TestDepth:
    """How far each run goes, which is the other thing worth varying.

    An extract-only run is one vision call per image and no carrier quote, so
    a session driving a B1 rollout costs a fraction of the same session at
    full depth -- and B1 is the only stage a rollout here can bucket on.
    """

    def test_the_default_is_the_full_plan(self):
        app = FakeApp()
        run(app, runs=1, every=0)
        assert app.sent(0)["depth"] == ["plan"]

    def test_extract_only_is_asked_for_explicitly(self):
        app = FakeApp()
        run(app, runs=2, every=0, depth="extract")
        assert [app.sent(i)["depth"] for i in range(2)] == [["extract"]] * 2

    def test_mixed_alternates_rather_than_flipping_a_coin(self):
        # A coin flip can hand you five plans in a row, which is the session
        # you were trying not to run.
        app = FakeApp()
        run(app, runs=4, every=0, depth="mixed")
        got = [app.sent(i)["depth"][0] for i in range(4)]
        assert got == ["plan", "extract", "plan", "extract"]

    def test_extracting_from_no_screenshots_names_the_combination(self):
        with pytest.raises(DriveError, match="nothing to do"):
            run(FakeApp(), runs=1, every=0, vary="roster", depth="extract")

    def test_an_unknown_depth_is_refused(self):
        with pytest.raises(DriveError, match="not a depth"):
            run(FakeApp(), runs=1, every=0, depth="everything")


class TestPacing:
    """`--every` is a floor on starts, and the server's rule wins over it."""

    def test_runs_are_spaced_by_the_interval(self):
        app = FakeApp(duration=2.0)
        run(app, runs=3, every=30)
        assert app.starts == [0.0, 30.0, 60.0]

    def test_a_run_longer_than_the_interval_does_not_double_up(self):
        # The app refuses a second concurrent run, so the interval cannot be
        # honoured and the driver must not pretend otherwise: it waits.
        app = FakeApp(duration=45.0)
        report = run(app, runs=3, every=30)
        assert app.starts == [0.0, 45.0, 90.0]
        assert report.counted("finished") == 3

    def test_the_driver_waits_for_the_outcome_before_starting_the_next(self):
        app = FakeApp(duration=10.0)
        report = run(app, runs=2, every=0)
        assert [a.outcome for a in report.attempts] == ["manifest", "manifest"]
        assert [a.seconds for a in report.attempts] == [10.0, 10.0]

    def test_a_refusal_is_retried_rather_than_fatal(self):
        # Somebody clicked Start in a browser while the driver was waiting.
        app = FakeApp(duration=5.0)
        app.jobs["deadbeef0000"] = 3.0  # already running when the driver arrives
        report = run(app, runs=1, every=0)
        assert report.conflicts >= 1
        assert report.counted("finished") == 1

    def test_not_waiting_is_the_one_way_to_collide_on_purpose(self):
        app = FakeApp(duration=100.0)
        report = run(app, runs=2, every=1, wait=False)
        assert report.conflicts > 0


class TestWhatItReports:
    def test_a_failed_run_is_counted_as_failed(self):
        app = FakeApp(duration=1.0)
        app.state = "failed"
        report = run(app, runs=2, every=0)
        assert report.counted("failed") == 2
        assert "2 failed" in report.summary

    def test_the_job_id_comes_off_the_redirect(self):
        app = FakeApp()
        report = run(app, runs=1, every=0)
        assert report.attempts[0].job_id in app.jobs
