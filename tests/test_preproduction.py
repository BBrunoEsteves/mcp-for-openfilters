"""Checks of 2026-10-30 against OpenFilters' GUI: polarization and tilt as in its dialogs (s, p,
unpolarized or a polarization angle), Preproduction > Simulate random errors, and the spectroscopic
option of the monitoring dialogs. Physics compared with the independent transfer-matrix code."""

import asyncio
import math
import random

import numpy as np
import pytest

from mcp_for_openfilters import engine, server
from reference_tmm import rt

WVLS = list(np.arange(450.0, 851.0, 10.0))
STACK = [{"material": "TiO2", "thickness": 61.0}, {"material": "SiO2", "thickness": 102.0},
         {"material": "TiO2", "thickness": 125.0}, {"material": "SiO2", "thickness": 95.0}]
DESIGN = {"substrate": "BK7", "incident_medium": "void", "center_wavelength": 600, "layers": STACK}


def N(name, wvls=WVLS):
    return np.array(engine.material_N(name, wvls, "hold"))


def reference(layers, angle, pol, wvls=WVLS, medium="void", substrate="BK7"):
    stack = [(N(l["material"], wvls), l["thickness"]) for l in reversed(layers)]  # from the medium inward
    return rt(wvls, N(medium, wvls), stack, N(substrate, wvls), angle, pol)


@pytest.mark.parametrize("psi", [0.0, 30.0, 45.0, 72.5, 90.0])
@pytest.mark.parametrize("angle", [0.0, 40.0, 70.0])
def test_polarization_angle_mixes_s_and_p(psi, angle):
    # OpenFilters' dialogs take a polarization angle psi: R = sin^2 psi Rs + cos^2 psi Rp.
    Rs, Ts, _ = reference(STACK, angle, "s")
    Rp, Tp, _ = reference(STACK, angle, "p")
    s2, c2 = math.sin(math.radians(psi)) ** 2, math.cos(math.radians(psi)) ** 2
    for given in (psi, f"{psi:g}", f"{psi:g}deg"):
        out = engine.spectra(DESIGN, ["R", "T"], WVLS, [angle], [given])
        R, T = (np.asarray(s["values"]) for s in out["series"])
        np.testing.assert_allclose(R, s2 * Rs + c2 * Rp, atol=1e-12)
        np.testing.assert_allclose(T, s2 * Ts + c2 * Tp, atol=1e-12)


def test_polarization_names_parse_back_and_range_is_checked():
    for value in (0.0, 12.5, 45.0, 90.0):
        assert engine.polarization_value(engine.polarization_name(value)) == value
    for bad in (-1, 91, "q", "30 rad", True):
        with pytest.raises(engine.EngineError):
            engine.polarization_value(bad)


def test_tools_accept_a_polarization_angle():
    # The schemas only allowed strings and the engine only parsed real numbers: no tool took psi.
    out = asyncio.run(server.compute_spectrum(DESIGN, ["R"], WVLS, [45.0], [30, "60deg"], plot=False))
    assert [s["polarization"] for s in out["series"]] == ["30deg", "60deg"]
    for name, args in (("compute_spectrum", {"polarizations": [30, "s"], "wavelengths": [600], "plot": False}),
                       ("compute_color", {"polarization": 30}),
                       ("compute_monitoring_curves", {"polarization": 30, "wavelengths": [600], "plot": False}),
                       ("simulate_random_errors", {"polarization": 30, "nb_tests": 3, "wavelengths": [600], "plot": False})):
        result = asyncio.run(server.mcp.call_tool(name, {"design": DESIGN, **args}))  # through the JSON schema
        assert not result.is_error, (name, result)
    targets = [{"kind": "R", "angle": 45, "polarization": 30, "wavelength": 600, "value": 0.1}]
    evaluated = engine.evaluate(DESIGN, targets)
    Rs, _, _ = reference(STACK, 45.0, "s", [600.0])
    Rp, _, _ = reference(STACK, 45.0, "p", [600.0])
    assert evaluated["targets"][0]["worst_value"] == pytest.approx(0.25 * Rs[0] + 0.75 * Rp[0], abs=1e-6)
    assert engine.color(DESIGN, [30.0], polarization="20")["polarization"] == "20deg"


def test_project_keeps_a_polarization_angle(tmp_path):
    targets = [{"kind": "T", "angle": 30, "polarization": 22.5, "from": 500, "to": 600, "step": 10, "value": 0.9}]
    path = str(tmp_path / "psi.ofp")
    engine.save_project(path, [DESIGN], targets)
    back = engine.load_project(path)["targets"][0]
    assert engine.polarization_value(back["polarization"]) == 22.5


@pytest.mark.parametrize("angle", [0.0, 30.0, 50.0])
def test_reverse_angle_is_measured_in_the_substrate(angle):
    # OpenFilters' reverse direction: the light comes from the substrate, at `angle` inside it
    # (beyond about 41 deg in BK7 the front stack totally reflects).
    for pol in ("s", "p"):
        R = np.asarray(engine.spectra(DESIGN, ["R"], WVLS, [angle], [pol], reverse=True)["series"][0]["values"])
        sub, air = N("BK7"), N("void")
        stack = [(N(l["material"]), l["thickness"]) for l in STACK]  # substrate side first
        R_ref = [rt([w], sub[i], [(n[i], d) for n, d in stack], air[i], angle, pol)[0][0] for i, w in enumerate(WVLS)]
        np.testing.assert_allclose(R, R_ref, atol=1e-12)


def _redraw(design, nb_tests, seed, kind, value, dist):
    """The thicknesses OpenFilters' random_errors draws: per test, per front layer, from `random`."""
    random.seed(seed)
    designs = []
    for _ in range(nb_tests):
        layers = []
        for l in design["layers"]:
            error = value * l["thickness"] if kind == "relative" else value
            t = random.uniform(l["thickness"] - error, l["thickness"] + error) if dist == "uniform" \
                else random.gauss(l["thickness"], error)
            layers.append(dict(l, thickness=max(t, 0.0)))
        designs.append(layers)
    return designs


@pytest.mark.parametrize("kind,value,dist", [("relative", 0.02, "uniform"), ("absolute", 3.0, "normal")])
def test_random_errors_tests_are_the_drawn_designs(kind, value, dist):
    error = {"type": kind, "value": value, "distribution": dist}
    out = engine.random_errors(DESIGN, ["R", "T"], WVLS, angle=20.0, polarization=30.0, thickness_error=error,
                               nb_tests=12, seed=7)
    R, T = out["series"]
    psi = (math.sin(math.radians(30.0)) ** 2, math.cos(math.radians(30.0)) ** 2)
    for layers, R_test, T_test in zip(_redraw(DESIGN, 12, 7, kind, value, dist), R["tests"], T["tests"]):
        Rs, Ts, _ = reference(layers, 20.0, "s")
        Rp, Tp, _ = reference(layers, 20.0, "p")
        np.testing.assert_allclose(R_test, psi[0] * Rs + psi[1] * Rp, atol=1e-12)
        np.testing.assert_allclose(T_test, psi[0] * Ts + psi[1] * Tp, atol=1e-12)
    for s in (R, T):
        tests = np.asarray(s["tests"])
        np.testing.assert_allclose(s["mean"], tests.mean(axis=0), atol=1e-12)
        np.testing.assert_allclose(s["std_dev"], tests.std(axis=0, ddof=1), atol=1e-12)
        np.testing.assert_allclose(s["min"], tests.min(axis=0), atol=1e-15)
        np.testing.assert_allclose(s["max"], tests.max(axis=0), atol=1e-15)
    np.testing.assert_allclose(R["design"], engine.spectra(DESIGN, ["R"], WVLS, [20.0], [30.0])["series"][0]["values"],
                               atol=1e-12)


def test_random_errors_gd_in_fs_and_color_in_delta_e():
    grid = {"from": 500, "to": 700, "step": 2}
    out = engine.random_errors(DESIGN, ["R_GD", "color"], grid, polarization="s", nb_tests=5)
    gd, color = out["series"]
    nominal = engine.spectra(DESIGN, ["R_GD"], grid, [0.0], ["s"])["series"][0]["values"]
    np.testing.assert_allclose(gd["design"], nominal, rtol=1e-9)  # fs, edges padded as in spectra()
    assert color["R"]["design_Lab"] == pytest.approx(engine.color(DESIGN, [0.0], "s")["colors"][0]["Lab"], abs=1e-9)
    assert color["R"]["worst_delta_E2000"] == pytest.approx(max(color["R"]["delta_E2000_tests"]), rel=1e-9)
    with pytest.raises(engine.EngineError, match="s or p"):
        engine.random_errors(DESIGN, ["R_phase"], grid, polarization="unpolarized", nb_tests=5)


def test_random_errors_stops_at_its_time_limit_and_warns_about_back_layers():
    with pytest.raises(engine.EngineError, match="fewer tests"):
        engine.random_errors(DESIGN, ["R"], WVLS, nb_tests=100000, time_limit=0.5)
    two_sided = dict(DESIGN, consider_backside=True, back_layers=[{"material": "SiO2", "thickness": 90.0}])
    out = engine.random_errors(two_sided, ["T"], WVLS, nb_tests=3)
    assert any("only the front layers" in w for w in out["warnings"])


def test_simulate_random_errors_tool():
    out = asyncio.run(server.simulate_random_errors(DESIGN, ["R", "T"], WVLS, nb_tests=20, plot=True,
                                                    show={"std_devs": 3, "mean": True, "all_results": True},
                                                    save_all_results=True))
    result, images = out[0], out[1:]
    assert len(images) == 2 and len(result["plot_files"]) == 2
    assert result["results"][0]["quantity"] == "R" and result["results"][0]["max_std_dev"] > 0
    with open(result["statistics_csv"], encoding="utf-8") as f:
        assert f.readline().startswith("wavelength_nm,R_design,R_mean,R_std_dev,R_min,R_max")
    with open(result["all_results_csv"], encoding="utf-8") as f:
        assert len(f.readline().split(",")) == 1 + 2 * 20


def test_spectroscopic_monitoring_uses_the_design_wavelength_range():
    d = dict(DESIGN, consider_backside_on_monitoring=False,
             wavelength_range={"from": 450.0, "to": 750.0, "step": 5.0})
    m = engine.monitoring(d, "spectroscopic", mode="transmission", angle=30.0, polarization="p")
    assert m["wavelengths"][0] == 450.0 and m["wavelengths"][-1] == 750.0 and len(m["wavelengths"]) == 61
    assert "signal" not in m  # too many wavelengths for a dict
    final = m["signal_map"][:, -1]
    _, T_ref, _ = reference(STACK, 30.0, "p", m["wavelengths"])
    np.testing.assert_allclose(final, T_ref, atol=1e-9)


def test_reverse_monitoring_ends_at_the_reverse_spectrum():
    d = dict(DESIGN, consider_backside_on_monitoring=False)
    m = engine.monitoring(d, [550.0, 650.0], mode="reflection", direction="reverse", angle=20.0, polarization="s")
    R = engine.spectra(d, ["R"], [550.0, 650.0], [20.0], ["s"], reverse=True)["series"][0]["values"]
    np.testing.assert_allclose(m["signal_map"][:, -1], R, atol=1e-9)


def test_spectroscopic_monitoring_tool_selects_the_center_wavelength():
    d = dict(DESIGN, wavelength_range={"from": 500.0, "to": 700.0, "step": 4.0})  # 600 on the grid
    out = asyncio.run(server.compute_monitoring_curves(d, "spectroscopic", save_map=True, plot=True))
    result = out[0]
    assert result["selected_wavelength"] == 600.0 and list(result["signal_at_layer_ends"]) == ["600"]
    assert len(result["layer_end_spectra"]) == 4 and len(out) == 3  # map + selected curve
    with open(result["layer_end_spectra_csv"], encoding="utf-8") as f:
        assert f.readline().strip() == "wavelength_nm,after_layer_1,after_layer_2,after_layer_3,after_layer_4"
    with open(result["map_csv"], encoding="utf-8") as f:
        assert len(f.readline().split(",")) == 1 + 51
