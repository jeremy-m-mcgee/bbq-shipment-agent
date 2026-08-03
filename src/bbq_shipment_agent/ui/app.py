"""The web app: pick screenshots, start a run, watch it, read the manifest.

Design 1 says the deliverable is "a reviewable work package that a human
approves". This is a second way to look at one, and it changes nothing about
what the pipeline does: every route below goes through `wiring`, the same as
the CLI, and none of them can reach a stage directly.

## What the page is not allowed to do

The controls are the ones a run is *configured* by, and stop there. There is
no field for the 4.4C arrival threshold, none for the carrier cap, and none
for authority -- those are a Python constant, a Python constant and a repo
config file respectively, and a form control implying otherwise would be
wrong even if the POST handler ignored it. The manifest is read-only: this
front-end stops at the same place `run plan` does.

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

from ..wiring import RunOptions, ScreenshotSelection, available_screenshots
from .service import RunInProgress, RunService
from .view import screenshot_catalogue

TEMPLATES = Path(__file__).parent / "templates"


def create_app(
    options: RunOptions,
    screenshot_dir: Path | str | None = None,
    service: RunService | None = None,
) -> FastAPI:
    """Build the app around one set of launch options.

    The options are the floor, not the form: the browser chooses which
    screenshots to read, the profile and whether to replay, and everything
    else -- ledger root, cache directory, snapshot path, retry budget -- is
    fixed at launch. That split is deliberate. A form field for the ledger
    path is a way to append a real run to the wrong file by mistake.
    """
    app = FastAPI(title="bbq-shipment-agent", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    runs = service or RunService()

    directory = Path(screenshot_dir) if screenshot_dir else options.screenshots
    resolved_dir = Path(directory).resolve() if directory else None

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
                "has_recordings": _has_recordings(options),
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
        campaign: str = Form(""),
        offline: str = Form(""),
        replay: str = Form(""),
        no_screenshots: str = Form(""),
    ) -> Any:
        try:
            chosen = _options_for(
                options,
                directory=resolved_dir,
                mode=mode,
                names=screenshot,
                count=count,
                seed=seed,
                profile=profile,
                campaign=campaign,
                offline=bool(offline),
                replay=bool(replay),
                no_screenshots=bool(no_screenshots),
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
        """
        job = runs.get(job_id)
        if job is None or not job.view:
            raise HTTPException(status_code=404, detail="no manifest")
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
    offline: bool,
    replay: bool,
    no_screenshots: bool,
) -> RunOptions:
    """The form, as options. Every branch here is a user-visible choice.

    Kept out of the route so it can be tested without a request, and kept out
    of `wiring` because parsing a checkbox is not something the CLI should
    have to know about.
    """
    selection: ScreenshotSelection | None = None
    screenshots: Path | None = None if no_screenshots else directory

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
        campaign=campaign.strip() or None,
        offline=offline,
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
