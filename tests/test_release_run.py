"""Regression tests from the pre-release scenario run through the live tools (2026-11-06, version 0.2.0)."""

import time

import pytest

from mcp_for_openfilters import engine, jobs


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path))
    engine.catalog(reload=True)


VCOAT_TARGET = [{"kind": "R", "wavelength": 1064, "value": 0.0005, "inequality": "<=", "tolerance": 0.0001}]


@pytest.mark.parametrize("layers", [
    [("SiO2", 299.63), ("TiO2", 208.85)],
    [("SiO2", 203.38), ("TiO2", 89.97), ("SiO2", 98.15)],
])
def test_needle_run_with_fresh_needles_does_not_crawl(layers):
    # S5.12: two starts of a V-coat campaign (12 starts, max_layers 6) ran for the whole default time limit
    # (300 s each), 550 000 iterations in 20 s, and still failed (chi2 ~8e4): after inserting needles,
    # OpenFilters' Levenberg-Marquardt crawled with the needles at its 0 nm bound (it sets no iteration
    # limit; same in the pure-Python core). Restarted with those layers at 1 nm it converges at once.
    design = {"substrate": "FusedSilica", "center_wavelength": 1064,
              "layers": [{"material": m, "thickness": t} for m, t in layers]}
    start = time.time()
    result = engine.optimize(design, VCOAT_TARGET, "needle", {"max_layers": 6, "time_limit": 60})
    assert time.time() - start < 15
    assert result["all_targets_met"] and result["stop_reason"] != "time_limit"
    assert result["edge_needles_added"] == 0  # stall restarts are not edge needles


def test_stalled_run_stops_with_an_explanation(monkeypatch):
    # Without the restart (refine method), a stall is reported as such rather than running to the time limit.
    monkeypatch.setattr(engine, "STALL_ITERATIONS", 5)
    monkeypatch.setattr(engine, "STALL_GAIN", 0.999)  # every window "stalls"
    design = {"substrate": "FusedSilica", "center_wavelength": 1064,
              "layers": [{"material": "TiO2", "thickness": 300}, {"material": "SiO2", "thickness": 100}]}
    result = engine.optimize(design, VCOAT_TARGET, "refine", {"time_limit": 30})
    assert result["stop_reason"] == "stalled"
    assert "less than 1 %" in result["stop_explanation"]


def test_out_of_data_range_names_only_the_wavelengths_used():
    # S4.7: Ge (data 124-1771 nm) used at 8000-12000 nm was reported as "used at 1771-12000 nm".
    design = {"substrate": "Si", "layers": [{"material": "Ge", "thickness": 500}]}
    with pytest.raises(engine.EngineError, match=r"n,k at 8000-12000 nm"):
        engine.spectra(design, ["R"], {"from": 8000, "to": 12000, "step": 50})
    (name, data, ranges), = engine.outside_data(design, [1500, 2500])
    assert ranges == [(data[1], 2500.0)]  # partly inside: from the end of the data
    with pytest.raises(engine.EngineError, match=r"used at 8000-12000 nm"):
        engine.require_extrapolation_choice(design, [8000, 12000], "This optimization")


def test_repeated_polarizations_give_one_series_each():
    # Batch 6: polarizations [30, "30", 45, "unpolarized"] gave four series, two of them duplicates.
    design = {"substrate": "BK7", "layers": [{"material": "TiO2", "thickness": 60}]}
    out = engine.spectra(design, ["R", "R"], [550], [45, 45.0], [30, "30", "30deg", 45, "unpolarized", "s", 90])
    assert [(s["quantity"], s["polarization"]) for s in out["series"]] == \
        [("R", "30deg"), ("R", "unpolarized"), ("R", "s")]


def test_tolerance_warns_when_the_design_itself_fails():
    # Batch 6: a yield of 0 for a design that misses its targets even without errors came with no hint.
    design = {"substrate": "BK7", "layers": [{"material": "SiO2", "thickness": 100}]}
    targets = [{"kind": "R", "wavelength": 550, "value": 0.001, "inequality": "<="}]
    task = {"op": "tolerance", "params": {}, "args": {"design": design, "target_specs": targets, "n_trials": 20,
                                                       "thickness_error": {"type": "relative", "sigma": 0.01}}}
    m = jobs.JobManager(workers=1)
    try:
        job_id = m.submit("tolerance", [task])
        end = time.time() + 60
        while m.status(job_id)["status"] == "running" and time.time() < end:
            time.sleep(0.2)
        summary = m.results(job_id)["summary"]
        assert summary["nominal_passes"] is False
        assert summary["warnings"][0].startswith("the design itself does not meet the targets")
    finally:
        m.shutdown()
