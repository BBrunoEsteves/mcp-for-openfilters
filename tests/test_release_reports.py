"""Regression tests from the two pre-release chat reports (2026-11-06, version 0.2.0): Batch 6 (8/8 pass) and
Batches 1-5 (57/57 pass), whose suggested improvements were checked one by one."""

import json
import os
import time

import numpy as np
import pytest

from mcp_for_openfilters import engine, jobs, plotting, thermal

VCOAT = [{"kind": "R", "wavelength": 1064, "value": 0.0005, "inequality": "<="}]


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path))
    engine.catalog(reload=True)


def _layers(*pairs):
    return [{"material": m, "thickness": t} for m, t in pairs]


def test_compiled_solver_has_no_uninitialized_rows():
    # OpenFilters' C++ Levenberg-Marquardt allocated its work arrays uninitialized; b has max(nb_par,
    # nb_points) rows and only nb_points are set, so with 3 thicknesses and 1 target point leftover heap
    # memory entered the QR solve: about 4 % of runs of this start ended with every thickness NaN (never
    # with the pure-Python solver, which starts from 0.0). Fixed in tools/port_from_py2.py.
    design = {"substrate": "FusedSilica", "center_wavelength": 1064,
              "layers": _layers(("SiO2", 156.25), ("TiO2", 82.11), ("SiO2", 110.91))}
    targets = engine.build_targets([dict(VCOAT[0], tolerance=0.0001)], pad=True)
    start = engine.design_from_filter(engine.build_filter(design))
    stops = [engine._run("needle", start, targets, {"max_layers": 6, "max_iterations": 50},
                         time.time() + 60, None, info={})[2] for _ in range(100)]
    assert "numerical_error" not in stops


def test_needles_left_at_zero_are_restarted():
    # S6.8 task 7: the needle method inserted needles that stayed at 0 nm and OpenFilters called it converged
    # after one iteration (chi2 11.2, not met, 4 of 6 layers removed). Restarted from 1 nm it converges.
    design = {"substrate": "FusedSilica", "center_wavelength": 1064, "layers": _layers(("SiO2", 243.09), ("TiO2", 225.63))}
    result = engine.optimize(design, VCOAT, "needle", {"max_layers": 6, "time_limit": 60})
    assert result["all_targets_met"]
    assert result["edge_needles_added"] == 0


def test_removed_zero_layers_message_adds_up(monkeypatch):
    # S6.8: "4 layer(s) went to 0 nm and were removed (2 layers at the start, 2 now)" for 6 layers with
    # two at 0 nm whose removal merged two pairs of neighbours.
    stuck = _layers(("SiO2", 90.7), ("TiO2", 0.0), ("SiO2", 272.2), ("TiO2", 179.5), ("SiO2", 0.0), ("TiO2", 59.8))
    monkeypatch.setattr(engine, "_continue_needles", lambda current, chi2, stop, *rest: (current, chi2, stop, 0, 0))
    monkeypatch.setattr(engine, "_run", lambda method, design, *rest, **kw: (dict(design, layers=stuck), 11.2, "converged", 1))
    design = {"substrate": "FusedSilica", "center_wavelength": 1064, "layers": _layers(("SiO2", 243.09), ("TiO2", 225.63))}
    result = engine.optimize(design, VCOAT, "needle", {"time_limit": 10})
    note = next(w for w in result["warnings"] if "ended at 0 nm" in w)
    assert note.startswith("2 layer(s) ended at 0 nm and were removed, merging the 2 pair(s)")
    assert "6 layers before, 2 now (2 at the start)" in note


def test_invisible_layers_are_judged_on_the_result(monkeypatch):
    # S2.8: a campaign row warned that "layer 1 (Ge) has the same index as the substrate" although layer 1 of
    # the optimized design was ZnS: the check ran on the random start.
    seen = []
    original = engine._invisible_layers
    monkeypatch.setattr(engine, "_invisible_layers", lambda design, w: seen.append(design) or original(design, w))
    design = {"substrate": "BK7", "center_wavelength": 550, "layers": _layers(("TiO2", 60), ("SiO2", 90))}
    targets = [{"kind": "R", "from": 450, "to": 650, "step": 10, "value": 0.01, "inequality": "<="}]
    result = engine.optimize(design, targets, "refine", {"time_limit": 10})
    assert [l["thickness"] for l in seen[-1]["layers"]] == [l["thickness"] for l in result["design"]["layers"]]
    assert not any(l["thickness"] == 60 for l in seen[-1]["layers"])


def test_between_grid_worst_stays_inside_the_target_range():
    # S5.11: "worst 0.111065 at 399.75 nm" for a target from 400 to 900 nm.
    design = {"substrate": "BK7", "layers": _layers(("TiO2", 20))}
    spec = {"kind": "R", "from": 400, "to": 900, "step": 1, "value": 0.001, "inequality": "<="}
    row = {"worst_at_nm": 400.0, "worst_value": 0.1, "max_abs_deviation": 0.099}
    engine._refine_worst(design, spec, row, 0.25)
    assert 400.0 <= row["worst_at_nm"] <= 400.25


def test_cancelled_campaign_ranks_what_finished():
    # S4.11/S5.11: a cancelled campaign never marked duplicates (five copies of one design ranked 1-5) and
    # wrote no summary.csv or project.
    start = {"substrate": "BK7", "center_wavelength": 550,
             "layers": _layers(("TiO2", 15), ("SiO2", 30), ("TiO2", 110), ("SiO2", 90))}
    targets = [{"kind": "R", "from": 400, "to": 900, "step": 1, "value": 0.002, "inequality": "<="}]
    task = {"op": "optimize", "params": {}, "args": {"design": start, "target_specs": targets, "method": "needle",
                                                     "options": {"time_limit": 2, "max_layers": 30}}}
    m = jobs.JobManager(workers=2)
    try:
        job_id = m.submit("campaign", [dict(task, params={"start": i}) for i in range(8)],
                          meta={"targets": targets, "top_n": 5, "tolerance": {"n_trials": 20}})
        end = time.time() + 60
        while len(m._done_indices(job_id)) < 2 and time.time() < end:
            time.sleep(0.1)
        assert m.cancel(job_id)["status"] == "cancelled"
        while not m._load(job_id).get("partial") and time.time() < end:
            time.sleep(0.1)
        status, out = m.status(job_id), m.results(job_id)
        assert status["status"] == "cancelled" and "partial" in status["note"]
        assert os.path.exists(status["outputs"]["summary_csv"]) and os.path.exists(status["outputs"]["project"])
        assert out["summary"]["partial"].startswith("the campaign was cancelled")
        assert "yield stage did not run" in out["summary"]["partial"]
        # Identical starts cut by a 2 s time limit may stop at different designs (CPU speed), so check the rule:
        # ranked rows are distinct designs, and every other finished row names the ranked design it copies.
        design = {r["task"]: m.result(job_id, r["task"])["result"]["design"] for r in out["rows"]}
        ranked = [r for r in out["rows"] if "rank" in r]
        assert len(out["rows"]) >= 2 and ranked
        assert not any(jobs.same_design(design[a["task"]], design[b["task"]], a["merit_chi2"], b["merit_chi2"])
                       for i, a in enumerate(ranked) for b in ranked[i + 1:])
        ranked_tasks = {r["task"] for r in ranked}
        assert all(r.get("duplicate_of") in ranked_tasks for r in out["rows"] if "rank" not in r)
    finally:
        m.shutdown()


def test_thermal_names_graded_layers():
    # S6.7: "thermal calculations do not support graded layers", also when no thermal properties were given.
    design = {"substrate": "FusedSilica", "center_wavelength": 550,
              "layers": [{"material": "IdealMixture", "graded": {"steps": [10, 50, 10], "step_thicknesses": [50, 50, 50]}}]}
    with pytest.raises(engine.EngineError, match=r"graded layers: layer 1 \(IdealMixture\).*layer_sensitivity"):
        thermal.sensitivity(design, quantity="R", near=550)


def test_every_out_of_range_material_is_named():
    # S4.7: the refusal named Ge; the Si substrate (data to 5000 nm) came only on the next call.
    with pytest.raises(engine.EngineError, match=r"Ge \(data .*\); Si \(data 206.6-5000 nm"):
        engine.spectra({"substrate": "Si", "layers": _layers(("Ge", 500))}, ["R"], [8000, 12000])


def test_near_peak_says_when_it_is_not_the_highest():
    # S6.3: near 650 gave the local peak at 663.5 nm while the highest value was at 633.5 nm.
    x = np.linspace(0, 100, 1001)
    values = np.exp(-((x - 30) / 3) ** 2) + 0.5 * np.exp(-((x - 70) / 3) ** 2)
    out = engine.half_max_summary(x, values, {"near": 75})
    assert abs(out["peak_nm"] - 70) < 0.2 and "highest value in the range is 1" in out["peak_note"]
    assert "peak_note" not in engine.half_max_summary(x, values, {"near": 25})


def test_target_legend_names_what_is_drawn():
    # S6.8: "grey bars: targets" on a plot whose only target is a single-wavelength marker.
    assert plotting._target_note(VCOAT) == "▲ ≥ / ▼ ≤ targets"
    assert plotting._target_note([{"kind": "R", "from": 1, "to": 2, "value": 0.1}]).startswith("grey bars")
    assert plotting._target_note([]) is None


@pytest.mark.parametrize("backside", [False, True])
@pytest.mark.parametrize("method", ["refine", "needle"])
def test_absorptance_targets_can_be_optimized(backside, method):
    # Found by the plot-label audit: every optimization with an A target failed in OpenFilters itself
    # ("first argument must be dR"; pure Python "'list' object has no attribute 'data'"): without the
    # backside the sample's dR/dT were the per-parameter lists instead of one parameter's derivative.
    design = {"substrate": "BK7", "consider_backside": backside,
              "layers": _layers(("Ag", 10), ("SiO2", 50))}
    result = engine.optimize(design, [{"kind": "A", "wavelength": 550, "value": 0.06, "tolerance": 0.001}], method,
                             {"time_limit": 20})
    assert result["stop_reason"] not in ("openfilters_error", "numerical_error")
    A = engine.spectra(result["design"], ["A"], [550])["series"][0]["values"][0]
    assert result["all_targets_met"] and abs(A - 0.06) < 1e-3


@pytest.mark.parametrize("layers", [
    [("SiO2", 299.63), ("TiO2", 208.85)],
    [("SiO2", 203.38), ("TiO2", 89.97), ("SiO2", 98.15)],
    [("SiO2", 243.09), ("TiO2", 225.63)],
])
def test_stuck_needles_are_caught_without_the_merit_window(monkeypatch, layers):
    # CI 2026-11-06: on Linux and macOS the crawl gained a little over 1 % per 10000 iterations, so the merit
    # window never fired (31-60 s instead of 0.4 s). A needle under 1 nm for 2000 iterations now stops the run.
    monkeypatch.setattr(engine, "STALL_GAIN", 0.0)  # the window never fires, as there
    design = {"substrate": "FusedSilica", "center_wavelength": 1064, "layers": _layers(*layers)}
    start = time.time()
    result = engine.optimize(design, [dict(VCOAT[0], tolerance=0.0001)], "needle", {"max_layers": 6, "time_limit": 60})
    # 0.2-2 s here, about 18 s on GitHub's Linux runners: bounded well below the 60 s limit (see test_release_run).
    assert time.time() - start < 45 and result["all_targets_met"] and result["stop_reason"] != "time_limit", (
        f"{time.time() - start:.1f} s, stop {result['stop_reason']}, {result['iterations']} iterations, met "
        f"{result['all_targets_met']}, chi2 {result['merit_chi2']:.3g}, layers "
        f"{[round(l['thickness'], 3) for l in result['design']['layers']]}, warnings {result.get('warnings')}")


def test_lifted_needles_are_refined_before_needles_are_added(monkeypatch):
    # CI Linux 2026-11-06: after a stall, the restart with the needles lifted to 1 nm went straight to the needle
    # method, whose first step there added two needles: 5 + 2 > max_layers 6, stop at 0 iterations, the lifted
    # needles never refined, and the result collapsed to one SiO2 layer (chi2 1.1e5). Simulated here: a needle
    # run on a design with lifted (1 nm) layers stops at once, as it did on Linux.
    real_run = engine._run

    def linux_like(method, design, targets, options, *rest, **kwargs):
        if method == "needle" and any(l["thickness"] == engine.STALLED_LAYER_NM for l in design["layers"]):
            return design, float(engine._evaluator(engine.build_filter(design), targets).get_chi_2()), "max_layers", 0
        return real_run(method, design, targets, options, *rest, **kwargs)

    monkeypatch.setattr(engine, "_run", linux_like)
    targets = engine.build_targets([dict(VCOAT[0], tolerance=0.0001)], pad=True)
    stuck = {"substrate": "FusedSilica", "center_wavelength": 1064,
             "layers": _layers(("SiO2", 200), ("TiO2", 0.0), ("SiO2", 900), ("TiO2", 0.0), ("SiO2", 351.186))}
    chi2 = float(engine._evaluator(engine.build_filter(stuck), targets).get_chi_2())
    design, new_chi2, _, _, _ = engine._continue_needles(stuck, chi2, "stalled", {}, targets, {"max_layers": 6},
                                                         time.time() + 60, None)
    assert new_chi2 < 1e-6 * chi2 and len(design["layers"]) > 1
