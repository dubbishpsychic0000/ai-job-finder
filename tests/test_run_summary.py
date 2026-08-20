from app.workflows.pipeline import RunResult, render_run_summary


def test_run_summary_reports_zero_result_run():
    text = render_run_summary(RunResult(
        discovery={"new_jobs": 0, "fetched": 0, "errors": []},
        analysis={"errors": []}, action={"applied": 0, "asked": 0, "investigated": 0, "drafts": 0, "errors": []},
        followup={"errors": []},
    ))
    assert "0 new / 0 fetched" in text
    assert "Gmail drafts: 0" in text


def test_run_summary_counts_drafts_and_errors():
    text = render_run_summary(RunResult(
        discovery={"new_jobs": 2, "fetched": 7, "errors": ["source"]},
        analysis={"errors": []},
        action={"applied": 1, "asked": 1, "investigated": 1, "drafts": 2,
                "errors": ["action"]}, followup={"errors": []},
    ))
    assert "2 new / 7 fetched" in text
    assert "Gmail drafts: 2 | Errors: 2" in text


def test_notification_digest_handles_sqlite_naive_delivery_time(db):
    """SQLite round-trips DateTime values without timezone metadata."""
    from datetime import timedelta

    from app import memory as mem
    from app.models import utcnow
    from app.notifications.service import NotificationService

    row = mem.store.enqueue_notification(db, "JOB_FOUND")
    row.status = "delivered"
    row.delivered_at = (utcnow() - timedelta(minutes=1)).replace(tzinfo=None)
    db.flush()
    assert NotificationService(db).digest_due(now=utcnow()) is False
