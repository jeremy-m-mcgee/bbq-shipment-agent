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
            ["run", "init"],
            ["run", "plan"],
            ["run", "review"],
            ["ui"],
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
        assert args.config and args.snapshot and args.ledger

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
        import textwrap

        from bbq_shipment_agent.cli import main

        config = tmp_path / "capabilities.yaml"
        config.write_text(
            textwrap.dedent(
                """
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
            ),
            encoding="utf-8",
        )
        ledger = tmp_path / "ledger"
        snapshot = Path(__file__).parent.parent / "config" / "ld-snapshot.json"
        code = main([
            "run", "extract", "--offline",
            "--screenshots", str(Path(__file__).parent / "fixtures" / "screenshots"),
            "--screenshot-count", "2", "--screenshot-seed", "5",
            "--extractions", str(Path(__file__).parent / "fixtures" / "b1-extractions.json"),
            "--recipients", str(Path(__file__).parent / "fixtures" / "roster-sf-dc.yaml"),
            "--ledger", str(ledger), "--config", str(config),
            "--snapshot", str(snapshot), *argv_extra,
        ])
        reasons = {}
        for line in (ledger / "runs.jsonl").read_text().splitlines():
            if line.strip():
                reasons.update(json.loads(line).get("evaluation_reasons") or {})
        return code, reasons, ledger

    def test_the_hash_to_filename_map_reaches_the_run_row(self, tmp_path):
        _, reasons, _ = self._run(tmp_path)
        mapping = reasons["screenshot_keys"]
        assert set(mapping) == {"03-instagram-dm.png", "05-email.png"}
        assert all(len(key) == 16 for key in mapping.values())

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
