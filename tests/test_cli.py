"""The argument parser.

Thin on purpose, and it exists because of a real miss: a `build_parser` that
raised `ArgumentError` on every invocation passed the whole suite, because
nothing had ever called it. A CLI that cannot start is the most complete
failure the program has, and it was the only one not covered.
"""

import argparse

import pytest

from bbq_shipment_agent.cli import _roster, _screenshots, build_parser
from bbq_shipment_agent.recipients import ExtractionError


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

    @pytest.mark.parametrize("command", ["init", "plan", "review"])
    def test_every_run_command_takes_the_a1_arguments(self, parser, command):
        args = parser.parse_args(["run", command, "--profile", "full"])
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


class TestScreenshotSampling:
    """`--screenshot-count` picks a subset of B1's input.

    The property that matters is not that the choice is random but that it is
    *named*: extraction accuracy is per-image, so a run that read three of
    seven is only comparable to another run if you can tell which three.
    """

    @pytest.fixture
    def images(self, tmp_path):
        for i in range(7):
            (tmp_path / f"{i:02d}-shot.png").write_bytes(b"")
        (tmp_path / "README.md").write_text("not a screenshot", encoding="utf-8")
        return tmp_path

    def args(self, images, count=None, seed=None):
        return argparse.Namespace(
            screenshots=images, screenshot_count=count, screenshot_seed=seed
        )

    def test_no_count_reads_the_whole_directory(self, images):
        assert len(_screenshots(self.args(images))) == 7

    def test_only_png_files_are_candidates(self, images):
        assert all(p.suffix == ".png" for p in _screenshots(self.args(images)))

    def test_a_count_reads_exactly_that_many(self, images):
        assert len(_screenshots(self.args(images, count=3, seed=1))) == 3

    def test_the_sample_comes_from_the_directory(self, images):
        sample = _screenshots(self.args(images, count=3, seed=1))
        assert set(sample) <= set(images.glob("*.png"))
        assert len(set(sample)) == 3  # no image read twice

    def test_the_same_seed_reads_the_same_sample(self, images):
        first = _screenshots(self.args(images, count=3, seed=42))
        second = _screenshots(self.args(images, count=3, seed=42))
        assert first == second

    def test_the_sample_is_ordered_regardless_of_the_draw(self, images):
        # B1 reads in a stable order, so two runs that drew the same images
        # cannot differ in the sequence they were handed to the model.
        sample = _screenshots(self.args(images, count=4, seed=7))
        assert list(sample) == sorted(sample)

    def test_a_seed_is_generated_and_reported_when_none_is_given(self, images, capsys):
        _screenshots(self.args(images, count=2))
        assert "seed" in capsys.readouterr().out

    def test_the_chosen_files_are_named_in_the_output(self, images, capsys):
        sample = _screenshots(self.args(images, count=3, seed=5))
        printed = capsys.readouterr().out
        assert all(p.name in printed for p in sample)

    def test_asking_for_more_than_exist_is_refused(self, images):
        # Not clamped: a run silently reading 7 when told 10 looks like a run
        # that got what it asked for.
        with pytest.raises(ExtractionError, match="exceeds"):
            _screenshots(self.args(images, count=10))

    def test_asking_for_none_is_refused(self, images):
        with pytest.raises(ExtractionError, match="at least 1"):
            _screenshots(self.args(images, count=0))

    def test_an_empty_directory_is_still_refused(self, tmp_path):
        with pytest.raises(ExtractionError, match="no .png"):
            _screenshots(self.args(tmp_path, count=1))

    def test_sampling_without_a_directory_to_sample_is_refused(self, tmp_path):
        # Otherwise the option is silently ignored, which reads as though a
        # sample were taken.
        args = argparse.Namespace(
            screenshots=None,
            screenshot_count=3,
            screenshot_seed=None,
            recipients=tmp_path / "r.yaml",
        )
        with pytest.raises(ExtractionError, match="was not given"):
            _roster(args)


class TestRefusals:
    def test_a_bare_invocation_is_refused(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_a_run_with_no_subcommand_is_refused(self, parser):
        with pytest.raises(SystemExit):
            parser.parse_args(["run"])
