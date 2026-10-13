"""``python -m worker``: run the review worker as its own process.

docs/WORKFLOW_DESIGN.md §8: the worker is a separate systemd unit (or
container) with its own event loop and engine, so an API restart never
kills an in-flight review. SIGTERM/SIGINT stop claiming; the run then
drains what it holds (docs/BACKEND_ARCHITECTURE.md § Deployment) — the
supervisor's shutdown grace must exceed the longest review.

Import-safe: everything side-effectful lives in ``main()`` behind the
``__main__`` guard, so tests can import this module.
"""

import asyncio
import logging
import os
import signal
from contextlib import suppress

from adapters.clock import SystemClock
from adapters.github.config import GitHubAppSettings
from adapters.github.factory import build_vcs_client
from domain.ports import GitProvider
from worker.config import WorkerSettings
from worker.loop import run_worker


def build_vcs_from_env() -> GitProvider:
    """The same VCS wiring as ``api.main.build_app``: GitHubAppSettings →
    ``build_vcs_client`` (the App auth lives inside the factory)."""
    return build_vcs_client(GitHubAppSettings.from_env(), clock=SystemClock())


async def _run_until_signalled(settings: WorkerSettings, vcs: GitProvider) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # add_signal_handler needs a running loop and is not implemented
        # on every platform (e.g. ProactorEventLoop historically); a plain
        # KeyboardInterrupt still ends asyncio.run without it.
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_worker(settings, vcs, stop=stop)
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.remove_signal_handler(sig)


def main() -> None:
    # Like main.py: uvicorn configures only its own loggers, so the root
    # handler here is what makes the worker's INFO records visible.
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    settings = WorkerSettings.from_env()
    vcs = build_vcs_from_env()
    asyncio.run(_run_until_signalled(settings, vcs))


if __name__ == "__main__":
    main()
