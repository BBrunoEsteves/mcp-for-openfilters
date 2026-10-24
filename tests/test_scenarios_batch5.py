"""Regression tests from scenario Batch 5 (recheck, 2026-10-24).

The batch ran against a server started before the Batch 4 fixes (Claude Desktop was not restarted), so its
Batch 4 failures are covered by test_scenarios_batch4.py; these are the new findings.
"""

import pytest

from openfilters_mcp import engine, jobs

DISPERSIVE = {"substrate": "FusedSilica", "center_wavelength": 800,
              "formula": {"formula": "(HL)^10 H", "materials": {"H": "TiO2", "L": "SiO2"}}}


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFILTERS_MCP_HOME", str(tmp_path))
    engine.catalog(reload=True)


def test_a_design_on_its_limit_within_round_off_is_met():
    # S5.6 / S5.12: merit 8e-14, worst R = 0.5 % + 2.9e-9, reported as not met.
    targets = [{"kind": "R", "from": 450, "to": 650, "step": 5, "value": 0.005, "inequality": "<="}]
    start = {"substrate": "BK7", "center_wavelength": 550,
             "layers": [{"material": "SiO2TiO2", "thickness": 500, "index": 1.9}]}
    r = engine.optimize(start, targets, "step")
    assert r["targets"][0]["worst_value"] == pytest.approx(0.005, abs=1e-8)
    assert r["all_targets_met"] and r["targets"][0]["pass_fraction"] == 1


def test_gdd_inequality_is_not_met_by_any_value():
    # The old slack (1e-9 * max(|goal|, 1)) was compared in s^2, where 1e-9 dwarfs every GDD.
    wl = {"from": 760, "to": 840, "step": 5}
    gdd = engine.spectra(DISPERSIVE, ["R_GDD"], wl, polarizations=["s"])["series"][0]["values"]
    assert max(gdd) > 3  # about +4 fs^2 at 760 nm
    targets = [{"kind": "R_GDD", "polarization": "s", **wl, "value": -100, "tolerance": 5, "inequality": "<="}]
    r = engine.evaluate(DISPERSIVE, targets)
    assert not r["all_targets_met"] and r["targets"][0]["pass_fraction"] == 0


def test_gdd_band_ends_are_not_copies_of_their_neighbours():
    # S5.14: GDD at 750 nm equalled 752 nm (OpenFilters reuses the neighbour's derivative at the ends).
    coarse = engine.spectra(DISPERSIVE, ["R_GDD"], {"from": 750, "to": 850, "step": 2},
                            polarizations=["s"])["series"][0]["values"]
    wide = engine.spectra(DISPERSIVE, ["R_GDD"], {"from": 740, "to": 860, "step": 2},
                          polarizations=["s"])["series"][0]["values"]
    assert coarse[0] != pytest.approx(coarse[1], rel=1e-3) and coarse[-1] != pytest.approx(coarse[-2], rel=1e-3)
    assert list(coarse) == pytest.approx(list(wide[5:-5]), rel=1e-9)  # the same as interior points


def test_duplicates_beyond_the_top_are_marked():
    # S5.12: tasks 3 and 11 (1 layer, 239.295 vs 239.296 nm, same merit) had neither rank nor duplicate_of.
    def design(*thicknesses):
        return {"layers": [{"material": "SiO2", "thickness": t} for t in thicknesses]}
    designs = {0: design(100.0), 1: design(150.0), 2: design(239.295), 3: design(239.296)}

    class Manager:
        def result(self, job_id, index):
            return {"result": {"design": designs[index]}}

    rows = [{"task": i, "merit_chi2": m} for i, m in zip(range(4), (0.0, 1.0, 11.2242684, 11.2242684))]
    top = jobs.distinct_top(Manager(), {"id": "x"}, rows, 1)
    assert [r["task"] for r in top] == [0]
    assert rows[3]["duplicate_of"] == 2 and "duplicate_of" not in rows[1] and "duplicate_of" not in rows[2]


def test_a_start_that_collapses_to_the_bare_substrate_is_explained():
    # S5.12 task 0: openfilters_error with both layers at 0 nm, and no error_detail in the campaign row.
    targets = [{"kind": "R", "wavelength": 1064, "value": 0.0005, "inequality": "<="}]
    start = {"substrate": "FusedSilica", "center_wavelength": 1064,
             "layers": [{"material": "TiO2", "thickness": 65.91}, {"material": "SiO2", "thickness": 109.18}]}
    r = engine.optimize(start, targets, "needle", {"max_layers": 6})
    # C++ core: "length must be positive"; pure Python: IndexError (both OpenFilters' own checks)
    assert r["stop_reason"] == "openfilters_error" and r["error_detail"]
    assert any("every layer went to 0 nm" in w for w in r["warnings"])
    row = jobs.result_row(0, "optimize", {"ok": True, "params": {}, "result": r})
    assert row["error_detail"] == r["error_detail"]


def test_sub_nanometre_layers_are_flagged_without_min_thickness():
    # S5.14: a 0.075 nm outer layer went unmentioned.
    targets = [{"kind": "R", "from": 500, "to": 600, "step": 10, "value": 0.01, "inequality": "<="}]
    start = {"substrate": "BK7", "center_wavelength": 550,
             "layers": [{"material": "TiO2", "thickness": 0.3}, {"material": "SiO2", "thickness": 94.0}]}
    r = engine.optimize(start, targets, "refine", {"max_iterations": 1})
    if r["thinnest_layer_nm"] < 1:
        assert any("below what can be deposited" in w for w in r["warnings"])
    r = engine.optimize(start, targets, "refine", {"min_thickness": 5})
    assert not any("below what can be deposited" in w for w in r.get("warnings", []))


def test_between_grid_worst_finds_the_bottom_of_a_narrow_notch():
    # S5.10 rerun: the warning gave 57.9 % at 687.5 nm (the 4x grid point); the notch bottom was 45.05 %.
    design = {"substrate": "BK7", "center_wavelength": 532,
              "formula": {"formula": "(HL)^4 HH (LH)^4", "materials": {"H": "TiO2", "L": "SiO2"}}}
    targets = [{"kind": "T", "from": 500, "to": 560, "step": 5, "value": 0.05, "inequality": "<="}]
    (warning,) = engine.evaluate(design, targets)["warnings"]
    fine = engine.spectra(design, ["T"], {"from": 528, "to": 536, "step": 0.01})["series"][0]["values"]
    assert f"worst {engine.sig(max(fine))} at 532.0 nm" in warning


def test_layers_dropped_at_zero_thickness_are_reported(monkeypatch):
    # S5.12 rerun: 2-4 layer starts ended as one layer with no note.
    targets = [{"kind": "R", "wavelength": 1064, "value": 0.0005, "inequality": "<="}]
    design = {"substrate": "FusedSilica", "center_wavelength": 1064,
              "layers": [{"material": "TiO2", "thickness": 20.0}, {"material": "SiO2", "thickness": 245.0},
                         {"material": "TiO2", "thickness": 35.0}]}
    ended = dict(design, layers=[dict(design["layers"][0], thickness=0.0)] + design["layers"][1:])
    monkeypatch.setattr(engine, "_run", lambda *args, **kwargs: (ended, 1.0, "converged", 3))
    r = engine.optimize(design, targets, "refine")
    assert r["nb_layers"] == 2
    assert any("1 layer(s) went to 0 nm and were removed (3 layers at the start, 2 now)" in w for w in r["warnings"])
