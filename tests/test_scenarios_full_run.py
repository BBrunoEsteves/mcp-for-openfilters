"""Regression tests from the full scenario run (Batches 1-5 in one chat, 2026-10-24): 54/57 pass."""

import time

import pytest

from openfilters_mcp import engine, jobs


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFILTERS_MCP_HOME", str(tmp_path))
    engine.catalog(reload=True)


def test_layer_errors_use_the_same_numbers_as_layer_tables():
    # S4.4: "layer 0 has a negative thickness" while describe_design numbers layers from 1.
    with pytest.raises(engine.EngineError, match="layer 1 has a negative thickness"):
        engine.build_filter({"substrate": "BK7", "layers": [{"material": "SiO2", "thickness": -5}]})


def test_campaign_rows_carry_warnings_and_yield_intervals():
    # S2.1: campaign rows said "met" with no hint of the between-grid miss evaluate_design reported, and
    # yields 0.315 vs 0.22 (200 trials) came without intervals.
    targets = [{"kind": "R", "from": 430, "to": 670, "step": 10, "value": 0.005, "inequality": "<="}]
    start = {"substrate": "BK7", "center_wavelength": 550,
             "layers": [{"material": m, "thickness": t} for m, t in
                        (("TiO2", 12.0), ("SiO2", 39.0), ("TiO2", 117.0), ("SiO2", 82.0))]}
    task = {"op": "optimize", "params": {}, "args": {"design": start, "target_specs": targets, "method": "refine"}}
    m = jobs.JobManager(workers=1)
    try:
        job_id = m.submit("campaign", [task], meta={"targets": targets, "top_n": 1, "tolerance": {
            "n_trials": 50, "thickness_error": {"type": "relative", "sigma": 0.01}, "seed": 0}})
        end = time.time() + 120
        while m.status(job_id)["status"] == "running" and time.time() < end:
            time.sleep(0.2)
        out = m.results(job_id)
        row = out["rows"][0]
        lo, hi = row["yield_ci95"]
        assert lo <= row["yield"] <= hi and hi - lo > 0.05
        result = m.result(job_id, row["task"])["result"]
        assert row.get("warnings") == result.get("warnings")
        assert out["summary"]["openfilters_errors"] == 0
    finally:
        m.shutdown()
