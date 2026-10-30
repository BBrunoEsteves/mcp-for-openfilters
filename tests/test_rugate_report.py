"""Regression tests from the rugate / graded-layer report of 2026-10-26.

Item numbers (B1, ..., F5, C1-C5) are those of the report. Reference values: its section 5, computed with
the connector in a Claude chat (C1-C5) and reproduced here.
"""

import json
import os

import numpy as np
import pytest

from openfilters_mcp import engine

RUGATE = os.path.join(os.path.dirname(engine.__file__), "..", "openfilters_core", "examples", "Rugate.ofp")
BASE = {"substrate": "FusedSilica", "incident_medium": "void", "center_wavelength": 1064}
CONSTANTS = {"n140": 1.4, "n14431": 1.4431, "n14536": 1.4536, "n15675": 1.5675, "n160": 1.6, "n1755": 1.755,
             "n180": 1.8, "n1965": 1.965, "n200": 2.0, "n21346": 2.1346, "n21464": 2.1464, "n220": 2.2}


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFILTERS_MCP_HOME", str(tmp_path))
    engine.catalog(reload=True)
    for name, n in CONSTANTS.items():
        engine.add_material(name, "constant", n=n)


@pytest.fixture
def server(data_home):
    # The server's worker processes keep the data folder they started with: start them for this test's.
    from openfilters_mcp import server

    def shutdown():
        if server._interactive is not None:
            server._interactive.shutdown()
            server._interactive = None

    shutdown()
    yield server
    shutdown()


def mixture_pair(low):
    return dict(BASE, formula={"formula": "(HL)^12", "materials": {"H": {"material": "IdealMixture", "index": 2.2},
                                                                  "L": {"material": "IdealMixture", "index": low}}})


def summary(design, wavelengths=None):
    s = engine.spectra(design, ["R"], wavelengths or {"from": 300, "to": 1500, "step": 1})
    x, v = s["wavelengths"], s["series"][0]["values"]
    i, j = int(np.argmax(v)), int(np.argmin(v))
    return {"max": v[i], "at_max": x[i], "min": v[j], "at_min": x[j], "bands": engine.crossing_ranges(x, v, 0.99)}


def flat(bands):
    return [edge for band in bands for edge in band]


def report_c4():
    """The report's script (section 4, F1) with q = 5: ln n sinusoidal in optical thickness, 16 periods,
    sin^2 apodization over 4 periods at each end, IdealMixture steps of 0.05."""
    lam0, n_low, n_high, periods, taper = 1064.0, 1.5, 2.2, 16, 4
    mean, amplitude = (np.log(n_low) + np.log(n_high)) / 2, (np.log(n_high) - np.log(n_low)) / 2
    s = np.linspace(0, periods * lam0 / 2, 200000)
    x = s / (lam0 / 2)
    envelope = np.ones_like(x)
    envelope[x < taper] = np.sin(np.pi / 2 * x[x < taper] / taper) ** 2
    end = x > periods - taper
    envelope[end] = np.sin(np.pi / 2 * (periods - x[end]) / taper) ** 2
    n = np.exp(mean + amplitude * envelope * np.sin(2 * np.pi * x))
    k = (np.round((n - 1.5) / 0.01 / 5) * 5).astype(int)
    steps, thicknesses = [], []
    for ki, ni in zip(k, n):
        if steps and steps[-1] == ki:
            thicknesses[-1] += (s[1] - s[0]) / ni
        else:
            steps.append(int(ki))
            thicknesses.append((s[1] - s[0]) / ni)
    return dict(BASE, layers=[{"material": "IdealMixture", "graded": {"steps": steps, "step_thicknesses": thicknesses}}])


# --- C1-C5: the report's reference results -------------------------------------------------------------

def test_c1_quarter_wave_stack():
    r = summary(dict(BASE, formula={"formula": "(HL)^12", "materials": {"H": "n220", "L": "n140"}}))
    assert r["max"] == pytest.approx(0.99988702, abs=1e-6) and r["at_max"] == 1064
    assert r["min"] == pytest.approx(0.00569519, abs=1e-6) and r["at_min"] == 1276
    assert flat(r["bands"]) == pytest.approx(flat([[339.81, 371.01], [939.73, 1226.19]]), abs=0.1)


def test_c2_sinusoidal_n_has_a_false_second_harmonic():
    formula = "(0.1667A 0.1852B 0.1987C 0.2037D 0.1987C 0.1852B 0.1667A 0.1481E 0.1346F 0.1296G 0.1346F 0.1481E)^12"
    design = dict(BASE, formula={"formula": formula, "materials": {
        "A": "n180", "B": "n200", "C": "n21464", "D": "n220", "E": "n160", "F": "n14536", "G": "n140"}})
    r = summary(design)
    assert r["max"] == pytest.approx(0.99843746, abs=1e-6) and r["at_max"] == 1069
    assert flat(r["bands"]) == pytest.approx(flat([[985.50, 1162.37]]), abs=0.1)
    assert summary(design, {"from": 500, "to": 560, "step": 0.5})["max"] == pytest.approx(0.75, abs=0.01)


def test_c3_sinusoidal_ln_n_in_optical_thickness():
    formula = "(" + " ".join(f"0.166667{c}" for c in "ABCDCBAEFGFE") + ")^12"
    r = summary(dict(BASE, formula={"formula": formula, "materials": {
        "A": "n1755", "B": "n1965", "C": "n21346", "D": "n220", "E": "n15675", "F": "n14431", "G": "n140"}}))
    assert r["max"] == pytest.approx(0.99875545, abs=1e-6) and r["at_max"] == 1064
    assert r["min"] == pytest.approx(0.000976, abs=1e-6) and r["at_min"] == 930
    assert flat(r["bands"]) == pytest.approx(flat([[976.04, 1165.79]]), abs=0.1)


def test_c4_apodized_graded_layer():
    design = report_c4()
    assert len(design["layers"][0]["graded"]["steps"]) == 337
    r = summary(design)
    assert r["max"] == pytest.approx(0.99601014, abs=1e-6) and r["at_max"] == 1047
    assert r["min"] == pytest.approx(0.0182, abs=1e-4) and r["at_min"] == 843
    assert flat(r["bands"]) == pytest.approx(flat([[993.70, 1130.70]]), abs=0.1)


def test_c5_mixture_symbols_within_their_range():
    r = summary(mixture_pair(1.5), {"from": 400, "to": 1500, "step": 2})
    assert r["max"] == pytest.approx(0.99940844, abs=1e-6) and r["at_max"] == 1064
    assert r["min"] == pytest.approx(0.0091807, abs=1e-6) and r["at_min"] == 1250


# --- B1, B2, B5, B6: validation of mixture indices and graded layers -------------------------------------

def test_b1_b2_mixture_index_outside_its_range_is_refused():
    # C6: L = 1.4 on IdealMixture (1.5-2.4) crashed the compiled core ("the calculation process crashed").
    for call in (lambda d: engine.describe(d), lambda d: engine.spectra(d, ["R"], [1064])):
        with pytest.raises(engine.EngineError, match=r"index 1.4 is outside the range of the mixture IdealMixture "
                                                     r"at the center wavelength 1064 nm \[1.5000, 2.4000\]"):
            call(mixture_pair(1.4))
    layer = dict(BASE, layers=[{"material": "IdealMixture", "thickness": 100, "index": 2.5}])
    with pytest.raises(engine.EngineError, match="layer 1: index 2.5 is outside"):
        engine.describe(layer)


def test_b1_the_server_reports_the_error_instead_of_crashing(server):
    import asyncio
    with pytest.raises(server.ToolError, match="outside the range of the mixture") as error:
        asyncio.run(server.compute_spectrum(mixture_pair(1.4), quantities=["R"], plot=False))
    assert "crashed" not in str(error.value)
    assert asyncio.run(server.compute_spectrum(mixture_pair(1.5), quantities=["R"], plot=False))["series"]


@pytest.mark.parametrize("graded, message", [
    ({"steps": [0, 50, 95], "step_thicknesses": [10, 10, 10]}, "step 95 is outside IdealMixture's steps 0-90"),
    ({"steps": [0, 50], "step_thicknesses": [10, 10, 10]}, "2 steps but 3 step_thicknesses"),
    ({"steps": [0, 1], "step_thicknesses": [10, -1]}, "thicknesses >= 0 nm"),
    ({"steps": [0.5, 1], "step_thicknesses": [10, 10]}, "steps must be integers"),
    ({"indices_at_cw": [1.6, 1.2], "step_thicknesses": [10, 10]}, "index 1.2 is outside the range"),
    ({"step_thicknesses": [10, 10]}, "needs"),
    ({"steps": [1], "step_thicknesses": [10], "profile": "rugate"}, "unknown"),
])
def test_b5_graded_layers_are_validated(graded, message):
    with pytest.raises(engine.EngineError, match=message):
        engine.describe(dict(BASE, layers=[{"material": "IdealMixture", "graded": graded}]))


def test_b5_a_graded_layer_needs_a_mixture():
    with pytest.raises(engine.EngineError, match="SiO2 is not a mixture"):
        engine.describe(dict(BASE, layers=[{"material": "SiO2", "graded": {"steps": [0], "step_thicknesses": [10]}}]))


def test_b5_indices_at_cw_are_rounded_to_the_steps():
    by_index = dict(BASE, layers=[{"material": "IdealMixture", "graded": {
        "indices_at_cw": [1.6, 1.8, 2.0, 2.2], "step_thicknesses": [25, 25, 25, 25]}}])
    by_step = dict(BASE, layers=[{"material": "IdealMixture", "graded": {
        "steps": [10, 30, 50, 70], "step_thicknesses": [25, 25, 25, 25]}}])
    assert summary(by_index, [800, 1064]) == summary(by_step, [800, 1064])
    assert not engine.material_warnings(by_index, [1064])
    rounded = dict(BASE, layers=[{"material": "IdealMixture", "graded": {
        "indices_at_cw": [1.603, 2.2], "step_thicknesses": [25, 25]}}])
    (warning,) = engine.material_warnings(rounded, [1064])
    assert "rounded to IdealMixture's composition steps" in warning and "largest change 0.003" in warning


def test_b6_graded_thickness_is_the_sum_of_the_steps():
    graded = {"steps": [10, 70], "step_thicknesses": [100.004, 200.0]}
    for thickness in (None, 300.0):  # omitted, or equal to the sum within 0.01 nm
        layer = {"material": "IdealMixture", "graded": graded, **({} if thickness is None else {"thickness": thickness})}
        assert engine.describe(dict(BASE, layers=[layer]))["layers"][0]["thickness_nm"] == pytest.approx(300.004)
    with pytest.raises(engine.EngineError, match="differs from the sum of its step_thicknesses"):
        engine.describe(dict(BASE, layers=[{"material": "IdealMixture", "thickness": 310, "graded": graded}]))


# --- B3, B4: graded layers in describe_design -------------------------------------------------------------

def test_b4_graded_layer_summary():
    row = engine.describe(report_c4())["layers"][0]
    assert row["graded"] and row["n_steps"] == 337
    assert (row["n_min@cw"], row["n_max@cw"], row["n_first@cw"], row["n_last@cw"]) == (1.5, 2.2, 1.8, 1.8)
    assert row["thickness_nm"] == pytest.approx(4715.321, abs=1e-3)
    assert row["optical_thickness_nm"] == pytest.approx(16 * 1064 / 2, rel=2e-3)  # 16 half-wave periods
    assert row["qwot@cw"] == pytest.approx(4 * row["optical_thickness_nm"] / 1064, abs=1e-4)


def test_b3_index_profile_draws_graded_layers(server, monkeypatch):
    import asyncio
    from openfilters_mcp import plotting
    drawn = {}
    original = plotting.index_profile

    def spy(segments, substrate_n, medium_n, center_wavelength, title="Index profile"):
        drawn.update(segments=segments, substrate=substrate_n, medium=medium_n)
        return original(segments, substrate_n, medium_n, center_wavelength, title)

    monkeypatch.setattr(plotting, "index_profile", spy)
    design = dict(BASE, layers=[{"material": "SiO2", "thickness": 50}] + report_c4()["layers"])
    out = asyncio.run(server.describe_design(design, plot_index_profile=True))
    assert out[0]["plot_files"] and "index_profile" not in out[0]
    assert len(drawn["segments"]) == 338 and drawn["segments"][0][0] == 50
    assert sum(d for d, _ in drawn["segments"]) == pytest.approx(4765.321, abs=1e-3)
    assert drawn["substrate"] == pytest.approx(engine.material_N("FusedSilica", [1064])[0].real, abs=1e-4)
    assert drawn["medium"] == 1.0
    assert out[0]["design"] == {"design_file": out[0]["design"]["design_file"]}  # 337 steps: in a file


# --- B7, F3: projects with graded layers -------------------------------------------------------------------

def test_b7_rugate_example_is_summarized_and_reusable(server):
    import asyncio
    out = asyncio.run(server.load_project("examples/Rugate"))
    assert len(out["designs"]) == 5 and len(json.dumps(out)) < 20000  # was 331 403 characters
    rugate = out["designs"][1]
    layer = rugate["layers"][0]
    assert layer["graded"] is True and layer["n_steps"] == 1543 and "step_thicknesses" not in json.dumps(layer)
    full = engine.load_project(RUGATE)["designs"][1]
    assert engine.resolve_design_files({"design_file": rugate["design_file"]}) == full
    spectrum = asyncio.run(server.compute_spectrum({"design_file": rugate["design_file"]}, quantities=["R"],
                                                   wavelengths=[550], plot=False))
    assert spectrum["series"][0]["max"] == pytest.approx(summary(full, [550])["max"], abs=1e-8)
    assert len(asyncio.run(server.load_project("examples/Rugate", full_graded_arrays=True))["designs"][1]["layers"][0]
               ["graded"]["steps"]) == 1543


def test_f3_graded_designs_survive_a_save_and_reload(tmp_path):
    original = engine.load_project(RUGATE)
    path = engine.save_project(str(tmp_path / "copy.ofp"), original["designs"], None, original["comment"])
    again = engine.load_project(path)["designs"]
    for before, after in zip(original["designs"], again):
        assert after == before
        assert summary(after, {"from": 400, "to": 900, "step": 5}) == summary(before, {"from": 400, "to": 900, "step": 5})


# --- F5, B9 -----------------------------------------------------------------------------------------------

def test_f5_mixture_steps():
    table = engine.mixture_table("IdealMixture", [550.0, 1064.0])
    assert table["n_steps"] == 91 and table["columns"] == ["step", "n@550", "n@1064"]
    assert table["rows"][0] == [0, 1.5, 1.5] and table["rows"][90] == [90, 2.4, 2.4]
    assert all(row[1] == pytest.approx(1.5 + 0.01 * row[0]) for row in table["rows"])


def test_b9_spectrum_plot_names_the_quantity(monkeypatch):
    from openfilters_mcp import plotting
    labels = []
    original = plotting._figure
    monkeypatch.setattr(plotting, "_figure", lambda title, x, y: labels.append((title, y)) or original(title, x, y))
    result = engine.spectra(mixture_pair(1.5), ["R"], [1000, 1064])
    plotting.spectra(result, "C5")
    result = engine.spectra(mixture_pair(1.5), ["R", "T"], [1000, 1064])
    plotting.spectra(result, "C5")
    assert labels == [("C5", "Reflectance"), ("C5", "Reflectance / Transmittance")]
