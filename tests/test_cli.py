"""The argument parser, and the adapter from it to `RunOptions`.

Thin on purpose, and it exists because of a real miss: a `build_parser` that
raised `ArgumentError` on every invocation passed the whole suite, because
nothing had ever called it. A CLI that cannot start is the most complete
failure the program has, and it was the only one not covered.

The screenshot-selection tests moved to `test_wiring.py` with the code they
cover. What is left here is parsing, plus `_options`, which is the seam the UI
made necessary: the CLI and the browser have to arrive at the same options
object, and only one of them has a Namespace.
"""

from pathlib import Path

import pytest

from bbq_shipment_agent.cli import _options, build_parser


@pytest.fixture(scope="module")
def parser():
    return build_parser()


class TestItBuilds:
    def test_the_parser_constructs(self, parser):
        # Duplicate option strings raise here, not at parse time.
        assert parser is not None

    @pytest.mark.parametrize(
        "argv",
        [
            ["ledger", "verify"],
            ["ledger", "rebuild"],
            ["ledger", "tools"],
            ["run", "init"],
            ["run", "plan"],
            ["run", "review"],
            ["ui"],
            ["drive"],
        ],
    )
    def test_every_subcommand_parses_and_has_a_handler(self, parser, argv):
        args = parser.parse_args(argv)
        assert callable(args.handler)


class TestPlanningArguments:
    @pytest.mark.parametrize("command", ["plan", "review"])
    def test_both_planning_commands_take_the_same_inputs(self, parser, command):
        # `review` plans before it reviews, so an option one accepts and the
        # other does not is a trap rather than a simplification.
        args = parser.parse_args(
            [
                "run", command,
                "--recipients", "r.yaml",
                "--quotes", "q.json",
                "--validations", "v.json",
                "--completions", "c.json",
                "--extractions", "e.json",
                "--repairs", "p.json",
                "--cache", ".c",
                "--max-attempts", "3",
                "--backoff", "1.5",
                "--screenshots", "shots",
                "--screenshot-count", "3",
                "--screenshot-seed", "42",
                "--offline",
            ]
        )
        assert str(args.recipients) == "r.yaml"
        assert args.max_attempts == 3
        assert args.offline is True
        assert args.screenshot_count == 3
        assert args.screenshot_seed == 42

    @pytest.mark.parametrize("command", ["plan", "review"])
    def test_sampling_is_off_unless_asked_for(self, parser, command):
        args = parser.parse_args(["run", command])
        assert args.screenshot_count is None
        assert args.screenshot_seed is None

    @pytest.mark.parametrize("command", ["init", "plan", "review", "ui"])
    def test_every_run_command_takes_the_a1_arguments(self, parser, command):
        argv = [command] if command == "ui" else ["run", command]
        args = parser.parse_args(argv + ["--profile", "full"])
        assert args.profile == "full"
        assert args.snapshot and args.ledger

    def test_the_retry_defaults_are_higher_than_the_library(self, parser):
        # A 22-recipient run is ~300 quote calls and UPS rate-limits well
        # before that on Shippo's shared master account.
        from bbq_shipment_agent.planning.rates import (
            DEFAULT_BACKOFF_S,
            DEFAULT_MAX_ATTEMPTS,
        )

        args = parser.parse_args(["run", "plan"])
        assert args.max_attempts > DEFAULT_MAX_ATTEMPTS
        assert args.backoff > DEFAULT_BACKOFF_S


class TestTheUiCommand:
    def test_it_serves_the_fixture_screenshots_by_default(self, parser):
        # A picker with nothing to pick is a poor first screen, and this is
        # the only directory the repo is guaranteed to have.
        args = parser.parse_args(["ui"])
        assert args.screenshots.name == "screenshots"

    def test_it_takes_the_planning_arguments_too(self, parser):
        # The form supplies the selection and the profile. Everything else
        # still arrives as a launch default, so the UI must accept them.
        args = parser.parse_args(
            ["ui", "--quotes", "q.json", "--extractions", "e.json", "--offline"]
        )
        assert str(args.quotes) == "q.json"
        assert args.offline is True

    def test_it_has_a_port_and_no_host(self, parser):
        # Loopback is not configurable on purpose: the page serves home
        # addresses and has no authentication.
        args = parser.parse_args(["ui", "--port", "9000"])
        assert args.port == 9000
        assert not hasattr(args, "host")


class TestOptionsAdapter:
    """`_options` is where a parsed command line becomes what `wiring` takes."""

    def test_a_count_becomes_a_selection(self, parser):
        options = _options(
            parser.parse_args(
                ["run", "plan", "--screenshots", "s", "--screenshot-count", "2"]
            )
        )
        assert options.selection.count == 2
        assert options.selection.explicit is None

    def test_no_sampling_arguments_means_no_selection(self, parser):
        # Not an empty selection: absent means "read the directory", and an
        # empty one would have to mean something else.
        options = _options(parser.parse_args(["run", "plan", "--screenshots", "s"]))
        assert options.selection is None

    def test_the_replay_paths_carry_through(self, parser):
        options = _options(
            parser.parse_args(
                ["run", "plan", "--quotes", "q.json", "--repairs", "p.json"]
            )
        )
        assert str(options.quotes) == "q.json"
        assert str(options.repairs) == "p.json"

    def test_init_has_no_planning_arguments_and_still_adapts(self, parser):
        # `run init` never sees --cache or --screenshots. The adapter has to
        # cope, or adding an option to one command breaks another.
        options = _options(parser.parse_args(["run", "init", "--packet-count", "22"]))
        assert options.packet_count == 22
        assert options.screenshots is None


class TestTheToolReadout:
    """`ledger tools` answers "which tools did this run use" from the JSONL.

    It reads a cache built in memory rather than `ledger.duckdb`, so it cannot
    report something a stale committed cache is holding and the source of
    truth is not.
    """

    @pytest.fixture
    def ledger(self, tmp_path):
        from bbq_shipment_agent.ledger import AgentInvocationRecord, LedgerWriter

        writer = LedgerWriter(tmp_path / "ledger")
        writer.append(
            AgentInvocationRecord(
                run_id="r1", agent_key="screenshot-extraction", outcome="extracted:7"
            )
        )
        writer.append(
            AgentInvocationRecord(
                run_id="r1",
                agent_key="address-repair",
                outcome="repaired:2/3",
                tools_offered=["read_image_region", "validate_address"],
                tools_called=["read_image_region", "validate_address", "validate_address"],
            )
        )
        writer.append(
            AgentInvocationRecord(
                run_id="r2",
                agent_key="review-narrator",
                outcome="clean",
                tools_offered=["read_manifest"],
                tools_called=[],
            )
        )
        return tmp_path / "ledger"

    def _run(self, ledger, capsys, run=""):
        import argparse

        from bbq_shipment_agent.cli import _cmd_tools

        code = _cmd_tools(argparse.Namespace(ledger=ledger, run=run))
        return code, capsys.readouterr().out

    def test_it_counts_repeat_calls_rather_than_naming_a_tool_once(
        self, ledger, capsys
    ):
        _, out = self._run(ledger, capsys)
        assert "validate_address x2" in out
        assert "read_image_region x1" in out

    def test_offered_but_never_called_is_visible(self, ledger, capsys):
        # The finding the pair exists for: three tools handed over and none
        # used is a fact about the instructions, not a quiet nothing.
        _, out = self._run(ledger, capsys, run="r2")
        assert "offered read_manifest" in out
        assert "called nothing" in out

    def test_a_stage_with_no_tool_loop_says_so(self, ledger, capsys):
        _, out = self._run(ledger, capsys, run="r1")
        assert "no tool loop" in out

    def test_an_unknown_run_is_an_error_not_an_empty_report(self, ledger, capsys):
        # Silence would read as "that run called no tools", which is a
        # different answer from "there is no such run".
        code, out = self._run(ledger, capsys, run="nope")
        assert code == 1
        assert "no agent invocations for run nope" in out


class TestRefusals:
    def test_a_bare_invocation_is_refused(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_a_run_with_no_subcommand_is_refused(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["run"])


class TestExtractRecordsWhatItRead:
    """`run extract` stops before planning, so it is the one front-end path
    that has to write the screenshot map itself. It did not, and the hashes on
    its invocation records named files nothing in the ledger could resolve."""

    def _run(self, tmp_path, argv_extra=()):
        import json

        from bbq_shipment_agent.cli import main

        ledger = tmp_path / "ledger"
        snapshot = Path(__file__).parent.parent / "config" / "ld-snapshot.json"
        code = main([
            "run", "extract", "--offline",
            "--screenshots", str(Path(__file__).parent / "fixtures" / "screenshots"),
            "--screenshot-count", "2", "--screenshot-seed", "5",
            "--extractions", str(Path(__file__).parent / "fixtures" / "b1-extractions.json"),
            "--recipients", str(Path(__file__).parent / "fixtures" / "roster-sf-dc.yaml"),
            "--ledger", str(ledger),
            "--snapshot", str(snapshot), *argv_extra,
        ])
        reasons = {}
        for line in (ledger / "runs.jsonl").read_text().splitlines():
            if line.strip():
                reasons.update(json.loads(line).get("evaluation_reasons") or {})
        return code, reasons, ledger

    def test_it_reports_what_it_could_not_read(self, tmp_path, capsys):
        """The terminal summary had the same hole the result page did.

        `run extract` printed the images it was given and the recipients it
        found, and a screenshot that produced nothing appeared in neither. The
        progress stream said so while the run went, and then scrolled.
        """
        import json
        import shutil

        from bbq_shipment_agent.cli import main

        fixtures = Path(__file__).parent / "fixtures"
        shots = tmp_path / "shots"
        shots.mkdir()
        for name in ("01-imessage-thread.png", "07-whatsapp-group.png"):
            shutil.copyfile(fixtures / "screenshots" / name, shots / name)

        # Unparseable by construction, and it stays that way however tolerant
        # `_parse` becomes. What B1 really returned under the vague variation
        # is in `test_extraction.py`; here the reply only has to fail.
        recording = json.loads(
            (fixtures / "b1-extractions.json").read_text(encoding="utf-8")
        )
        recording["replies"]["01-imessage-thread.png"]["text"] = (
            '{"name": "Ana Ruiz", "street1": "1600 Pennsylvania Ave NW"}\n'
            '{"name": "Marcus Feld", "street1": "233 S Wacker Dr"}'
        )
        broken = tmp_path / "b1-sibling-objects.json"
        broken.write_text(json.dumps(recording), encoding="utf-8")

        code = main([
            "run", "extract", "--offline",
            "--screenshots", str(shots),
            "--extractions", str(broken),
            "--recipients", str(fixtures / "roster-sf-dc.yaml"),
            "--ledger", str(tmp_path / "ledger"),
            "--snapshot", str(Path(__file__).parent.parent / "config" / "ld-snapshot.json"),
        ])
        assert code == 0

        out = capsys.readouterr().out
        assert "B1 read 1 of 2 screenshot(s)" in out
        assert "could not be read" in out
        assert "Extra data" in out
        # Named in a reply nothing could parse, so not recipients.
        assert "Ana Ruiz" not in out
        # Design 4: chased by a human, not dropped.
        assert "jules_g" in out

    def test_the_hash_to_filename_map_reaches_the_run_row(self, tmp_path):
        # Named the sample rather than described it, once: seed 5 of seven
        # images. Adding an eighth screenshot re-drew the sample and failed a
        # test about something else entirely, which is the wrong thing to
        # notice when the fixture set is meant to grow.
        _, reasons, _ = self._run(tmp_path)
        mapping = reasons["screenshot_keys"]
        available = {
            p.name for p in (Path(__file__).parent / "fixtures" / "screenshots").glob("*.png")
        }
        assert len(mapping) == 2
        assert set(mapping) <= available
        assert all(len(key) == 16 for key in mapping.values())

    def test_the_same_seed_re_reads_the_same_sample(self, tmp_path):
        # What the seed is for, and what naming the pair was reaching for.
        # Design 10: a seeded sample is reconstructible from its seed, which is
        # a claim about repeatability rather than about which files won.
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        _, reasons_a, _ = self._run(a)
        _, reasons_b, _ = self._run(b)
        assert reasons_a["screenshot_keys"] == reasons_b["screenshot_keys"]

    def test_every_invocation_image_key_resolves_to_a_filename(self, tmp_path):
        # The property the map exists for: LaunchDarkly is given the hash and
        # never the filename, so this join is the only account of which file a
        # served variation actually read.
        import json

        _, reasons, ledger = self._run(tmp_path)
        known = set(reasons["screenshot_keys"].values())
        rows = [
            json.loads(line)
            for line in (ledger / "agent_invocations.jsonl").read_text().splitlines()
            if line.strip()
        ]
        keys = [r["image_key"] for r in rows if r.get("image_key")]
        assert keys and set(keys) <= known

    def test_a1_capability_reasons_survive_the_append(self, tmp_path):
        # `evaluation_reasons` is one JSON field, so an append rewrites it
        # whole. A writer starting from a bare dict would drop A1's record.
        _, reasons, _ = self._run(tmp_path)
        assert "capabilities" in reasons
        assert reasons["screenshot_seed"] == 5
