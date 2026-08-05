"""The user/department pool. Design section 6.6.

One claim is worth more than the rest here and every test below is a way of
saying it: **the pool is the only source of a department**. A key is an input,
a department never is, so a form and a targeting rule cannot end up disagreeing
about who is in `kitchen`.
"""

import random

import pytest
import yaml

from bbq_shipment_agent.operators import Operator, OperatorError, OperatorPool

POOL = """
default_operator: dana
operators:
  - key: dana
    department: ops
  - key: priya
    department: kitchen
  - key: marcus
    department: kitchen
"""


def pool(text: str = POOL) -> OperatorPool:
    return OperatorPool.from_mapping(yaml.safe_load(text), source="pool")


class TestReadingIt:
    def test_the_pairs_come_back_whole(self):
        assert pool().operators == (
            Operator("dana", "ops"),
            Operator("priya", "kitchen"),
            Operator("marcus", "kitchen"),
        )

    def test_a_missing_file_is_an_empty_population_not_an_error(self, tmp_path):
        # A checkout with no operators.yaml should plan a run, not refuse one.
        assert OperatorPool.load(tmp_path / "nothing.yaml").operators == ()

    def test_an_operator_with_no_department_is_refused(self):
        with pytest.raises(OperatorError, match="needs both"):
            pool("operators:\n  - key: dana\n")

    def test_a_duplicate_key_is_refused(self):
        # Which department a rule sees would otherwise depend on which entry
        # the lookup happened to reach first.
        with pytest.raises(OperatorError, match="listed twice"):
            pool(
                "operators:\n"
                "  - key: dana\n    department: ops\n"
                "  - key: dana\n    department: finance\n"
            )

    def test_a_default_naming_nobody_is_refused(self):
        with pytest.raises(OperatorError, match="default_operator"):
            pool("default_operator: nobody\noperators:\n  - key: dana\n    department: ops\n")


class TestResolvingAKey:
    def test_a_key_resolves_to_its_department(self):
        assert pool().get("priya") == Operator("priya", "kitchen")

    def test_an_unknown_key_is_an_error_rather_than_a_new_operator(self):
        # The alternative is inventing somebody at the point of use, which is
        # exactly how a department becomes a claim nobody checked.
        with pytest.raises(OperatorError, match="not an operator"):
            pool().get("mallory")

    def test_nobody_named_takes_the_default(self):
        assert pool().get(None) == Operator("dana", "ops")

    def test_nobody_named_and_no_default_is_nobody(self):
        # Which is how a run ends up with no `user` context kind at all --
        # absent and "present but unset" should not be two states.
        assert pool("operators:\n  - key: dana\n    department: ops\n").get(None) is None


class TestPicking:
    """Randomised for `drive`, and seeded so a session stays reproducible."""

    def test_it_picks_from_the_population(self):
        assert pool().pick(random.Random(3)) in pool().operators

    def test_the_same_seed_picks_the_same_person(self):
        assert pool().pick(random.Random(11)) == pool().pick(random.Random(11))

    def test_an_empty_population_picks_nobody(self):
        assert OperatorPool().pick(random.Random(1)) is None


def test_the_committed_pool_loads():
    # The file the CLI, the form and the driver all default to. A typo in it
    # is a broken `run plan`, not a broken test somewhere else.
    committed = OperatorPool.load()
    assert committed.operators
    assert all(person.key and person.department for person in committed.operators)
