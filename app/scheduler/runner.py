"""Scheduler runner.

MVP: an infinite loop that runs the pipeline every `search_cadence_hours`,
honouring the pause control. Later this becomes Celery/Redis tasks — the loop
stays as the orchestrator that enqueues them.
"""
from __future__ import annotations

import logging
import time

from app.config import get_config
from app.scheduler.control import is_paused
from app.workflows.pipeline import run_pipeline

logger = logging.getLogger(__name__)


def serve_forever(poll_seconds: int = 60) -> None:
    config = get_config()
    cadence = config.scheduler.get("search_cadence_hours", 12) * 3600
    logger.info("Scheduler starting (cadence %ds, poll %ds)", cadence, poll_seconds)
    next_run = time.monotonic()
    while True:
        if not is_paused():
            try:
                result = run_pipeline()
                logger.info("Run complete: discover=%s analyze=%s actions=%s",
                            result.discovery.get("new_jobs"),
                            result.analysis.get("analyzed"),
                            result.action.get("applied"))
            except Exception:
                logger.exception("Scheduled run failed")
            next_run = time.monotonic() + cadence
        else:
            logger.info("Paused — skipping run")
        while time.monotonic() < next_run:
            time.sleep(min(poll_seconds, max(1, next_run - time.monotonic())))
