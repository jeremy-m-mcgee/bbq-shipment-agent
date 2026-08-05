"""Firing runs at a running UI on an interval, for live testing.

`run plan` plans once and `ui` plans when you click. Neither produces what a
live test wants, which is *many* runs, differing from each other, spread over
enough time to watch a targeting rule or a percentage rollout actually split.
Design 6.6 is the reason the differences matter: B1's config is retrieved once
per screenshot under an `image` context keyed on the file's content hash, so
which images a run reads is the one axis a rollout in this system can bucket
on. A driver that fired the same request twenty times would exercise the
server and none of that.

## It is a client, not a third front-end

Everything here goes through `POST /runs` on a UI that is already serving.
There is no `RunOptions` in this module, no `open_run`, no stage: the app
decides what a form means, exactly as it does for a browser, so a run started
by the driver and a run started by a click are the same run. That is what
keeps the two-front-end claim in CLAUDE.md true — a third way to *ask* for a
run is not a third way to *perform* one.

## One at a time is the server's rule and the driver obeys it

`RunService.start` refuses a second concurrent run rather than queueing it, so
`--every` is a floor on the gap between starts and never a promise of one. The
driver waits for the run in flight to finish, then fires when the interval has
elapsed — which for a live screenshot run is usually later than the interval,
and the report says so rather than hiding it. A 409 is still handled, because
the operator may be clicking in a browser at the same time.

## Sockets

`HttpTransport` opens one, and it is the only thing here that does. Everything
else takes a transport by injection, the same shape as the rest of the package
— so the tests drive a full session against a fake and no test opens a socket.
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_INTERVAL = 30.0
#: How often the driver asks a running job whether it is done. A run takes
#: tens of seconds at best, so a second of latency costs nothing and a tighter
#: poll is just noise in the server log.
DEFAULT_POLL = 1.0

#: The checkbox the picker renders per image. Parsing the page is how the
#: driver learns the catalogue without being told the directory twice, and the
#: name/value pair is the same contract the browser posts back.
CHECKBOX = re.compile(r'name="screenshot"\s+value="([^"]+)"')

#: The option the operator select renders per person, learned the same way and
#: for the same reason. The population a `user` rollout splits is a committed
#: file the driver deliberately does not read: the app renders it, the driver
#: posts a key back, and the app decides what that means.
OPERATOR = re.compile(r'data-operator="([^"]+)"')


class DriveError(RuntimeError):
    """The driver cannot do its job. An operator error with a fixable cause."""


@dataclass(frozen=True)
class Response:
    """What came back, without pretending a 409 is an exception."""

    status: int
    body: str
    url: str = ""

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            return None


class Transport(Protocol):
    """The two verbs a driving session needs."""

    def get(self, path: str) -> Response: ...

    def post(self, path: str, fields: list[tuple[str, str]]) -> Response: ...


class HttpTransport:
    """urllib against a loopback server.

    Stdlib rather than a client library because this adds no dependency to a
    package whose only HTTP client would otherwise be one it never ships. The
    app answers a `POST /runs` with a 303 to the run page; urllib follows it,
    so the job id is read off the final URL rather than a header.
    """

    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str) -> Response:
        return self._open(urllib.request.Request(self._url(path), method="GET"))

    def post(self, path: str, fields: list[tuple[str, str]]) -> Response:
        body = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(
            self._url(path),
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._open(request)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _open(self, request: urllib.request.Request) -> Response:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                return Response(reply.status, reply.read().decode(), reply.url)
        except urllib.error.HTTPError as exc:
            # A refusal is an answer. 409 in particular is the server saying
            # "a run is already going", which is a normal thing to be told.
            return Response(exc.code, exc.read().decode(errors="replace"), exc.url)
        except urllib.error.URLError as exc:
            raise DriveError(
                f"cannot reach {self.base_url}: {exc.reason}. Start the app "
                f"with `uv run bbq-shipment-agent ui` first."
            ) from exc


@dataclass(frozen=True)
class DriveOptions:
    """What a driving session varies, and how fast.

    Frozen and seeded: a session is reconstructible from `seed` plus these
    values, which is the same reason `--screenshot-seed` exists on the CLI. A
    live test that produced a surprising run should be re-runnable.
    """

    base_url: str = DEFAULT_URL
    #: Floor on the gap between *starts*, not a guarantee. See the module note.
    every: float = DEFAULT_INTERVAL
    #: 0 means until interrupted.
    runs: int = 0
    #: sample | explicit | all | roster
    vary: str = "sample"
    #: plan | extract | mixed. `extract` stops each run after B1, which is one
    #: vision call per image and no carrier quote at all -- the depth to drive
    #: a rollout at, since B1 is what a rollout here can bucket on. `mixed`
    #: alternates, for a session that exercises both.
    depth: str = "plan"
    #: Fixed sample size. None picks a new one per run, which is the point.
    count: int | None = None
    #: Cycled, one per run. Empty leaves the launch default alone.
    profiles: tuple[str, ...] = ()
    #: Prefix for a per-run campaign attribute, so the run context differs too.
    campaign: str | None = None
    #: Which operator keys to draw from, empty meaning everyone the page
    #: offers. One key pins a session to one identity.
    operators: tuple[str, ...] = ()
    #: Whether to name an operator at all. On by default: the `user` kind is
    #: the only kind besides `image` whose key is stable across runs, so a
    #: session that never varies it leaves the second bucketable axis at rest.
    pick_operator: bool = True
    seed: int | None = None
    replay: bool = False
    offline: bool = False
    poll: float = DEFAULT_POLL
    #: Fire and forget. Off by default: without waiting, every run after the
    #: first would be refused.
    wait: bool = True


@dataclass(frozen=True)
class Request:
    """One POST body, and how to say out loud what it asked for."""

    fields: list[tuple[str, str]]
    summary: str


def plan_request(
    options: DriveOptions,
    catalogue: tuple[str, ...],
    index: int,
    rng: random.Random,
    operators: tuple[str, ...] = (),
) -> Request:
    """The form a browser would have posted, chosen by the varying rule.

    Pure, and separate from the loop, because "what makes these runs different
    from each other" is the whole substance of a live test and is worth being
    able to assert on without a server.
    """
    fields: list[tuple[str, str]] = []
    mode = "all"
    summary = f"all {len(catalogue)}"

    if options.vary == "roster" or not catalogue:
        fields.append(("no_screenshots", "1"))
        summary = "roster only" if options.vary == "roster" else "roster only (no images)"
    elif options.vary == "sample":
        wanted = _size_for(options, rng, len(catalogue))
        # A seed the driver chose and prints, rather than one the server
        # generates, so the sample is reconstructible from this log alone.
        seed = rng.randrange(1_000_000)
        mode = "sample"
        fields += [("count", str(wanted)), ("seed", str(seed))]
        summary = f"sample {wanted} of {len(catalogue)} (seed {seed})"
    elif options.vary == "explicit":
        wanted = _size_for(options, rng, len(catalogue))
        picked = sorted(rng.sample(list(catalogue), wanted))
        mode = "explicit"
        fields += [("screenshot", name) for name in picked]
        summary = f"picked {len(picked)}: {', '.join(picked)}"
    elif options.vary != "all":
        raise DriveError(
            f"{options.vary!r} is not a way to vary a run. "
            "Use sample, explicit, all or roster."
        )

    fields.insert(0, ("mode", mode))

    depth = _depth_for(options, index)
    if depth == "extract" and ("no_screenshots", "1") in fields:
        # There is nothing for B1 to read on the roster path, and the app says
        # so with a 400. Saying it here names the combination instead of the
        # request that carried it.
        raise DriveError(
            "--depth extract has nothing to do on a run with no screenshots. "
            "Drop --vary roster, or drive at --depth plan."
        )
    fields.append(("depth", depth))
    summary = f"{summary}  ->{depth}"

    if options.profiles:
        profile = options.profiles[index % len(options.profiles)]
        fields.append(("profile", profile))
        summary = f"{summary}  [{profile}]"
    if operators:
        # Randomised rather than cycled, unlike the profiles: a rollout on the
        # `user` kind buckets each key by hash, so a session that visited them
        # in a fixed order would still split the same way while looking like
        # it had been arranged. The rng is the seeded one, so the sequence is
        # in the session's log and repeatable from `--seed`.
        who = rng.choice(operators)
        fields.append(("operator", who))
        summary = f"{summary}  @{who}"
    if options.campaign:
        fields.append(("campaign", f"{options.campaign}-{index + 1:03d}"))
    if options.offline:
        fields.append(("offline", "1"))
    if options.replay:
        fields.append(("replay", "1"))
    return Request(fields, summary)


def _size_for(options: DriveOptions, rng: random.Random, available: int) -> int:
    """How many screenshots this run reads.

    `None` means vary it, which is the default and the interesting case. A
    given count is clamped to what is on offer -- asking for nine of seven is
    a sloppy command line rather than a run worth refusing.

    Zero is refused rather than clamped, because it used to be read as "not
    specified" and quietly became a random size. A sweep over `0 1 2 3` would
    have had its first run silently do something else entirely, and the flag
    that means "no images" already exists.
    """
    if options.count is None:
        return rng.randint(1, available)
    if options.count < 1:
        raise DriveError(
            f"--count {options.count} reads nothing. For a run with no images, "
            "use --vary roster."
        )
    return min(options.count, available)


def _depth_for(options: DriveOptions, index: int) -> str:
    """One run's depth. `mixed` alternates rather than randomises.

    Alternating so a short session is guaranteed to contain both -- a coin
    flip can hand you five plans in a row, which is the session you were
    trying not to run.
    """
    if options.depth == "mixed":
        return "extract" if index % 2 else "plan"
    if options.depth not in ("plan", "extract"):
        raise DriveError(
            f"{options.depth!r} is not a depth. Use plan, extract or mixed."
        )
    return options.depth


@dataclass
class Attempt:
    """One run the driver started, and what became of it."""

    index: int
    summary: str
    job_id: str | None = None
    state: str = "unknown"
    outcome: str | None = None
    error: str | None = None
    seconds: float = 0.0


@dataclass
class DriveReport:
    """The session, for a caller that wants to assert on it or print it."""

    attempts: list[Attempt] = field(default_factory=list)
    #: Times the server refused because a run was already in flight.
    conflicts: int = 0

    def counted(self, state: str) -> int:
        return sum(1 for a in self.attempts if a.state == state)

    @property
    def summary(self) -> str:
        return (
            f"{len(self.attempts)} started, {self.counted('finished')} finished, "
            f"{self.counted('failed')} failed, {self.conflicts} refused as busy"
        )


def front_page(transport: Transport) -> str:
    """The picker page, or a refusal that says what was wrong with it."""
    reply = transport.get("/")
    if reply.status != 200:
        raise DriveError(
            f"the app answered {reply.status} for its own front page. "
            f"Is {reply.url or 'that URL'} really `bbq-shipment-agent ui`?"
        )
    return reply.body


def screenshots_in(body: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(CHECKBOX.findall(body)))


def operators_in(body: str) -> tuple[str, ...]:
    """The operator keys the form offers. Empty when the pool is empty.

    Parsed rather than loaded, which is the point: `config/operators.yaml` is
    the server's business, and a driver that read it could offer a key the
    running app would refuse.
    """
    return tuple(dict.fromkeys(OPERATOR.findall(body)))


def catalogue_of(transport: Transport) -> tuple[str, ...]:
    """Which images the picker is offering, read off its own page.

    Empty is a legitimate answer: a UI launched at a directory with no `.png`
    files serves the roster form instead, and the driver should drive that
    rather than complain about it.
    """
    return screenshots_in(front_page(transport))


def operators_of(transport: Transport) -> tuple[str, ...]:
    """Who the picker is offering to run as."""
    return operators_in(front_page(transport))


def operators_for(options: DriveOptions, offered: tuple[str, ...]) -> tuple[str, ...]:
    """The keys this session draws from, checked against what is on offer.

    A key the app does not know would be a 400 on every run, so it is named
    here — once, against the population — rather than discovered on the first
    POST with the whole session's pacing already underway.
    """
    if not options.pick_operator:
        return ()
    if not options.operators:
        return offered
    unknown = sorted(set(options.operators) - set(offered))
    if unknown:
        raise DriveError(
            f"the app offers no operator called {', '.join(unknown)}. "
            f"It knows {sorted(offered) or 'nobody'} — see config/operators.yaml "
            "on the machine serving the UI."
        )
    return tuple(options.operators)


def job_id_of(reply: Response) -> str | None:
    """The run id, from the URL urllib landed on after the 303."""
    match = re.search(r"/runs/([0-9a-f]+)", reply.url or "")
    return match.group(1) if match else None


def drive(
    options: DriveOptions,
    transport: Transport,
    *,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> DriveReport:
    """Start runs until the count is reached or the caller interrupts.

    `sleep` and `clock` are parameters so a test can drive a whole session in
    no time at all, which is the only way the pacing rules get asserted rather
    than described.
    """
    rng = random.Random(options.seed)
    # One GET for both populations: they come off the same page, and fetching
    # it twice would let a driver that reloaded mid-session drive two
    # different apps.
    page = front_page(transport)
    catalogue = screenshots_in(page)
    operators = operators_for(options, operators_in(page))
    report = DriveReport()

    say(
        f"  driving {options.base_url}   {len(catalogue)} screenshot(s) offered   "
        f"one run per {options.every:g}s, varying by {options.vary}, "
        f"depth {options.depth}"
    )
    if operators:
        say(f"  running as one of: {', '.join(sorted(operators))}")
    if not options.replay:
        # Named per depth, because the two cost wildly different things and
        # "live" on its own reads as the expensive one.
        say(
            "  these are live runs: a vision call per screenshot"
            + (
                "."
                if options.depth == "extract"
                else ", plus Shippo quotes and a D1 call each."
            )
        )

    try:
        _loop(
            options, transport, report, rng, catalogue, operators, say, sleep, clock
        )
    except KeyboardInterrupt:
        # The ordinary way an open-ended session ends. The tally is what the
        # operator came for, so it is printed either way rather than lost to
        # the traceback.
        say("\n  stopped")
    say(f"\n  {report.summary}")
    return report


def _loop(
    options: DriveOptions,
    transport: Transport,
    report: DriveReport,
    rng: random.Random,
    catalogue: tuple[str, ...],
    operators: tuple[str, ...],
    say: Callable[[str], None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    due = clock()
    index = 0
    while options.runs == 0 or index < options.runs:
        _wait_until(due, sleep, clock)
        request = plan_request(options, catalogue, index, rng, operators)
        started = clock()
        reply = transport.post("/runs", request.fields)

        if reply.status == 409:
            # Somebody is using the browser, or a previous run outlived our
            # wait. Neither is an error; try again on the next poll.
            report.conflicts += 1
            say(f"  #{index + 1:<3} busy — a run is already going, retrying")
            due = clock() + options.poll
            continue
        if reply.status >= 400:
            raise DriveError(
                f"the app refused the run with {reply.status}: {_detail(reply)}"
            )

        attempt = Attempt(index + 1, request.summary, job_id_of(reply))
        report.attempts.append(attempt)
        say(f"  #{attempt.index:<3} started {attempt.job_id or '?'}   {attempt.summary}")

        # The next start is due an interval after this one *began*, so a slow
        # run does not push the schedule out any further than it has to.
        due = started + options.every
        if options.wait:
            _await_finish(attempt, options, transport, sleep, clock, started)
            say(f"      {_verdict(attempt)}")
        index += 1


def _wait_until(
    due: float, sleep: Callable[[float], None], clock: Callable[[], float]
) -> None:
    remaining = due - clock()
    if remaining > 0:
        sleep(remaining)


def _await_finish(
    attempt: Attempt,
    options: DriveOptions,
    transport: Transport,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    started: float,
) -> None:
    """Poll `/runs/{id}/state` until the job stops running.

    Waiting is not politeness, it is the only way the next start is not
    refused — and it is also where the outcome comes from, which is the thing
    a live test wants to read afterwards.
    """
    if attempt.job_id is None:
        attempt.state = "unwatched"
        return
    while True:
        reply = transport.get(f"/runs/{attempt.job_id}/state")
        state = reply.json() if reply.status == 200 else None
        if isinstance(state, dict) and state.get("state") != "running":
            attempt.state = str(state.get("state", "unknown"))
            attempt.outcome = state.get("outcome")
            attempt.error = state.get("error")
            attempt.seconds = clock() - started
            return
        if state is None:
            attempt.state = "unwatched"
            attempt.seconds = clock() - started
            return
        sleep(options.poll)


def _verdict(attempt: Attempt) -> str:
    tail = attempt.error or attempt.outcome or ""
    return f"{attempt.state} in {attempt.seconds:.0f}s   {tail}".rstrip()


def _detail(reply: Response) -> str:
    body = reply.json()
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return reply.body[:200]
