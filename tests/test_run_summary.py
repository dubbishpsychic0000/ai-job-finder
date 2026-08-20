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
