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
from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import GitHubAppSettings
from adapters.github.factory import build_vcs_client
from adapters.github.publisher import GitHubReviewPublisher
from adapters.llm.config import LLMSettings
from adapters.llm.factory import build_gateway
from adapters.observability import metrics
from adapters.observability.config import LoggingSettings, SentrySettings
from adapters.observability.logging import configure_logging
from adapters.observability.sentry import init_sentry
from domain.errors import ERROR_KINDS
from domain.ports import GitProvider
from worker.config import WorkerSettings
from worker.loop import run_worker
from worker.process import DEBUG_LOG_DIFF_ENV, RECORDED_STEPS, ReviewStage

logger = logging.getLogger(__name__)


def build_vcs_from_env() -> GitProvider:
    """The same VCS wiring as ``api.main.build_app``: GitHubAppSettings →
    ``build_vcs_client`` (the App auth lives inside the factory)."""
    return build_vcs_client(GitHubAppSettings.from_env(), clock=SystemClock())


def build_review_from_env() -> ReviewStage:
    """The LLM gateway and the GitHub publisher. A missing LLM or GitHub
    variable fails here, at startup, naming the variable."""
    app_settings = GitHubAppSettings.from_env()
    return ReviewStage(
        gateway=build_gateway(LLMSettings.from_env()),
        publisher=GitHubReviewPublisher(app_settings, GitHubAppAuth(app_settings, SystemClock())),
    )


async def _run_until_signalled(
    settings: WorkerSettings, vcs: GitProvider, review: ReviewStage
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # add_signal_handler needs a running loop and is not implemented
        # on every platform (e.g. ProactorEventLoop historically); a plain
        # KeyboardInterrupt still ends asyncio.run without it.
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_worker(settings, vcs, stop=stop, review=review)
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.remove_signal_handler(sig)


def main() -> None:
    # JSON to stdout and, with LOG_DIR, to the rotated worker.jsonl
    # (docs/observability.md § Logs).
    logging_settings = LoggingSettings.from_env()
    configure_logging(logging_settings, service="worker")
    if os.environ.get(DEBUG_LOG_DIFF_ENV) == "1" and not logging_settings.is_local:
        # The escape hatch logs raw diffs, so it works only in ENV=local.
        logger.warning("worker.debug_log_diff.ignored")
    init_sentry(SentrySettings.from_env())
    settings = WorkerSettings.from_env()
    vcs = build_vcs_from_env()
    review = build_review_from_env()
    # Prometheus on the internal :9101, for Alloy (docs/observability.md § Metrics). The
    # server thread is a daemon: it ends with the process, after the drain.
    metrics.init_worker_metrics(ERROR_KINDS, steps=[step.value for step in RECORDED_STEPS])
    metrics.serve(metrics.WORKER_REGISTRY, metrics.WORKER_PORT)
    asyncio.run(_run_until_signalled(settings, vcs, review))


if __name__ == "__main__":
    main()
