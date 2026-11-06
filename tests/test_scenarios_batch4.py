"""Regression tests from scenario Batch 4 (robustness, 2026-10-24)."""

import asyncio
import os
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_for_openfilters import engine, jobs, server, settings, tasks

BANDPASS = {"substrate": "BK7", "center_wavelength": 532,
            "formula": {"formula": "(HL)^4 HH (LH)^4", "materials": {"H": "TiO2", "L": "SiO2"}}}


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path))
    engine.catalog(reload=True)


def _wait_until(condition, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.1)
    return False


def test_campaign_ranking_treats_tiny_merits_as_zero():
    # S4.14: a 3-layer design with merit 3.4e-23 ranked above a 2-layer design with 5.2e-22.
    rows = [{"task": 0, "all_targets_met": True, "merit_chi2": 3.4e-23, "nb_layers": 3, "total_thickness_nm": 200},
            {"task": 1, "all_targets_met": True, "merit_chi2": 5.2e-22, "nb_layers": 2, "total_thickness_nm": 250},
            {"task": 2, "all_targets_met": True, "merit_chi2": 0.0, "nb_layers": 6, "total_thickness_nm": 300},
            {"task": 3, "all_targets_met": False, "merit_chi2": 0.5, "nb_layers": 1, "total_thickness_nm": 100}]
    assert [r["task"] for r in sorted(rows, key=jobs.rank_key)] == [1, 0, 2, 3]


def test_extrapolation_warning_names_only_the_wavelengths_outside_the_data():
    # S4.7: "values at 1500-2500 nm are extrapolated" although Ge's data reach 1771 nm.
    design = {"substrate": "BK7", "layers": [{"material": "Ge", "thickness": 50}]}
    (warning,) = engine.material_warnings(design, [1500, 2500])
    assert "at 1771-2500 nm n,k are held" in warning and "1500" not in warning


def test_worse_between_grid_points_is_reported_for_a_failing_design():
    # S4.10: worst R 98.5 % on a 5 nm grid while the curve dipped to 58 % between grid points.
    targets = [{"kind": "T", "from": 500, "to": 560, "step": 5, "value": 0.05, "inequality": "<="}]
    r = engine.evaluate(BANDPASS, targets)
    assert not r["all_targets_met"] and r["targets"][0]["worst_value"] < 0.5
    (warning,) = r["warnings"]
    # The peak itself (S5.10: narrow features are refined beyond the 4x grid point at 532.5 nm).
    assert "worse between the targets' grid points" in warning and "0.956897 at 532.0 nm" in warning


def test_no_between_warning_when_the_fine_grid_is_not_worse():
    targets = [{"kind": "T", "from": 400, "to": 500, "step": 5, "value": 0.01, "inequality": ">="}]
    r = engine.evaluate(BANDPASS, targets)
    assert not r["all_targets_met"] and "warnings" not in r


def test_refused_optimization_creates_no_job():
    # S4.8: a design with no front layer was refused, but logged as a "done" job.
    targets = [{"kind": "R", "wavelength": 550, "value": 0.05, "inequality": "<="}]
    before = set(os.listdir(settings.jobs_dir()))
    with pytest.raises(ToolError, match="at least one front layer"):
        asyncio.run(server.optimize_design({"substrate": "BK7", "layers": []}, targets))
    with pytest.raises(ToolError, match="method must be one of"):
        asyncio.run(server.optimize_design(BANDPASS, targets, method="annealing"))
    assert set(os.listdir(settings.jobs_dir())) == before


def test_job_whose_tasks_all_fail_is_failed_not_done():
    targets = [{"kind": "R", "wavelength": 550, "value": 0.05, "inequality": "<="}]
    m = jobs.JobManager(workers=1)
    try:
        job_id = m.submit("optimize", [{"op": "optimize", "params": {}, "args": {
            "design": {"substrate": "BK7", "layers": []}, "target_specs": targets, "method": "refine"}}],
            meta={"design": {"substrate": "BK7", "layers": []}, "targets": targets})
        assert _wait_until(lambda: m.status(job_id)["status"] != "running")
        status = m.status(job_id)
        assert status["status"] == "failed" and "front layer" in status["error"]
    finally:
        m.shutdown()


def test_cancel_counts_tasks_and_stops_queued_ones():
    # S4.11: 33 reported cancelled, but the 24 tasks the process pool had already queued ran anyway.
    targets = [{"kind": "R", "from": 400, "to": 900, "step": 1, "value": 0.002, "inequality": "<="}]
    start = {"substrate": "BK7", "layers": [{"material": m, "thickness": 80.0} for m in ["TiO2", "SiO2"] * 4]}
    task = {"op": "optimize", "params": {}, "args": {"design": start, "target_specs": targets, "method": "needle",
                                                     "options": {"time_limit": 3}}}
    m = jobs.JobManager(workers=1)
    try:
        job_id = m.submit("campaign", [task] * 6, meta={"targets": targets, "top_n": 3, "tolerance": None})
        folder = m.job_dir(job_id)
        assert _wait_until(lambda: any(n.endswith(tasks.STARTED_SUFFIX) for n in os.listdir(os.path.join(folder, "tasks"))))
        out = m.cancel(job_id)
        assert out["tasks_done"] + out["tasks_still_running"] + out["tasks_cancelled"] == 6
        assert out["tasks_cancelled"] >= 4
        expected = out["tasks_done"] + out["tasks_still_running"]
        assert _wait_until(lambda: len(m.rows(job_id)) >= expected)
        time.sleep(4)  # long enough for a queued task to have run, had it not been stopped
        assert len(m.rows(job_id)) == expected
        partial = m.results(job_id)
        assert [r["rank"] for r in partial["rows"]] == list(range(1, expected + 1))
        again = m.cancel(job_id)
        assert again["status"] == "cancelled" and again["tasks_cancelled"] == 0 and "already" in again["note"]
    finally:
        m.shutdown()


def test_cancel_on_a_finished_job_leaves_it_done(tmp_path):
    m = jobs.JobManager(workers=1)
    try:
        job_id = m.submit("batch", [{"op": "spectrum", "params": {}, "args": {
            "design": BANDPASS, "quantities": ["R"], "wavelengths": [532]}}], label="tiny")
        assert _wait_until(lambda: m.status(job_id)["status"] == "done")
        out = m.cancel(job_id)
        assert out["status"] == "done" and out["tasks_cancelled"] == 0
        assert m.status(job_id)["status"] == "done"
    finally:
        m.shutdown()


def test_describe_omits_the_echo_of_long_formulas():
    # S4.9: 121 layers came back twice (table + normalized JSON).
    long = {"substrate": "BK7", "center_wavelength": 800,
            "formula": {"formula": "(HL)^60 H", "materials": {"H": "TiO2", "L": "SiO2"}}}
    out = asyncio.run(server.describe_design(long))
    assert out["nb_layers"] == 121 and "design" not in out and "design_note" in out
    short = asyncio.run(server.describe_design(BANDPASS))
    assert short["design"]["layers"]
