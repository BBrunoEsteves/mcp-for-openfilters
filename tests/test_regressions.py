"""Regression tests from real sessions (an AR campaign on BK7, 2026-10-23).

The user's TiO2 was a constant n = 2.0; SiO2 and BK7 are the built-in materials.
"""

import math

import pytest

from openfilters_mcp import engine

AR_TARGETS = [{"kind": "R", "angle": angle, "from": 500, "to": 700, "step": 5, "value": 0.002,
               "inequality": "<=", "tolerance": 0.0001} for angle in (0, 35)]


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFILTERS_MCP_HOME", str(tmp_path))
    engine.catalog(reload=True)
    engine.add_material("TiO2", "constant", n=2.0)


def ar_design(thicknesses, first="TiO2"):
    other = {"TiO2": "SiO2", "SiO2": "TiO2"}
    layers, material = [], first
    for t in thicknesses:
        layers.append({"material": material, "thickness": t})
        material = other[material]
    return {"substrate": "BK7", "center_wavelength": 550, "layers": layers}


def test_nan_thickness_is_refused():
    # A NaN layer evaluated as an empty coating (R = 0), so it passed "R <=" targets.
    with pytest.raises(engine.EngineError, match="invalid thickness"):
        engine.evaluate(ar_design([float("nan"), 100.0]), AR_TARGETS)
    with pytest.raises(engine.EngineError, match="invalid thickness"):
        engine.evaluate(ar_design([None, 100.0]), AR_TARGETS)  # NaN arrives as null over JSON


def test_inequality_is_met_only_when_it_holds():
    # R of this two-layer AR is 0.2-0.5 %: within the default tolerance (1 %) of
    # "R <= 0.2 %", but the specification is not met.
    targets = [{"kind": "R", "from": 500, "to": 700, "step": 5, "value": 0.002, "inequality": "<="}]
    result = engine.evaluate(ar_design([20.0, 100.0]), targets)
    assert result["merit_chi2"] > 0
    assert result["all_targets_met"] is False and result["targets"][0]["pass_fraction"] < 1
    loose = [dict(targets[0], value=0.2)]
    assert engine.evaluate(ar_design([20.0, 100.0]), loose)["all_targets_met"] is True


def test_optimizer_never_returns_nan_after_a_numerical_failure():
    # This start made OpenFilters' Levenberg-Marquardt step turn every thickness into
    # NaN after 42 iterations (a layer pinned at 0 nm); the NaN design then ranked first.
    start = ar_design([106.47, 182.94, 46.07, 63.99, 194.96, 104.88, 188.77])
    result = engine.optimize(start, AR_TARGETS[:1] + [dict(AR_TARGETS[1], angle=20), dict(AR_TARGETS[1], angle=35)],
                             "needle", {"time_limit": 60, "max_layers": 20})
    thicknesses = [layer["thickness"] for layer in result["design"]["layers"]]
    assert all(math.isfinite(t) and t > 0 for t in thicknesses)
    assert math.isfinite(result["merit_chi2"]) and result["merit_chi2"] > 0
    assert result["nb_layers"] <= 20


def test_needle_continues_where_openfilters_finds_only_edge_needles():
    # OpenFilters' needle search only accepts minima inside a layer; here the best
    # needle is at the outer surface, so it stopped at 4 layers with chi2 = 58188.
    start = ar_design([27.26, 29.49, 82.03, 100.53])
    result = engine.optimize(start, AR_TARGETS, "needle", {"time_limit": 60, "max_layers": 10})
    assert result["edge_needles_added"] >= 1
    assert 4 < result["nb_layers"] <= 10
    assert result["merit_chi2"] < 0.5 * result["initial_chi2"]


def test_needle_survives_openfilters_removing_every_layer():
    # Campaign start 24 (2026-10-24): both layers shrank to 0 nm, OpenFilters' thin-layer removal left no
    # layer and crashed ("length must be positive" in C++, IndexError in Python), failing the start.
    start = {"substrate": "BK7", "layers": [{"material": "SiO2", "thickness": 32.73},
                                           {"material": "TiO2", "thickness": 33.76}]}
    engine.remove_material("TiO2")  # this case used the built-in TiO2
    targets = [{"kind": "R", "from": 420, "to": 680, "step": 5, "value": 0.005, "inequality": "<="}]
    result = engine.optimize(start, targets, "needle", {"max_layers": 8, "time_limit": 30})
    assert math.isfinite(result["merit_chi2"])
    assert all(math.isfinite(layer["thickness"]) for layer in result["design"]["layers"])


def test_campaign_top_designs_are_distinct():
    from openfilters_mcp import jobs

    def design(*thicknesses):
        return {"layers": [{"material": m, "thickness": t} for m, t in zip(["TiO2", "SiO2"] * 3, thicknesses)]}

    designs = {0: design(15.71, 32.30), 1: design(15.72, 32.31), 2: design(12.3, 32.8, 107.9), 3: design(15.70, 32.29)}

    class Manager:
        def result(self, job_id, index):
            return {"result": {"design": designs[index]}}

    rows = [{"task": i} for i in (0, 1, 2, 3)]
    top = jobs.distinct_top(Manager(), {"id": "x"}, rows, 3)
    assert [r["task"] for r in top] == [0, 2]
    assert rows[1]["duplicate_of"] == 0 and rows[3]["duplicate_of"] == 0


def test_campaign_ranks_by_yield_when_a_yield_stage_ran():
    # Campaign 2026-10-24: a nominal pass with 0 % yield ranked above a 1e-7 miss with 26.5 %.
    from openfilters_mcp import jobs
    rows = [{"task": 0, "all_targets_met": True, "merit_chi2": 0.0, "yield": 0.0},
            {"task": 1, "all_targets_met": False, "merit_chi2": 1e-9, "yield": 0.265},
            {"task": 2, "all_targets_met": False, "merit_chi2": 5.0},  # outside the yield stage
            {"task": 3, "error": "failed"}]
    assert [r["task"] for r in sorted(rows, key=jobs.rank_key)] == [1, 0, 2, 3]
    no_yield = [{"task": 0, "all_targets_met": False, "merit_chi2": 0.1}, {"task": 1, "all_targets_met": True}]
    assert [r["task"] for r in sorted(no_yield, key=jobs.rank_key)] == [1, 0]


def test_tiny_violation_is_not_rounded_to_zero():
    # "not met" with max_abs_deviation 0.0 contradicted itself (a 8e-8 miss rounded to 6 decimals).
    engine.add_material("AT_A", "constant", n=1.38)
    engine.add_material("AT_S", "constant", n=1.52)
    d = {"substrate": "AT_S", "center_wavelength": 550, "layers": [{"material": "AT_A", "qwot": 1}]}
    r = ((1.52 - 1.38 ** 2) / (1.52 + 1.38 ** 2)) ** 2
    row = engine.evaluate(d, [{"kind": "R", "wavelength": 550, "value": r - 8e-8, "inequality": "<="}])["targets"][0]
    assert row["pass_fraction"] == 0 and row["max_abs_deviation"] == pytest.approx(8e-8, rel=1e-3)


def test_every_stop_reason_is_explained():
    # Campaign rows showed "cycling" with no explanation.
    start = ar_design([27.26, 29.49, 82.03, 100.53])
    result = engine.optimize(start, AR_TARGETS, "needle", {"time_limit": 20, "max_layers": 10})
    assert result["stop_explanation"]
    import re
    source = open(engine.__file__, encoding="utf-8").read()
    # openfilters_error is returned directly by _run, not assigned.
    produced = set(re.findall(r'stop(?:_reason)? = "(\w+)"', source)) | {"converged", "finished", "openfilters_error"}
    assert produced <= set(engine.STOP_REASONS), produced - set(engine.STOP_REASONS)


def test_optimizer_uses_the_compiled_solver_when_installed():
    # Port bug: targets.py imports "from .moremath.Levenberg_Marquardt import ...", which loaded the
    # pure-Python file and replaced the compiled solver everywhere (in Python 2 it found the compiled one).
    from openfilters_core import moremath, optimization_needles, optimization_refinement
    if not moremath.get_moremath_dll_import_success():
        pytest.skip("compiled core not installed")
    for module in (optimization_refinement, optimization_needles):
        assert getattr(module.Levenberg_Marquardt, "__file__", None) is None, module.__name__


def test_materials_outside_their_valid_range():
    # OpenFilters evaluates every model anywhere: Ge (data 124-1771 nm) gave n = -34 at 10 um and
    # BK7's Sellmeier formula n = 0, silently.
    with pytest.raises(engine.EngineError, match="unphysical index"):
        engine.spectra({"substrate": "Si", "layers": [{"material": "Ge", "thickness": 500}]}, ["R"], [10000])
    with pytest.raises(engine.EngineError, match="unphysical index"):
        engine.evaluate({"substrate": "BK7", "layers": [{"material": "SiO2", "thickness": 100}]},
                        [{"kind": "R", "wavelength": 10000, "value": 0.1, "inequality": "<="}])
    out = engine.spectra({"substrate": "BK7", "layers": [{"material": "Ge", "thickness": 50}]}, ["R"], [1500, 2500])
    assert out["warnings"] and "extrapolated" in out["warnings"][0]
    assert not engine.spectra(ar_design([50.0, 90.0]), ["R"], [550])["warnings"]


def test_malformed_formula_gives_a_clear_error():
    with pytest.raises(engine.EngineError, match="Stack formulas use letters"):
        engine.describe({"substrate": "BK7", "formula": {"formula": "(HL)^ H", "materials": {"H": "TiO2", "L": "SiO2"}}})


def test_a_solver_step_past_the_deadline_stops_the_optimization(monkeypatch):
    # OpenFilters' pure-Python solver looped forever inside one step with a GDD target; the time
    # limit was only checked between steps.
    monkeypatch.setattr(engine, "DEADLINE_GRACE", 0.0)
    result = engine.optimize(ar_design([27.26, 29.49, 82.03, 100.53]), AR_TARGETS, "needle", {"time_limit": 1e-6})
    assert result["stop_reason"] == "time_limit" and math.isfinite(result["merit_chi2"])


def test_unknown_design_keys_are_refused_with_a_suggestion():
    with pytest.raises(engine.EngineError, match="did you mean 'substrate_thickness_mm'"):
        engine.describe({"substrate": "BK7", "substrate_thickness": 1e6, "layers": []})
    with pytest.raises(engine.EngineError, match="did you mean 'thickness'"):
        engine.describe({"substrate": "BK7", "layers": [{"material": "SiO2", "thicknes": 10}]})


def test_refractiveindex_page_ids_may_contain_spaces(monkeypatch):
    # 495 catalogue pages have spaces ("main/MgF2/nk/Rodriguez-de Marcos") and were refused.
    from openfilters_mcp import refractiveindex
    fetched = []
    monkeypatch.setattr(refractiveindex, "_fetch", lambda path: fetched.append(path) or "DATA: []")
    refractiveindex.load_page("main/MgF2/nk/Rodriguez-de Marcos")
    assert fetched == ["data/main/MgF2/nk/Rodriguez-de Marcos.yml"]


def test_invisible_layer_is_capped_and_flagged():
    # Scenario S2.8: a Ge layer on a Ge substrate (same index) has no gradient; it ran off to 4e16 nm.
    engine.add_material("X_Ge", "constant", n=4.0)
    engine.add_material("X_ZnS", "constant", n=2.2)
    start = {"substrate": "X_Ge", "center_wavelength": 10000, "layers": [
        {"material": "X_Ge", "thickness": 600}, {"material": "X_ZnS", "thickness": 1100}]}
    targets = [{"kind": "R", "from": 8000, "to": 12000, "step": 200, "value": 0.01, "inequality": "<="}]
    result = engine.optimize(start, targets, "refine", {"time_limit": 20})
    assert max(l["thickness"] for l in result["design"]["layers"]) <= 20000 + 1e-6
    assert any("optically invisible" in w for w in result.get("warnings", []))


def test_gdd_band_edges_are_evaluated_with_centred_derivatives():
    # Scenario S2.9: OpenFilters reuses the neighbouring estimate at a band's first and last point, so a
    # dispersive mirror "met" -20 +/- 5 fs^2 on its grid while the true edge GDD was -13.5 fs^2.
    for name, n in (("C_S", 1.45), ("C_H", 2.3), ("C_L", 1.45)):
        engine.add_material(name, "constant", n=n)
    d = {"substrate": "C_S", "center_wavelength": 800,
         "formula": {"formula": "(HL)^10 H", "materials": {"H": "C_H", "L": "C_L"}}}
    target = {"kind": "R_GDD", "polarization": "s", "from": 760, "to": 840, "step": 2, "value": 4.0, "tolerance": 50}
    row = engine.evaluate(d, [target])["targets"][0]
    assert row["points"] == 41  # the padding points are not reported
    fine = engine.spectra(d, ["R_GDD"], [759.5, 760.0, 760.5], [0.0], ["s"])["series"][0]["values"]
    edge = engine.spectra(d, ["R_GDD"], list(range(750, 851, 2)), [0.0], ["s"])["series"][0]["values"][5]  # 760 nm
    assert abs(edge - fine[1]) < 0.05  # centred estimate at 760 nm agrees with a fine grid


def test_margin_and_between_points_check():
    thick = {"substrate": "BK7", "layers": [{"material": "TiO2", "thickness": 2000}]}  # dense fringes
    coarse = engine.spectra(thick, ["R"], list(range(500, 601, 20)))["series"][0]["values"]
    target = [{"kind": "R", "from": 500, "to": 600, "step": 20, "value": float(max(coarse)) + 1e-6, "inequality": "<="}]
    result = engine.evaluate(thick, target)
    assert result["all_targets_met"] and any("between its points" in w for w in result["warnings"])
    tightened = engine.with_margin([{"kind": "R", "value": 0.005, "inequality": "<="},
                                    {"kind": "T", "value": 0.9, "inequality": ">="},
                                    {"kind": "R", "value": 0.5}], 0.001)
    assert [t["value"] for t in tightened] == [pytest.approx(0.004), pytest.approx(0.901), 0.5]


def test_colour_hex_is_gamma_encoded_srgb():
    # Scenario S2.10: the hex was built from OpenFilters' linear RGB (#0a327e instead of about #367bbb).
    c = engine.color(ar_design([18.6, 97.0, 26.6]), [0.0])["colors"][0]
    expected = [12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055 for v in c["linear_RGB"]]
    assert c["sRGB"] == pytest.approx(expected)
    assert c["hex"] == "#%02x%02x%02x" % tuple(round(v * 255) for v in expected)


def test_material_summary_gives_the_data_range():
    row = next(r for r in engine.list_materials("Ge") if r["name"] == "Ge")
    assert row["data_range_nm"][1] < 2000 and "note" not in row  # 400-1550 nm are inside the data
    row = next(r for r in engine.list_materials("Ge", wavelengths=(550.0, 2500.0)) if r["name"] == "Ge")
    assert "2500" in row["note"] and "extrapolated" in row["note"]
