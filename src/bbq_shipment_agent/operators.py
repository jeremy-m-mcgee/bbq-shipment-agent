"""Who a run claims to be. Design section 6.6.

The `user` context kind carries a username as its key and a department as its
one attribute, and this module is where both come from. `config/operators.yaml`
holds the pairs; everything else — the CLI flag, the form field, the driver —
names a *key* and gets the pair back.

## Why the pool is the only source of a department

A front-end that posted `operator` and `department` as two independent fields
would let the two disagree, and nothing downstream could tell which was true.
A targeting rule reading `department is "kitchen"` would then be describing
whatever the last caller typed rather than a fact about anybody. So the
department is never an input: it is looked up, and an unknown key is an error
rather than a new operator invented at the point of use.

That is the same shape as `_ATTRIBUTES` in `context.py` refusing an undeclared
attribute and `resolve` refusing an unknown capability key. A context is sent
to LaunchDarkly's servers, so what can appear in one is a list somebody has
read and committed.

## Randomising is a driver concern, and it is seeded

`pick` exists for `drive`, which fires many runs at a serving UI precisely to
make them differ from each other. It takes a `Random` rather than reaching for
module-level randomness so a session stays reconstructible from its seed — the
same rule the screenshot sample follows, and for the same reason: a set picked
from nothing is diagnosable from nothing.

The driver does not read this file. It is a client of the UI (see `drive`),
so it learns the population from the page the app renders and posts a key
back, which keeps the app the only thing that decides what a form means.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

import yaml

DEFAULT_OPERATORS_PATH = Path("config/operators.yaml")


class OperatorError(ValueError):
    """The operator configuration cannot be read, or names somebody unknown."""


@dataclass(frozen=True)
class Operator:
    """One person a run can present itself as.

    `key` is what LaunchDarkly targets and buckets on, and it is stable across
    runs — which is the whole reason this kind is worth having. `run.key` is a
    fresh UUID, so a percentage rollout bucketed on the run re-rolls every
    time; a username does not.
    """

    key: str
    department: str


@dataclass(frozen=True)
class OperatorPool:
    """The committed population, and the only way to resolve a key."""

    operators: tuple[Operator, ...] = ()
    #: Which key an unattributed run takes. Empty means none, and a run then
    #: carries no `user` kind at all rather than one saying "unknown".
    default_operator: str = ""

    @classmethod
    def load(cls, path: Path | str = DEFAULT_OPERATORS_PATH) -> OperatorPool:
        """Read the pool, treating a missing file as an empty population.

        Missing is not an error: a checkout with no `operators.yaml` should
        plan a run, not refuse one. What it cannot then do is name an
        operator, and `get` says so with the path in hand.
        """
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise OperatorError(f"{path}: {exc}") from None
        except yaml.YAMLError as exc:
            raise OperatorError(f"{path}: not valid YAML. {exc}") from None
        return cls.from_mapping(raw, source=str(path))

    @classmethod
    def from_mapping(cls, raw: Any, *, source: str = "operators") -> OperatorPool:
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise OperatorError(f"{source}: expected a mapping.")

        operators: list[Operator] = []
        seen: set[str] = set()
        for entry in raw.get("operators") or ():
            if not isinstance(entry, dict):
                raise OperatorError(
                    f"{source}: each operator is a mapping with `key` and "
                    f"`department`, got {entry!r}."
                )
            key = str(entry.get("key") or "").strip()
            department = str(entry.get("department") or "").strip()
            if not key or not department:
                raise OperatorError(
                    f"{source}: operator {entry!r} needs both a `key` and a "
                    "`department`. A user with no department is a context "
                    "attribute nothing can target."
                )
            if key in seen:
                # Two entries for one key means the department a rule sees
                # depends on which one the lookup happened to find first.
                raise OperatorError(f"{source}: operator {key!r} is listed twice.")
            seen.add(key)
            operators.append(Operator(key=key, department=department))

        default = str(raw.get("default_operator") or "").strip()
        if default and default not in seen:
            raise OperatorError(
                f"{source}: default_operator {default!r} is not one of "
                f"{sorted(seen)}."
            )
        return cls(operators=tuple(operators), default_operator=default)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(operator.key for operator in self.operators)

    def get(self, key: str | None) -> Operator | None:
        """Resolve a key, or the configured default when none is named.

        Returns None when nobody is named and no default is set, which is how
        a run ends up with no `user` context kind.
        """
        wanted = (key or self.default_operator).strip()
        if not wanted:
            return None
        for operator in self.operators:
            if operator.key == wanted:
                return operator
        raise OperatorError(
            f"{wanted!r} is not an operator. Known: {sorted(self.keys)}. "
            "Add them to config/operators.yaml — a department is looked up, "
            "never supplied, so an unlisted key has nothing to be."
        )

    def pick(self, rng: Random) -> Operator | None:
        """One of the population, chosen by a seeded generator."""
        return rng.choice(self.operators) if self.operators else None
