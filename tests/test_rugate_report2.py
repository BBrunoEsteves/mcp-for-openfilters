"""Regression tests from the second rugate / graded-layer report (2026-10-27, build 739785d).

N1: |E|^2 inside graded layers; N2: layer_sensitivity with graded layers; N3: tolerance inputs refused
before a job is created; F4: tolerance analysis of graded layers (whole-layer thickness scaling);
R1-R6: the report's reference rugates made by the profile generator (its section 4).
"""

import asyncio
import os

import numpy as np
import pytest

from mcp_for_openfilters import engine

BASE = {"substrate": "FusedSilica", "incident_medium": "void", "center_wavelength": 1064}
TARGETS = [{"kind": "R", "from": 1000, "to": 1130, "step": 5, "value": 0.99, "inequality": ">="},
           {"kind": "R", "from": 400, "to": 920, "step": 5, "value": 0.06, "inequality": "<="},
           {"kind": "R", "from": 1240, "to": 1500, "step": 5, "value": 0.06, "inequality": "<="}]


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path))
    engine.catalog(reload=True)


@pytest.fixture
def server(data_home, monkeypatch):
    # Worker processes keep the data folder they started with: start them (and the job manager) for this test's.
    from mcp_for_openfilters import server

    def shutdown():
        if server._interactive is not None:
            server._interactive.shutdown()
            server._interactive = None

    shutdown()
    monkeypatch.setattr(server, "_manager", None)
    yield server
    shutdown()


def rugate(periods, apodization=None, matching=True):
    graded = {"profile": {"type": "rugate", "n_min": 1.5, "n_max": 2.2, "periods": periods}}
    if apodization:
        graded["profile"]["apodization"] = apodization
    if matching:
        graded["matching"] = {"substrate": "quintic", "medium": "quintic"}
    return dict(BASE, layers=[{"material": "IdealMixture", "graded": graded}])


R5 = rugate(32, {"type": "hann"})


# --- R1-R6: the report's section 4 -----------------------------------------------------------------------

# R_peak (950-1200), R@1064, blue lobe (700-930), red lobe (1230-1500), R532 (500-560), R355 (340-370),
# and the R > 0.99 band.
REFERENCE = {
    "R1": (rugate(16, matching=False), [0.999623, 0.999620, 0.5341, 0.5423, 0.1671, 0.1482], [977.91, 1158.51]),
    "R2": (rugate(16), [0.999622, 0.999620, 0.5405, 0.5510, 0.0815, 0.0620], None),
    "R3": (rugate(16, {"type": "hann"}), [0.958573, 0.953663, 0.0454, 0.0491, 0.0433, 0.0425], []),
    "R4": (rugate(24, {"type": "hann", "taper_periods": 4}), [0.999966, 0.999966, 0.3538, 0.2633, 0.0436, 0.0431], None),
    "R5": (R5, [0.999632, 0.999621, 0.0462, 0.0486, 0.0455, 0.0435], [995.70, 1137.70]),
    "R6": (rugate(24, {"type": "gaussian"}), [0.987249, 0.985948, 0.0615, 0.1019, 0.0431, 0.0436], []),
}


@pytest.mark.parametrize("case", sorted(REFERENCE))
def test_reference_rugates(case):
    design, values, band = REFERENCE[case]
    s = engine.spectra(design, ["R"], {"from": 300, "to": 1500, "step": 1})
    x, r = np.asarray(s["wavelengths"]), np.asarray(s["series"][0]["values"])

    def peak(lo, hi):
        return r[(x >= lo) & (x <= hi)].max()

    got = [peak(950, 1200), r[x == 1064][0], peak(700, 930), peak(1230, 1500), peak(500, 560), peak(340, 370)]
    assert got[:2] == pytest.approx(values[:2], abs=2e-6)
    assert got[2:] == pytest.approx(values[2:], abs=1e-4)
    if band is not None:
        bands = engine.crossing_ranges(x, r, 0.99)
        assert [edge for b in bands for edge in b] == pytest.approx(band, abs=0.1)


# --- N1: electric field inside graded layers ----------------------------------------------------------------

def test_n1_one_step_graded_layer_has_the_homogeneous_field():
    homogeneous = engine.electric_field(dict(BASE, layers=[{"material": "IdealMixture", "index": 1.8,
                                                            "thickness": 1000}]), 1064)
    graded = engine.electric_field(dict(BASE, layers=[{"material": "IdealMixture",
                                                       "graded": {"steps": [30], "step_thicknesses": [1000]}}]), 1064)
    assert graded["position_nm"] == homogeneous["position_nm"]
    assert graded["E_squared"] == pytest.approx(homogeneous["E_squared"], abs=1e-12)
    e, x = np.asarray(graded["E_squared"]), np.asarray(graded["position_nm"])
    maxima = [x[i] for i in range(1, len(e) - 1) if e[i] > e[i - 1] and e[i] >= e[i + 1]]
    assert len(maxima) == 3 and np.diff(maxima) == pytest.approx(1064 / (2 * 1.8), abs=1.0)  # ~3.4 periods


def test_n1_rugate_field_is_sampled_inside_thick_steps():
    r = engine.electric_field(R5, 1064)
    x, e = np.asarray(r["position_nm"]), np.asarray(r["E_squared"])
    assert np.diff(x).max() <= 1.0 + 1e-9
    plateau = (x > 9400) & (x < 9750)  # a ~350 nm step at n = 1.82: a full standing wave, not a straight line
    assert e[plateau].min() < 0.1 and e[plateau].max() > 2.5
    (peak,) = r["graded_layer_peaks"]
    assert peak["E_squared"] == e.max() and peak["position_nm"] == x[np.argmax(e)]
    assert peak["E_squared"] == pytest.approx(3.998813, abs=1e-5)


def test_n1_server_peak_is_the_fine_curve_maximum(server):
    out = asyncio.run(server.compute_electric_field(R5, 1064, plot=False))
    assert out["peak_E_squared"] == pytest.approx(3.99881, abs=1e-5)
    assert out["graded_layer_peaks"][0]["layer"] == 1


# --- N2: layer_sensitivity --------------------------------------------------------------------------------

def test_n2_layer_sensitivity_scales_graded_layers():
    r = engine.sensitivity(R5, TARGETS[:1])
    (row,) = r["layers"]
    assert row["graded"] is True and row["n_steps"] == 2305
    assert row["delta_chi2"] > 0 and row["delta_worst_margin"] < 0
    assert r["most_sensitive"] == ["front #1"]


# --- F4 and N3: tolerance analysis -----------------------------------------------------------------------

def test_f4_graded_layers_are_scaled_as_a_whole():
    """The R > 0.99 band (995.7-1137.7 nm) moves with the thickness: +1 % pushes its blue edge past 1000 nm,
    -0.5 % keeps the 1000-1130 nm target inside it."""
    def run(systematic):
        return engine.tolerance(R5, TARGETS[:1], n_trials=1,
                                thickness_error={"type": "relative", "sigma": 0.0, "systematic": systematic})
    assert run(0.01)["yield"] == 0.0
    assert run(-0.005)["yield"] == 1.0


def test_f4_yield_of_r5_and_mixture_index_note():
    r = engine.tolerance(R5, TARGETS, n_trials=60, thickness_error={"type": "relative", "sigma": 0.01},
                         index_error={"sigma": 0.005})
    # Passes for -0.68 % < error < +0.43 %: about 42 % with sigma = 1 %.
    assert 0.2 < r["yield"] < 0.65
    assert r["target_failure_rate"][1:] == [0.0, 0.0]
    assert any("does not perturb mixtures (IdealMixture)" in w for w in r["warnings"])


@pytest.mark.parametrize("kwargs, message", [
    ({"thickness_error": {"type": "percent", "sigma": 1}}, "type must be"),
    ({"thickness_error": {"type": "relative", "sigma": -0.01}}, "sigma must be a finite number >= 0"),
    ({"thickness_error": {"sigma": 0.01, "distrib": "normal"}}, "unknown thickness_error key"),
    ({"index_error": {"sigma": "big"}}, "index_error sigma"),
])
def test_n3_bad_error_models_are_refused(kwargs, message):
    with pytest.raises(engine.EngineError, match=message):
        engine.check_tolerance(R5, TARGETS, **kwargs)


def _jobs(server):
    from mcp_for_openfilters import settings
    folder = settings.jobs_dir()
    return os.listdir(folder) if os.path.isdir(folder) else []


def test_n3_tolerance_analysis_refuses_before_creating_a_job(server):
    with pytest.raises(server.ToolError, match="type must be"):
        asyncio.run(server.tolerance_analysis(R5, TARGETS, n_trials=10, thickness_error={"type": "percent"}))
    bad = dict(BASE, layers=[{"material": "IdealMixture", "index": 1.4, "thickness": 100}])
    with pytest.raises(server.ToolError, match="outside the range of the mixture"):
        asyncio.run(server.tolerance_analysis(bad, TARGETS, n_trials=10))
    with pytest.raises(server.ToolError, match="type must be"):
        asyncio.run(server.run_batch("tolerance", {"design": R5, "target_specs": TARGETS, "n_trials": 5,
                                                   "thickness_error": {"type": "$kind"}}, {"kind": ["percent"]}))
    with pytest.raises(server.ToolError, match="distribution must be"):
        asyncio.run(server.run_optimization_campaign(
            TARGETS, starts=[dict(BASE, layers=[{"material": "TiO2", "thickness": 100}])],
            tolerance={"thickness_error": {"distribution": "box"}}))
    assert _jobs(server) == []
