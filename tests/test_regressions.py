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
