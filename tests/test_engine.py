"""Tests of the engine layer used by the MCP tools."""

import numpy as np
import pytest

from mcp_for_openfilters import engine
from reference_tmm import rt

MIRROR = {"substrate": "FusedSilica", "center_wavelength": 1064,
          "formula": {"formula": "(HL)^8 H", "materials": {"H": "TiO2", "L": "SiO2"}}}
TARGETS = [{"kind": "R", "from": 1000, "to": 1130, "step": 5, "value": 0.995, "tolerance": 0.005, "inequality": ">="},
           {"kind": "T", "from": 600, "to": 700, "step": 5, "value": 0.9, "tolerance": 0.02, "inequality": ">="}]


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path))
    engine.catalog(reload=True)


def test_formula_expands_to_quarter_waves():
    d = engine.describe(MIRROR)
    assert d["nb_layers"] == 17
    assert all(row["qwot@cw"] == pytest.approx(1.0, abs=1e-3) for row in d["layers"])


def test_design_round_trip():
    design = engine.describe(MIRROR)["design"]
    again = engine.describe(design)["design"]
    assert again == design


def test_spectra_match_reference():
    wl = np.arange(800.0, 1300.0, 5.0)
    layers = engine.describe(MIRROR)["design"]["layers"]
    stack = [(engine.material_N(l["material"], wl), l["thickness"]) for l in reversed(layers)]
    R_ref, T_ref, _ = rt(wl, 1.0, stack, engine.material_N("FusedSilica", wl), 30.0, "p")
    s = engine.spectra(MIRROR, ["R", "T"], list(wl), [30.0], ["p"])
    np.testing.assert_allclose(s["series"][0]["values"], R_ref, atol=1e-9)
    np.testing.assert_allclose(s["series"][1]["values"], T_ref, atol=1e-9)


def test_electric_field_interior_matches_reference():
    wl = 1064.0
    layers = engine.describe(MIRROR)["design"]["layers"]
    stack = [(engine.material_N(l["material"], [wl])[0], l["thickness"]) for l in reversed(layers)]
    _, _, r = rt(np.array([wl]), 1.0, stack, engine.material_N("FusedSilica", [wl])[0], 0.0, "s")
    E, H = 1 + r[0], 1 - r[0]
    tops = []
    for N, t in stack:
        delta = 2 * np.pi * N * t / wl
        E, H = np.cos(delta) * E - 1j * np.sin(delta) / N * H, -1j * N * np.sin(delta) * E + np.cos(delta) * H
        tops.append(abs(E) ** 2)
    field = engine.electric_field(MIRROR, wl, 0.0, "s")
    pos = np.array(field["position_nm"])
    of_tops = [field["E_squared"][int(np.argmin(abs(pos - b)))] for b in field["layer_boundaries_nm"][::-1][1:]]
    np.testing.assert_allclose(of_tops, tops[:-1], atol=1e-6)


def test_monitoring_end_matches_spectrum():
    d = {"substrate": "BK7", "layers": [["TiO2", 60], ["SiO2", 95], ["TiO2", 60]]}
    m = engine.monitoring(d, [550, 633, 800], mode="reflection")
    s = engine.spectra(dict(d, consider_backside=True), ["R"], [550, 633, 800])
    np.testing.assert_allclose([v[-1] for v in m["signal"].values()], s["series"][0]["values"], atol=1e-9)


def test_target_validation():
    with pytest.raises(engine.EngineError):
        engine.build_targets([{"kind": "R", "wavelength": 550, "value": 99}])  # percent instead of fraction
    with pytest.raises(engine.EngineError):
        engine.build_targets([{"kind": "R_GD", "wavelength": 800, "value": 10}])  # needs s or p


def test_optimize_meets_targets_and_tolerance_is_reproducible():
    r = engine.optimize(MIRROR, TARGETS, "needle", {"time_limit": 60})
    assert r["all_targets_met"]
    a = engine.tolerance(r["design"], TARGETS, n_trials=20, seed=3)
    b = engine.tolerance(r["design"], TARGETS, n_trials=20, seed=3)
    assert a["yield"] == b["yield"] and 0 <= a["yield"] <= 1


def test_index_error_changes_spectrum():
    design = engine.describe(MIRROR)["design"]
    f = engine.build_filter(design, [1064.0])
    nominal = f.reflection(0.0, engine.S)[0]
    f2 = engine.build_filter(design, [1300.0])
    engine._scale_indices(f2, {"TiO2": 1.05})
    f3 = engine.build_filter(design, [1300.0])
    assert f2.reflection(0.0, engine.S)[0] != pytest.approx(f3.reflection(0.0, engine.S)[0])
    assert nominal > 0.99


def test_user_material_and_project_round_trip(tmp_path):
    engine.add_material("TestHfO2", "cauchy", coefficients=[1.875, 0.0123, 0.0])
    engine.add_material("TestTable", "table", table=[[400, 2.0, 0.01], [800, 1.9, 0.0]])
    assert engine.material_N("TestTable", [600])[0].real == pytest.approx(1.95, abs=0.01)
    design = {"substrate": "BK7", "layers": [{"material": "TestHfO2", "thickness": 100}, {"material": "SiO2", "qwot": 1}]}
    path = engine.save_project(str(tmp_path / "p.ofp"), [design], TARGETS)
    loaded = engine.load_project(path)
    assert loaded["designs"][0]["layers"][0]["material"] == "TestHfO2"
    assert len(loaded["targets"]) == 2
    assert engine.evaluate(loaded["designs"][0], loaded["targets"])["merit_chi2"] == \
        pytest.approx(engine.evaluate(design, TARGETS)["merit_chi2"])


def test_needle_derivatives_match_finite_differences():
    """OpenFilters' needle merit derivative (dMF = 0.5 dchi2/dd) against
    physically inserting a 0.01 nm layer."""
    from openfilters_core.optimization_needles import optimization_needles
    start = {"substrate": "BK7", "layers": [{"material": m, "thickness": t} for m, t in [("TiO2", 80), ("SiO2", 120)] * 3]}
    targets = [{"kind": "R", "from": 520, "to": 545, "step": 2.5, "value": 0.99, "tolerance": 0.01},
               {"kind": "T", "from": 600, "to": 680, "step": 5, "value": 0.9, "tolerance": 0.02}]
    o = optimization_needles(engine.build_filter(start), engine.build_targets(targets))
    o.calculate_needles()
    base = engine.evaluate(start, targets)["merit_chi2"]
    materials = o.get_needle_materials()
    h = 0.01
    for i_parameter, (_, layer) in enumerate(o.parameters):
        host = start["layers"][layer]
        positions = o.dMF_positions[i_parameter]
        for i_needle in (1, len(positions) // 2):
            for i_material, material in enumerate(materials):
                if material == host["material"]:
                    continue
                cut = positions[i_needle]  # measured from the substrate side of the layer
                layers = start["layers"][:layer] + [dict(host, thickness=cut), {"material": material, "thickness": h},
                                                     dict(host, thickness=host["thickness"] - cut)] + start["layers"][layer + 1:]
                finite = (engine.evaluate(dict(start, layers=layers), targets)["merit_chi2"] - base) / h
                analytic = 2 * o.dMF[i_material][i_parameter][i_needle]
                assert analytic == pytest.approx(finite, rel=0.02, abs=5.0)


AR = {"center_wavelength": 550, "formula": {"formula": "0.25H 0.25L H L", "materials": {"H": "TiO2", "L": "SiO2"}},
      "substrate": "BK7"}
AR_TARGETS = [{"kind": "R", "from": 450, "to": 650, "step": 5, "value": 0, "tolerance": 0.005}]


@pytest.mark.parametrize("min_thickness", [10.0, 20.0])
def test_min_thickness_is_enforced_quickly(min_thickness):
    """Regression: OpenFilters' needle loop with a minimum thickness cycled
    (add needle / remove thin layers) until the time limit."""
    import time
    t0 = time.time()
    r = engine.optimize(AR, AR_TARGETS, "needle", {"min_thickness": min_thickness, "time_limit": 120})
    assert r["stop_reason"] != "cycling"
    from openfilters_core import moremath
    if moremath.get_moremath_dll_import_success():
        # With the compiled solver it converges in seconds; the pure-Python one is slow enough
        # that the edge-needle rounds can use the whole time limit (still improving).
        assert r["stop_reason"] != "time_limit"
        assert time.time() - t0 < 100
    assert r["thinnest_layer_nm"] >= min_thickness - 1e-6
    assert r["merit_chi2"] == pytest.approx(engine.evaluate(r["design"], AR_TARGETS)["merit_chi2"], rel=1e-9)
    if min_thickness == 10.0:
        assert r["all_targets_met"]


def test_cycle_detection_stops_and_keeps_best_design():
    import time
    from openfilters_core.optimization_needles import optimization_needles
    targets = engine.build_targets(AR_TARGETS)
    o = optimization_needles(engine.build_filter(AR), targets)
    o.set_min_thickness(10.0)  # OpenFilters' own removal, which cycles on this case
    o.set_automatic_mode(True)
    monitor = engine._Monitor(o, time.time() + 60, None, None, None)
    o.set_parent(monitor)
    t0 = time.time()
    o.go()
    # Stopped by the cycle detector well before the 60 s deadline (the pure-
    # Python core on a CI machine can take ~20 s to go round three cycles).
    assert monitor.stop_reason == "cycling"
    assert time.time() - t0 < 50
    best = dict(engine.describe(AR)["design"], layers=monitor.best_layers)
    assert engine.evaluate(best, AR_TARGETS)["merit_chi2"] < 5.0


# --- two-sided designs -------------------------------------------------------

FRONT = [{"material": "TiO2", "thickness": 90.0}, {"material": "SiO2", "thickness": 140.0},
         {"material": "TiO2", "thickness": 60.0}]
BACK = [{"material": "SiO2", "thickness": 120.0}, {"material": "TiO2", "thickness": 75.0}]
WL = {"from": 400, "to": 1000, "step": 5}


def two_sided(**extra):
    # Substrate "L" (n = 1.5, k = 0) so that no substrate absorption enters the comparison.
    return {"substrate": "L", "substrate_thickness_mm": 1, "consider_backside": True,
            "layers": FRONT, "back_layers": BACK, **extra}


def test_two_sided_spectrum_matches_incoherent_combination():
    """T = T1 T2 / (1 - R1' R2') with R' seen from inside the substrate."""
    one_sided = lambda layers: {"substrate": "L", "layers": layers}
    front, back = one_sided(FRONT), one_sided(BACK)
    T1 = engine.spectra(front, ["T"], WL)["series"][0]["values"]
    R1r = engine.spectra(front, ["R"], WL, reverse=True)["series"][0]["values"]
    T2 = engine.spectra(back, ["T"], WL)["series"][0]["values"]
    R2r = engine.spectra(back, ["R"], WL, reverse=True)["series"][0]["values"]
    expected = T1 * T2 / (1 - R1r * R2r)
    T = engine.spectra(two_sided(), ["T"], WL)["series"][0]["values"]
    np.testing.assert_allclose(T, expected, rtol=1e-9)


def test_od_quantity():
    s = engine.spectra(two_sided(), ["T", "OD"], WL)
    np.testing.assert_allclose(s["series"][1]["values"], -np.log10(s["series"][0]["values"]), rtol=1e-12)


def test_describe_reports_back_face_and_warns_when_ignored():
    d = engine.describe(two_sided())
    assert d["nb_back_layers"] == 2 and d["consider_backside"] and "warning" not in d
    d = engine.describe(two_sided(consider_backside=False))
    assert "warning" in d


def test_two_sided_project_round_trip(tmp_path):
    path = engine.save_project(str(tmp_path / "two_sided.ofp"), [two_sided()], AR_TARGETS)
    text = open(path, encoding="latin-1").read()
    assert text.count("BackLayer:") == 2 and "ConsiderBackside: 1" in text
    loaded = engine.load_project(path)
    assert len(loaded["designs"]) == 1
    design = loaded["designs"][0]
    assert design["consider_backside"] and len(design["back_layers"]) == 2


def test_optimizer_keeps_back_layers_and_counts_them():
    targets = [{"kind": "T", "from": 500, "to": 600, "step": 10, "value": 0.98, "tolerance": 0.01, "inequality": ">="}]
    r = engine.optimize(two_sided(), targets, "refine", {"time_limit": 30})
    back = r["design"]["back_layers"]
    assert [l["thickness"] for l in back] == pytest.approx([l["thickness"] for l in BACK])
    with_back = engine.evaluate(r["design"], targets)["merit_chi2"]
    without_back = engine.evaluate(dict(r["design"], consider_backside=False), targets)["merit_chi2"]
    assert with_back != pytest.approx(without_back)


def test_tolerance_perturbs_back_face():
    targets = [{"kind": "T", "from": 500, "to": 600, "step": 10, "value": 0.9, "tolerance": 0.01}]
    design = dict(two_sided(), layers=[])  # only the back face is coated
    r = engine.tolerance(design, targets, n_trials=10, thickness_error={"type": "relative", "sigma": 0.05},
                         keep_values=True)
    assert np.std(r["chi2_values"]) > 0


def test_sensitivity_includes_back_face():
    targets = [{"kind": "T", "from": 500, "to": 600, "step": 10, "value": 0.9, "tolerance": 0.01}]
    rows = engine.sensitivity(two_sided(), targets)["layers"]
    assert {r["side"] for r in rows} == {"front", "back"} and len(rows) == len(FRONT) + len(BACK)


def test_bare_front_face_can_be_evaluated():
    targets = [{"kind": "R", "wavelength": 550, "value": 0.0, "tolerance": 0.01}]
    bare = {"substrate": "L", "layers": []}
    R = engine.spectra(bare, ["R"], [550])["series"][0]["values"][0]
    assert R == pytest.approx(0.04, abs=1e-6)  # ((1.5 - 1) / (1.5 + 1))^2
    assert engine.evaluate(bare, targets)["merit_chi2"] == pytest.approx((R / 0.01) ** 2, rel=1e-9)
    with pytest.raises(engine.EngineError):
        engine.optimize(bare, targets, "needle")


def test_catalog_sees_materials_written_by_other_processes():
    """Regression: each worker kept the catalog it loaded first, so materials
    added (or overwritten) by another process were missing or stale."""
    import os
    from mcp_for_openfilters import settings
    engine.catalog()  # cached in this process
    path = os.path.join(settings.materials_dir(), "ExternalMat.mat")
    with open(path, "w", encoding="latin-1") as f:  # written behind the cache's back
        f.write("Description: test\nKind: regular\nModel: constant\nProperties: 1.7\n")
    assert engine.material_N("ExternalMat", [550])[0].real == pytest.approx(1.7)
    with open(path, "w", encoding="latin-1") as f:  # overwritten with a different index
        f.write("Description: test, longer description\nKind: regular\nModel: constant\nProperties: 2.1\n")
    assert engine.material_N("ExternalMat", [550])[0].real == pytest.approx(2.1)


def test_project_keeps_settings_for_the_gui(tmp_path):
    """What the OpenFilters GUI needs to continue the work: targets, needle
    materials, step spacing, descriptions, layer flags, a useful wavelength range."""
    design = {"substrate": "BK7", "description": "made by a test", "needle_materials": ["TiO2", "SiO2"],
              "step_spacing": 0.02,
              "layers": [{"material": "TiO2", "thickness": 100, "refine_thickness": False},
                         {"material": "SiO2", "thickness": 150, "add_needles": False}]}
    path = engine.save_project(str(tmp_path / "gui.ofp"), [design], TARGETS)
    text = open(path, encoding="latin-1").read()
    assert text.count("Target:") == 2
    assert "Description: made by a test" in text and "NeedleMaterials: TiO2 SiO2" in text
    assert "StepSpacing: 0.020000" in text and "MinimumThickness: 0.000000" in text
    loaded = engine.load_project(path)["designs"][0]
    assert loaded["description"] == "made by a test" and loaded["needle_materials"] == ["TiO2", "SiO2"]
    assert loaded["step_spacing"] == pytest.approx(0.02)
    r = loaded["wavelength_range"]
    assert r["from"] <= 600 and r["to"] >= 1130 and (r["to"] - r["from"]) / r["step"] <= 2000
    assert loaded["layers"][0] == {"material": "TiO2", "thickness": 100.0, "refine_thickness": False,
                                  "add_needles": False}  # OpenFilters: no needles in a fixed layer
    assert loaded["layers"][1] == {"material": "SiO2", "thickness": 150.0, "add_needles": False}


def test_target_wavelength_range():
    assert engine.target_wavelength_range(None) is None
    assert engine.target_wavelength_range([{"kind": "R", "wavelength": 1064, "value": 1}]) == \
        {"from": 1010.0, "to": 1120.0, "step": 0.1}
    color = engine.target_wavelength_range([{"kind": "R_color", "values": [50, 0, 0]}])
    assert color["from"] <= 380 and color["to"] >= 780


def test_optimizer_honours_layer_flags_and_describes_its_result():
    start = {"substrate": "BK7", "layers": [{"material": "TiO2", "thickness": 40, "refine_thickness": False},
                                            {"material": "SiO2", "thickness": 60}]}
    targets = [{"kind": "R", "from": 500, "to": 600, "step": 10, "value": 0.0, "tolerance": 0.01}]
    r = engine.optimize(start, targets, "refine", {"time_limit": 30, "min_thickness": 5})
    layers = r["design"]["layers"]
    assert layers[0] == {"material": "TiO2", "thickness": 40.0, "refine_thickness": False, "add_needles": False}
    assert layers[1]["thickness"] != pytest.approx(60.0)
    description = r["design"]["description"]
    assert "refine method" in description and "minimum layer thickness 5 nm" in description
    assert f"chi2 {r['initial_chi2']:.6g} -> {r['merit_chi2']:.6g}" in description


def test_needle_result_keeps_needle_materials():
    start = {"substrate": "BK7", "layers": [{"material": "TiO2", "thickness": 80}, {"material": "SiO2", "thickness": 120}]}
    targets = [{"kind": "R", "from": 500, "to": 600, "step": 10, "value": 0.0, "tolerance": 0.01}]
    r = engine.optimize(start, targets, "needle", {"time_limit": 30, "max_iterations": 5,
                                                   "needle_materials": ["SiO2", "TiO2"]})
    assert sorted(r["design"]["needle_materials"]) == ["SiO2", "TiO2"]
    assert "needle method" in r["design"]["description"]


def test_user_material_overrides_built_in_one_of_the_same_name():
    # OpenFilters ships TiO2; a user's own TiO2 must be the one used everywhere.
    built_in = engine.material_N("TiO2", [550.0])[0]
    engine.add_material("TiO2", "constant", n=2.0)
    assert engine.material_N("TiO2", [550.0])[0] == pytest.approx(2.0) != built_in
    [row] = [r for r in engine.list_materials("TiO2") if r["name"] == "TiO2"]
    assert row["source"] == "user" and row["n,k"]["550"][0] == pytest.approx(2.0)
    d = {"substrate": "FusedSilica", "center_wavelength": 550, "layers": [{"material": "TiO2", "qwot": 1}]}
    assert engine.describe(d)["layers"][0]["n@cw"] == pytest.approx(2.0)


def test_remove_material_restores_the_built_in_one():
    built_in = engine.material_N("TiO2", [550.0])[0]
    engine.add_material("TiO2", "constant", n=2.0)
    out = engine.remove_material("TiO2")
    assert "built-in" in out["note"]
    assert engine.material_N("TiO2", [550.0])[0] == pytest.approx(built_in)
    with pytest.raises(engine.EngineError, match="built-in"):
        engine.remove_material("TiO2")  # only the user's copy can be removed
    with pytest.raises(engine.EngineError):
        engine.remove_material("../evil")


def test_worst_value_and_wavelength_per_target():
    d = {"substrate": "BK7", "center_wavelength": 550,
         "layers": [{"material": "TiO2", "thickness": 20}, {"material": "SiO2", "thickness": 100}]}
    targets = [{"kind": "R", "from": 450, "to": 650, "step": 5, "value": 0.005, "inequality": "<="},
               {"kind": "T", "wavelength": 550, "value": 0.99, "inequality": ">="}]
    rows = engine.evaluate(d, targets)["targets"]
    r = engine.spectra(d, ["R"], list(range(450, 651, 5)))["series"][0]["values"]
    assert rows[0]["worst_value"] == pytest.approx(max(r), rel=1e-5)
    assert rows[0]["worst_at_nm"] == 450 + 5 * int(np.argmax(r))
    assert rows[1]["worst_at_nm"] == 550


@pytest.mark.parametrize("space", ["Lab", "L*a*b*", "XYZ", "LCh"])
def test_color_targets_evaluate_and_round_trip(space, tmp_path):
    d = {"substrate": "BK7", "layers": [{"material": "TiO2", "thickness": 120}]}
    target = {"kind": "R_color", "color_space": space, "values": [30, 10, -20], "tolerances": [1, 1, 1]}
    result = engine.evaluate(d, [target])
    row = result["targets"][0]
    assert row["points"] == 3 and "worst_value" not in row
    lab = engine.color(d, [0.0])["colors"][0]["Lab"]
    if space in ("Lab", "L*a*b*"):  # the distance is ΔE*ab against compute_color's coordinates
        assert row["calculated"] == pytest.approx(lab, rel=1e-5)
        assert row["distance"] == pytest.approx(np.linalg.norm(np.array(lab) - [30, 10, -20]), rel=1e-5)
    path = str(tmp_path / "color.ofp")
    engine.save_project(path, [d], [target])
    loaded = engine.load_project(path)["targets"][0]
    assert loaded["kind"] == "R_color" and loaded["values"] == pytest.approx([30, 10, -20])


def test_unknown_material_error_suggests_refractiveindex():
    with pytest.raises(engine.EngineError, match="search_refractiveindex"):
        engine.spectra({"substrate": "BK7", "layers": [{"material": "Unobtainium", "thickness": 10}]}, ["R"], [550])


def _qw_mirror():
    for name, n in (("C_S", 1.45), ("C_H", 2.3), ("C_L", 1.45)):
        engine.add_material(name, "constant", n=n)
    return {"substrate": "C_S", "center_wavelength": 800,
            "formula": {"formula": "(HL)^10 H", "materials": {"H": "C_H", "L": "C_L"}}}


def test_group_delay_and_gdd_are_in_fs_like_the_openfilters_gui():
    # OpenFilters computes them in s and s^2; GD/GDD were reported (and targeted) in those units as "fs".
    d = _qw_mirror()
    w = np.arange(700.0, 900.01, 0.5)
    gd, gdd = (s["values"] for s in engine.spectra(d, ["R_GD", "R_GDD"], list(w), [0.0], ["s"])["series"])
    layers = engine.describe(d)["layers"]
    stack = [(complex(2.3 if l["material"] == "C_H" else 1.45), l["thickness_nm"]) for l in reversed(layers)]
    _, _, r = rt(w, 1.0, stack, 1.45, 0.0, "s")
    omega = 2 * np.pi * 299.792458 / w  # rad/fs
    gd_ref = -np.gradient(np.unwrap(np.angle(r)), omega)  # OpenFilters' phase convention
    gdd_ref = np.gradient(gd_ref, omega)
    i = int(np.argmin(abs(w - 750)))
    assert abs(gd[i]) == pytest.approx(abs(gd_ref[i]), rel=1e-2)
    assert abs(gdd[i]) == pytest.approx(abs(gdd_ref[i]), rel=1e-2) and abs(gdd[i]) > 1


def test_gdd_target_optimization_moves_and_round_trips(tmp_path):
    d = _qw_mirror()
    targets = [{"kind": "R_GDD", "polarization": "s", "from": 760, "to": 840, "step": 10, "value": -20, "tolerance": 5},
               {"kind": "R", "polarization": "s", "from": 760, "to": 840, "step": 10, "value": 0.99, "inequality": ">="}]
    result = engine.optimize(d, targets, "refine", {"time_limit": 30})
    assert result["merit_chi2"] < 0.5 * result["initial_chi2"]
    path = str(tmp_path / "gdd.ofp")
    engine.save_project(path, [d], targets)
    loaded = engine.load_project(path)["targets"][0]
    assert loaded["points"][0][1] == pytest.approx(-20) and loaded["points"][0][2] == pytest.approx(5)


def test_coarse_gdd_grid_is_warned():
    d = _qw_mirror()
    coarse = engine.spectra(d, ["R_GDD"], [760, 800, 840], [0.0], ["s"])
    assert any("numerical derivatives" in w for w in coarse["warnings"])
    fine = engine.spectra(d, ["R_GDD"], {"from": 760, "to": 840, "step": 1}, [0.0], ["s"])
    assert not fine["warnings"]
