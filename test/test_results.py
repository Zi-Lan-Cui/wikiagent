from evals.core.results import aggregate_reports, summarize_cases


def test_summary_reports_confusion_metrics_only_for_labeled_cases():
    summary = summarize_cases(
        [
            {"expected_verdict": "pass", "verdict": "pass"},
            {"expected_verdict": "fail", "verdict": "pass"},
            {"verdict": "review"},
        ]
    )
    assert summary["labeled_cases"] == 2
    assert summary["confusion_matrix"] == {"pass->pass": 1, "fail->pass": 1}
    assert summary["precision"] == 0.5
    assert summary["recall"] == 0.5


def test_aggregate_reports_keeps_component_boundaries():
    report = aggregate_reports(
        [{"component": "qa", "cases": [{"expected_verdict": "pass", "verdict": "pass"}]}]
    )
    assert report["status"] == "pass"
    assert report["components"]["qa"]["cases"] == 1
