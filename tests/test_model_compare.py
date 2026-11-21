from evals.model_compare import compare, run


def test_model_compare_mock_runs_and_summarizes():
    res = run("mock", only="s1_")
    s = res["summary"]
    assert s["turns"] == 1 and s["routing_accuracy"] == 1.0 and s["cost_usd_total"] == 0
    md = compare(res, res)
    assert "routing_accuracy" in md
