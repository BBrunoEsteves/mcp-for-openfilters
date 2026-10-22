"""Tests of the engine layer used by the MCP tools."""

import numpy as np
import pytest

from openfilters_mcp import engine
from reference_tmm import rt

MIRROR = {"substrate": "FusedSilica", "center_wavelength": 1064,
          "formula": {"formula": "(HL)^8 H", "materials": {"H": "TiO2", "L": "SiO2"}}}
TARGETS = [{"kind": "R", "from": 1000, "to": 1130, "step": 5, "value": 0.995, "tolerance": 0.005, "inequality": ">="},
           {"kind": "T", "from": 600, "to": 700, "step": 5, "value": 0.9, "tolerance": 0.02, "inequality": ">="}]


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFILTERS_MCP_HOME", str(tmp_path))
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
