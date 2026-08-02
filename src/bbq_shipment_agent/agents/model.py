"""The seam a model call sits behind, and the Anthropic implementation.

Symmetric with `RateQuoter` and `AddressValidator`: a protocol, a live
implementation, and a recorded one. Offline by default, live injected, so no
test can open a socket and no `--help` invocation pays for an SDK import.

## What belongs to LaunchDarkly and what belongs here

Design 6.1 draws the line: LD holds the instruction text, the model name and
the model parameters; Python holds tool definitions, execution and every piece
of control flow. This module is the Python side of that boundary and it holds
no prompt text and no model name of its own. `Invocation` is built from an
`AgentConfig` -- whatever LaunchDarkly served -- and this module only knows how
to put it on the wire.

The one exception is stated rather than hidden: the Anthropic API requires
`max_tokens`, and LD's `model_parameters` may not carry one. A Python default
applies in that case, and LD overrides it by setting the parameter.

## Identity comes from A1, not from a second lookup

`Invocation` is constructed from the config captured at run start. Re-reading
LaunchDarkly here would attribute a behavior to whatever the console happens
to be serving at write time, which is the exact confusion the instruction hash
exists to prevent (design 6.4, mitigation 2).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..agent_configs import AgentConfig

#: The Anthropic API requires a token ceiling and LD's `model_parameters` may
#: not carry one. Generous for a critique pass over ~22 rows; LD overrides it
#: by setting `max_tokens` on the AI Config, which is where a model parameter
#: belongs.
DEFAULT_MAX_TOKENS = 4_096

#: Model parameters this code knows how to pass through. An unknown parameter
#: is dropped rather than forwarded: LD is a delivery layer for values, and a
#: typo in the console should not become a TypeError inside a shipping run.
PASSTHROUGH_PARAMETERS: tuple[str, ...] = ("max_tokens", "temperature", "top_p")

PROVIDER = "anthropic"


class ModelUnavailable(Exception):
    """The model could not be called. Never a silently empty answer."""


@dataclass(frozen=True)
class Completion:
    """One model response, plus what it cost."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    #: The model that actually answered, as the API reported it. Kept beside
    #: the model LaunchDarkly asked for so the two can be compared: an alias
    #: that resolves elsewhere, or a name silently remapped by the provider,
    #: is otherwise invisible, and the ledger would record an attribution that
    #: was never true. Legitimate when they differ; worth saying so.
    model: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def truncated(self) -> bool:
        """Whether the answer stopped at the token ceiling rather than ending.

        Worth knowing separately from a parse failure: truncated JSON and
        malformed JSON look identical to a parser but have different fixes.
        """
        return self.stop_reason == "max_tokens"


@dataclass(frozen=True)
class Invocation:
    """Everything one model call needs, taken from LD's agent config."""

    agent_key: str
    model: str
    instructions: str
    parameters: dict[str, Any] = field(default_factory=dict)
    variation_key: str | None = None
    version: int | None = None

    @classmethod
    def from_config(cls, config: AgentConfig, instructions: str) -> Invocation:
        """Build from the config captured at A1.

        `instructions` is passed in rather than read off the config because the
        caller renders the template first. The un-rendered text is what gets
        hashed and snapshotted; the rendered text is what the model sees.
        """
        if not config.available:
            raise ModelUnavailable(
                f"{config.agent_key}: config is not available "
                f"({config.source}, {config.reason}). Nothing to invoke."
            )
        if not config.model:
            raise ModelUnavailable(
                f"{config.agent_key}: the AI Config names no model. Set one in "
                "LaunchDarkly -- design 6.1 puts the model name there, and "
                "there is deliberately no Python default to fall back on."
            )
        return cls(
            agent_key=config.agent_key,
            model=config.model,
            instructions=instructions,
            parameters={
                key: value
                for key, value in (config.model_parameters or {}).items()
                if key in PASSTHROUGH_PARAMETERS
            },
            variation_key=config.variation_key,
            version=config.version,
        )

    def request_parameters(self) -> dict[str, Any]:
        parameters = {"max_tokens": DEFAULT_MAX_TOKENS}
        parameters.update(self.parameters)
        return parameters


class ModelClient(Protocol):
    """Sends one prompt and returns one completion."""

    def complete(self, invocation: Invocation, prompt: str) -> Completion: ...


class AnthropicModel:
    """Live calls to the Anthropic API.

    Holds no model name: every call takes it from the `Invocation`, which took
    it from LaunchDarkly. Changing which model an agent runs on is a console
    edit, not a deploy, which is the whole point of the medium split.
    """

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            # Falsy rather than `is None`, for the same reason as the LD and
            # Shippo keys: `.env` is seeded from `.env.example`, so an
            # unconfigured key is present-but-empty rather than absent.
            raise ModelUnavailable(
                "ANTHROPIC_API_KEY is unset or empty. Turn `verification-enabled` "
                "off to run without the agent -- a run with no verification is a "
                "normal run, and design 6.10 makes degrading the expected path."
            )
        self._key = key
        self._client: Any = None

    def _sdk(self) -> Any:
        # Imported lazily so a run with verification off never pays for it.
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=self._key)
        return self._client

    def complete(self, invocation: Invocation, prompt: str) -> Completion:
        from anthropic import APIError

        try:
            message = self._sdk().messages.create(
                model=invocation.model,
                system=invocation.instructions,
                messages=[{"role": "user", "content": prompt}],
                **invocation.request_parameters(),
            )
        except APIError as exc:
            raise ModelUnavailable(f"{invocation.agent_key}: {exc}") from exc

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        return Completion(
            text=text,
            input_tokens=getattr(message.usage, "input_tokens", 0),
            output_tokens=getattr(message.usage, "output_tokens", 0),
            stop_reason=message.stop_reason,
            model=getattr(message, "model", None),
        )


class RecordedModel:
    """Replays completions captured from a live call. For tests.

    Keyed on the agent key and the instruction hash, so a recording cannot
    outlive the instructions it was captured against: edit the text in LD and
    the fixture stops matching rather than quietly answering for the new
    prompt. An unrecorded key raises for the same reason `RecordedQuoter` does.
    """

    def __init__(self, recording: dict[str, Any]) -> None:
        self._recording = recording
        self.calls: list[tuple[Invocation, str]] = []

    @classmethod
    def from_file(cls, path: Path | str) -> RecordedModel:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def key(invocation: Invocation) -> str:
        from ..hashing import short_hash

        return f"{invocation.agent_key}#{short_hash(invocation.instructions)}"

    def complete(self, invocation: Invocation, prompt: str) -> Completion:
        self.calls.append((invocation, prompt))
        key = self.key(invocation)
        row = self._recording.get(key)
        if row is None:
            raise ModelUnavailable(
                f"no recorded completion for {key}. Re-record against the live "
                "API rather than inventing what the model would have said."
            )
        return Completion(
            text=row["text"],
            input_tokens=row.get("input_tokens", 0),
            output_tokens=row.get("output_tokens", 0),
            stop_reason=row.get("stop_reason"),
            model=row.get("model"),
        )
