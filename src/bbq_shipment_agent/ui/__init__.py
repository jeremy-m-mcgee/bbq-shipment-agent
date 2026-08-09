"""The local web UI. A second front-end onto the same pipeline.

`app` holds the routes, `service` runs a plan on a worker thread, and `view`
turns a `PlanResult` into the plain data a template renders. Nothing here
sequences a stage or computes a number -- design 6.1 keeps control flow in
Python, and this is a window onto it rather than a second copy.
"""

from .app import create_app
from .service import Event, ReviewController, RunInProgress, RunJob, RunService

__all__ = [
    "create_app",
    "Event",
    "ReviewController",
    "RunInProgress",
    "RunJob",
    "RunService",
]
