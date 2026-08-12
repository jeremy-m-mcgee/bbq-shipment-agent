"""The web app: pick screenshots, start a run, watch it, read the manifest.

Design 1 says the deliverable is "a reviewable work package that a human
approves". This is a second way to look at one, and it changes nothing about
what the pipeline does: every route below goes through `wiring`, the same as
the CLI, and none of them can reach a stage directly.

## What the page is not allowed to do

The controls are the ones a run is *configured* by, and stop there. There is
no field for the 4.4C arrival threshold, none for the carrier cap, and none
for the kill switch -- the first two are Python constants and the third is a
repo config file, and a form control implying otherwise would be wrong even
if the POST handler ignored it.

## D2 review lives here now

Planning used to be where this front-end stopped -- the manifest was read-only,
the same place `run plan` does. It no longer stops there: a run that produces a
covering manifest parks in `awaiting_review` and the `/runs/{id}/review/*` routes
drive D2 as a pane beside the manifest (design 4, design 10's "a pane beside the
manifest rather than a second manifest"). What does *not* move is the boundary
design 4 draws inside D2: every decision is `ReviewSession`'s, the routes only
carry the operator's move to it, and the narrator only narrates the
classification it is handed. Approval writes the ledger from the browser, which
is the same trust as the CLI -- loopback only, no auth, one operator.

## Loopback only

`create_app` never binds anything itself, but the CLI that serves it binds
127.0.0.1 with no option to change it, and the pages assume that. There is no
authentication, and the reason there is none is that nothing off this machine
can reach it. The content is real home addresses and screenshots of people's
private messages.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from starlette.responses import FileResponse

from ..agents import NarratorUnavailable
from ..operators import OperatorError, OperatorPool
from ..review import Edit, EditKind, ReviewError
from ..wiring import (
    ENV_FILE_REMEDIES,
    RunDepth,
    RunOptions,
    ScreenshotSelection,
    available_screenshots,
    missing_credentials,
)
from .service import RunInProgress, RunService
from .view import render_markdown, review_view, screenshot_catalogue

TEMPLATES = Path(__file__).parent / "templates"


def create_app(
    options: RunOptions,
    screenshot_dir: Path | str | None = None,
    service: RunService | None = None,
) -> FastAPI:
    """Build the app around one set of launch options.

    The options are the floor, not the form: the browser chooses which
    screenshots to read and whether to replay, and everything else -- ledger
    root, cache directory, snapshot path, retry budget, and the profile -- is
    fixed at launch. That split is deliberate. A form field for the ledger
    path is a way to append a real run to the wrong file by mistake.

    The profile is not on the browser form: it is only a LaunchDarkly targeting
    label, and the modes it targets are shown on the run page once resolved.
    The POST still accepts a `profile` field so `drive` can vary it per run,
    but a human run carries the launch profile.
    """
    runs = service or RunService()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        yield
        # A review left open holds the LaunchDarkly client. Close them on
        # shutdown so ctrl-c does not hang on the SDK's background thread
        # (service.py). Harmless when nothing is parked.
        runs.close_all()

    app = FastAPI(
        title="bbq-shipment-agent", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    templates = Jinja2Templates(directory=str(TEMPLATES))
    # The narrator's replies carry light markdown; `| md` renders it safely.
    templates.env.filters["md"] = render_markdown

    directory = Path(screenshot_dir) if screenshot_dir else options.screenshots
    resolved_dir = Path(directory).resolve() if directory else None
    # Loaded once at launch, like the screenshot directory: the population is
    # a committed file, and a page that re-read it per request would offer a
    # key the run in flight was not started with. Rendering it is also what
    # lets `drive` learn the population without being told the file, which is
    # what keeps the driver a client rather than a third front-end.
    pool = OperatorPool.load(options.operators)

    def _images() -> tuple[Path, ...]:
        if resolved_dir is None or not resolved_dir.is_dir():
            return ()
        return available_screenshots(resolved_dir)

    def _allowed() -> dict[str, Path]:
        """The only files `/screenshots/{name}` will serve.

        Built by globbing the resolved directory and keyed by exact filename,
        rather than joining the request's name onto a path. `dir / name` with
        an attacker-supplied name is the whole of a traversal bug, and there
        is no reason to be within arm's reach of one to show seven PNGs.
        """
        return {p.name: p for p in _images()}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "screenshots": screenshot_catalogue(resolved_dir, _images())
                if resolved_dir
                else [],
                "directory": str(directory) if directory else "",
                "options": options,
                "operators": pool.operators,
                "has_recordings": _has_recordings(options),
                # Shown next to the button rather than left for the worker
                # thread to discover. A key that is in `.env` but never loaded
                # looks identical to one that was never set, and the page is
                # where someone is about to spend a minute finding out.
                "missing_keys": missing_credentials(options),
                # The banner's remedy comes from the same place the launch
                # banner's does: one operator's journey, told twice, and it
                # must not say two different things.
                "env_remedies": ENV_FILE_REMEDIES,
                # Whether this run will reach for a network at all, so the page
                # can price it. `replaying` is the form's "replay recordings"
                # box; a server with no recordings can never be true here.
                "replaying": options.replaying,
                "active": runs.active,
                "recent": runs.recent(),
            },
        )

    @app.get("/screenshots/{name}")
    def screenshot(name: str) -> Any:
        path = _allowed().get(name)
        if path is None:
            raise HTTPException(status_code=404, detail="no such screenshot")
        return FileResponse(path, media_type="image/png")

    @app.post("/runs")
    def start_run(
        mode: str = Form("all"),
        screenshot: list[str] = Form(default=[]),
        count: str = Form(""),
        seed: str = Form(""),
        profile: str = Form(""),
        operator: str = Form(""),
        campaign: str = Form(""),
        offline: str = Form(""),
        replay: str = Form(""),
        no_screenshots: str = Form(""),
        depth: str = Form("plan"),
    ) -> Any:
        try:
            # Checked here rather than left for the worker thread: an
            # unknown key is a 400 on the form that sent it, not a run that
            # opens and dies. The department is never posted -- `pool.get`
            # is the only thing that says which one a key belongs to.
            pool.get(operator.strip() or None)
        except OperatorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            chosen = _options_for(
                options,
                directory=resolved_dir,
                mode=mode,
                names=screenshot,
                count=count,
                seed=seed,
                profile=profile,
                operator=operator,
                campaign=campaign,
                offline=bool(offline),
                replay=bool(replay),
                no_screenshots=bool(no_screenshots),
                depth=depth,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            job = runs.start(chosen)
        except RunInProgress as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/runs/{job.id}", status_code=303)

    @app.get("/runs/{job_id}", response_class=HTMLResponse)
    def run_page(request: Request, job_id: str) -> Any:
        job = runs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such run")
        return templates.TemplateResponse(
            request,
            "run.html",
            {
                "job": job,
                "events": job.events,
                "header": job.header,
                "view": job.view,
                # Present whenever a review parked, terminal or not: the pane
                # renders the live (edited) manifest and shows the outcome after
                # approval, so a reload after approving still reads correctly.
                "review": review_view(job.review) if job.review is not None else None,
            },
        )

    @app.get("/runs/{job_id}/events")
    async def run_events(job_id: str, after: int = -1) -> Any:
        """Server-sent events, polled off the worker's list.

        A poll rather than a condition variable because the producer is a
        plain thread and the consumer is asyncio, and 200ms of latency on a
        run that takes a minute buys nothing worth a bridge between the two.
        """
        job = runs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such run")

        async def stream() -> Any:
            seq = after
            while True:
                for event in job.events_after(seq):
                    seq = event.seq
                    yield f"data: {json.dumps(event.to_json())}\n\n"
                if not job.running:
                    yield f"data: {json.dumps({'kind': 'end', 'state': job.state})}\n\n"
                    return
                await asyncio.sleep(0.2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/runs/{job_id}/manifest.txt", response_class=PlainTextResponse)
    def manifest_text(job_id: str) -> Any:
        """The manifest exactly as `run plan --out` writes it.

        The page is easier to read and this is easier to keep. Both come from
        the same `render`, so they cannot disagree.

        A parked review is served *its* manifest, not the run row's: an edit
        re-solves, and a text export still showing the planned carriers after
        the operator moved someone to Saturday describes a plan nobody
        approved.
        """
        job = runs.get(job_id)
        if job is None or not job.view:
            raise HTTPException(status_code=404, detail="no manifest")
        if job.review is not None:
            edited = review_view(job.review)["manifest_text"]
            if edited:
                return edited
        return job.view["manifest_text"] or "no manifest"

    @app.get("/runs/{job_id}/state")
    def run_state(job_id: str) -> Any:
        job = runs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such run")
        return JSONResponse(
            {
                "id": job.id,
                "state": job.state,
                "error": job.error,
                "outcome": (job.view or {}).get("outcome"),
            }
        )

    # -- D2 review -------------------------------------------------------------
    #
    # Every route takes an operator move to `ReviewSession`, which owns the
    # decision, and returns the re-rendered pane so the page swaps it in place.
    # The routes carry no control flow of their own: they classify nothing, and
    # the narrator only narrates what the session already decided (design 4).

    def _open_review(job_id: str) -> Any:
        """The parked controller for an open review, or the right HTTP error.

        404 for no such run; 409 once the review has ended or never opened, so a
        stale tab POSTing into a finished review is told rather than crashing."""
        job = runs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such run")
        controller = job.review
        if controller is None or job.state != "awaiting_review":
            raise HTTPException(
                status_code=409,
                detail="this run has no open review (it never started one, or "
                "it has already ended).",
            )
        return job, controller

    def _pane(request: Request, job: Any) -> Any:
        return templates.TemplateResponse(
            request,
            "_review_pane.html",
            {"job": job, "review": review_view(job.review)},
        )

    def _turn(request: Request, job_id: str, action: Any) -> Any:
        """One serialized review turn: acquire the lock, act, re-render.

        The lock is non-blocking, so an overlapping request (a double-submit, an
        impatient retry while a narration turn is still running) is refused with
        409 rather than queued into a second identical edit. `ReviewError` and
        the value errors an edit raises come back as 400 in the conversation;
        the review does not die on a bad argument."""
        job, controller = _open_review(job_id)
        if not controller.turn_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="a review turn is already in progress; wait for it to finish.",
            )
        try:
            action(job, controller)
        except (ReviewError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NarratorUnavailable:
            # The model died mid-turn. Drop the narrator and fall back to the
            # button-driven review rather than killing it (cli._review does the
            # same). The pane re-renders without the chat box, so the operator
            # keeps their edit and approval controls.
            controller.narrator = None
        finally:
            controller.turn_lock.release()
        return _pane(request, job)

    @app.post("/runs/{job_id}/review/say", response_class=HTMLResponse)
    def review_say(request: Request, job_id: str, message: str = Form(...)) -> Any:
        def act(job: Any, controller: Any) -> None:
            if controller.narrator is None:
                raise HTTPException(
                    status_code=409,
                    detail="this review is button-driven; there is no narrator "
                    "to ask (offline, or no model key).",
                )
            controller.narrator.say(message)

        return _turn(request, job_id, act)

    @app.post("/runs/{job_id}/review/say-stream")
    async def review_say_stream(job_id: str, message: str = Form(...)) -> Any:
        """The same narration turn as `/review/say`, streamed token by token.

        The reply renders as it arrives instead of after it finishes. The turn
        runs on a worker thread (the model call blocks) and pushes text deltas
        onto a queue the async generator drains as SSE; when the turn ends it
        sends the re-rendered pane as the final event, which the client swaps in.
        The client falls back to the synchronous endpoint if this fails, so the
        sync route stays the source of truth for what a turn does."""
        import queue as _queue
        import threading as _threading

        job, controller = _open_review(job_id)
        if controller.narrator is None:
            raise HTTPException(
                status_code=409,
                detail="this review is button-driven; there is no narrator to ask.",
            )
        # Same non-blocking turn lock as the sync route: an overlapping turn is
        # refused, not interleaved. Released in the generator's `finally`, so a
        # client that disconnects mid-stream still frees it.
        if not controller.turn_lock.acquire(blocking=False):
            raise HTTPException(
                status_code=409,
                detail="a review turn is already in progress; wait for it to finish.",
            )

        events: Any = _queue.Queue()
        done = object()

        def run_turn() -> None:
            try:
                controller.narrator.say(
                    message, on_delta=lambda d: events.put(("delta", d))
                )
            except NarratorUnavailable:
                # The model died mid-turn: drop it and fall back to buttons, the
                # same as the sync route. The final fragment re-renders without
                # the chat box.
                controller.narrator = None
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                events.put(("error", str(exc)))
            finally:
                events.put((done, None))

        _threading.Thread(target=run_turn, daemon=True).start()

        async def stream() -> Any:
            try:
                while True:
                    try:
                        kind, value = events.get_nowait()
                    except _queue.Empty:
                        await asyncio.sleep(0.03)
                        continue
                    if kind is done:
                        break
                    yield f"data: {json.dumps({kind: value})}\n\n"
                # The turn has fully returned (recording included), so the pane
                # renders its final state. `_review_pane.html` uses no request.
                html = templates.env.get_template("_review_pane.html").render(
                    job=job, review=review_view(job.review)
                )
                yield f"data: {json.dumps({'done': True, 'html': html})}\n\n"
            finally:
                controller.turn_lock.release()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/runs/{job_id}/review/edit", response_class=HTMLResponse)
    def review_edit(
        request: Request,
        job_id: str,
        kind: str = Form(...),
        recipient_key: str = Form(...),
        ship_date: str = Form(""),
        reason: str = Form(""),
    ) -> Any:
        from datetime import date

        def act(job: Any, controller: Any) -> None:
            try:
                edit = Edit(
                    kind=EditKind(kind),
                    recipient_key=recipient_key,
                    ship_date=date.fromisoformat(ship_date) if ship_date else None,
                    reason=reason,
                )
            except ValueError as exc:
                # A bad enum member or an unparseable date: a 400 the operator
                # sees, not a 500. `propose` raises `ReviewError` for the rest.
                raise ReviewError(str(exc)) from exc
            controller.session.propose(edit)

        return _turn(request, job_id, act)

    @app.post("/runs/{job_id}/review/confirm", response_class=HTMLResponse)
    def review_confirm(request: Request, job_id: str) -> Any:
        return _turn(request, job_id, lambda job, c: c.session.confirm())

    @app.post("/runs/{job_id}/review/discard", response_class=HTMLResponse)
    def review_discard(request: Request, job_id: str) -> Any:
        return _turn(request, job_id, lambda job, c: c.session.discard())

    @app.post("/runs/{job_id}/review/approve", response_class=HTMLResponse)
    def review_approve(request: Request, job_id: str) -> Any:
        def act(job: Any, controller: Any) -> None:
            state = controller.session.approve()  # writes E2, may raise
            job.state = state.value
            controller.close()

        return _turn(request, job_id, act)

    @app.post("/runs/{job_id}/review/reject", response_class=HTMLResponse)
    def review_reject(request: Request, job_id: str, reason: str = Form("")) -> Any:
        def act(job: Any, controller: Any) -> None:
            state = controller.session.reject(reason)
            job.state = state.value
            controller.close()

        return _turn(request, job_id, act)

    @app.post("/runs/{job_id}/review/abandon", response_class=HTMLResponse)
    def review_abandon(request: Request, job_id: str) -> Any:
        def act(job: Any, controller: Any) -> None:
            controller.abandon()
            job.state = "abandoned"

        return _turn(request, job_id, act)

    return app


def _has_recordings(options: RunOptions) -> bool:
    return any(
        (
            options.quotes,
            options.validations,
            options.completions,
            options.extractions,
            options.repairs,
        )
    )


def _options_for(
    base: RunOptions,
    *,
    directory: Path | None,
    mode: str,
    names: list[str],
    count: str,
    seed: str,
    profile: str,
    campaign: str,
    operator: str = "",
    offline: bool,
    replay: bool,
    no_screenshots: bool,
    depth: str = "plan",
) -> RunOptions:
    """The form, as options. Every branch here is a user-visible choice.

    Kept out of the route so it can be tested without a request, and kept out
    of `wiring` because parsing a checkbox is not something the CLI should
    have to know about.
    """
    selection: ScreenshotSelection | None = None
    screenshots: Path | None = None if no_screenshots else directory

    try:
        wanted_depth = RunDepth(depth)
    except ValueError as exc:
        raise ValueError(f"{depth!r} is not a depth. Use plan or extract.") from exc
    if wanted_depth is RunDepth.EXTRACT and screenshots is None:
        # The roster file is already structured, so there is nothing for B1 to
        # do and an extract-only run over it would open a run record and stop
        # having read nothing.
        raise ValueError(
            "an extract-only run needs screenshots. The roster path has "
            "nothing to extract."
        )

    if screenshots is not None:
        if mode == "explicit":
            if not names:
                raise ValueError(
                    "no screenshots were selected. Pick at least one, or "
                    "choose a different mode."
                )
            selection = ScreenshotSelection(explicit=tuple(names))
        elif mode == "sample":
            if not count.strip():
                raise ValueError("a sample needs a count.")
            try:
                wanted = int(count)
            except ValueError as exc:
                raise ValueError(f"{count!r} is not a number of screenshots.") from exc
            chosen_seed = int(seed) if seed.strip() else None
            selection = ScreenshotSelection(count=wanted, seed=chosen_seed)
        # mode == "all" leaves selection None, which reads the directory.

    options = replace(
        base,
        screenshots=screenshots,
        selection=selection,
        profile=profile.strip() or None,
        operator=operator.strip() or None,
        campaign=campaign.strip() or None,
        offline=offline,
        depth=wanted_depth,
    )
    if not replay:
        # The recordings were supplied at launch; this run was asked to go
        # live instead. Blanking them is the whole difference -- `wiring`
        # builds a live client for any path with no recording.
        options = replace(
            options,
            quotes=None,
            validations=None,
            completions=None,
            extractions=None,
            repairs=None,
        )
    return options
