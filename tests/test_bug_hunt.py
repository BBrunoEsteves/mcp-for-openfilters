"""Findings of the scenario run of 2026-10-30 (likely chat requests driven through the live tools)."""

import asyncio

import numpy as np
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from openfilters_mcp import engine, plotting, server

AR = {"substrate": "BK7", "center_wavelength": 550,
      "layers": [{"material": "TiO2", "thickness": 11.5}, {"material": "SiO2", "thickness": 35.0},
                 {"material": "TiO2", "thickness": 110.1}, {"material": "SiO2", "thickness": 90.8}]}
TARGETS = [{"kind": "R", "angle": 30, "polarization": 30, "from": 430, "to": 670, "step": 10, "value": 0.01,
            "inequality": "<="}]


@pytest.fixture
def figures(monkeypatch):
    """(title, xlabel, ylabel) of every figure drawn."""
    drawn = []
    original = plotting._figure

    def spy(title, xlabel, ylabel):
        drawn.append((title, xlabel, ylabel))
        return original(title, xlabel, ylabel)

    monkeypatch.setattr(plotting, "_figure", spy)
    return drawn


def test_plots_name_the_quantity_and_the_polarization(figures):
    # The target plots of optimize/evaluate/campaign said "R / T / A" on the y axis and "30°, 30" in the title.
    asyncio.run(server.evaluate_design(AR, TARGETS + [dict(TARGETS[0], polarization="30deg", value=0.012)]))
    asyncio.run(server.evaluate_design(AR, [dict(TARGETS[0], kind="T", value=0.9, inequality=">=", polarization="s"),
                                            dict(TARGETS[0], value=0.05, polarization="s")]))
    asyncio.run(server.compute_angle_scan(AR, 550, quantities=["T"]))
    asyncio.run(server.compute_monitoring_curves(AR, [550, 600], angle=20, polarization=30))
    asyncio.run(server.simulate_random_errors(AR, ["R"], {"from": 400, "to": 700, "step": 5}, angle=30,
                                              polarization=30, nb_tests=5))
    ylabels = [y for _, _, y in figures]
    assert not any(y in ("R / T / A", "R/T/A", "Signal", "Value") for y in ylabels), ylabels
    titles = [t for t, _, _ in figures]
    assert titles[0].endswith("30° incidence, polarization angle 30°")  # 30 and "30deg": one figure
    assert sum("polarization angle 30°" in t for t in titles[:1]) == 1
    assert ylabels[0] == "Reflectance" and ylabels[1] == "Reflectance / Transmittance"
    assert "Transmittance" in ylabels[2:] and any("polarization angle 30°" in t for t in titles[3:])


def test_spectrum_and_random_errors_use_the_design_wavelength_range():
    # A project's wavelength_range was ignored (380-1100 nm instead of the range the GUI analyses).
    design = dict(AR, wavelength_range={"from": 400.0, "to": 720.0, "step": 2.0})
    out = engine.spectra(design, ["R"])
    assert out["wavelengths"][0] == 400.0 and out["wavelengths"][-1] == 720.0
    r = engine.random_errors(design, ["R"], nb_tests=3)
    assert r["wavelengths"][0] == 400.0 and r["wavelengths"][-1] == 720.0
    r = engine.random_errors(AR, ["R"], nb_tests=3)  # no range: OpenFilters' own default, as its dialog
    assert (r["wavelengths"][0], r["wavelengths"][-1]) == (300.0, 1000.0)
    assert engine.spectra(AR, ["R"])["wavelengths"][-1] == 1100.0  # the connector's default otherwise


def test_random_errors_accept_sigma_like_tolerance_analysis():
    a = engine.random_errors(AR, ["R"], [550.0], thickness_error={"type": "relative", "sigma": 0.02}, nb_tests=4)
    b = engine.random_errors(AR, ["R"], [550.0], thickness_error={"type": "relative", "value": 0.02}, nb_tests=4)
    np.testing.assert_array_equal(a["series"][0]["tests"], b["series"][0]["tests"])
    with pytest.raises(engine.EngineError, match="not both"):
        engine.random_errors(AR, ["R"], [550.0], thickness_error={"value": 0.01, "sigma": 0.02}, nb_tests=4)


def test_batch_refuses_metrics_of_series_it_does_not_compute(tmp_path, monkeypatch):
    # A metric for "s" when only psi = 30 was computed failed every task after the job ran.
    template = {"design": {"substrate": "BK7", "layers": [{"material": "SiO2", "qwot": "$q"}]}, "quantities": ["R"],
                "wavelengths": [550], "angles": [45], "polarizations": [30],
                "metrics": [{"name": "R_s", "quantity": "R", "stat": "at", "wavelength": 550, "polarization": "s"}]}
    with pytest.raises(ToolError, match="polarization 's' is not computed"):
        asyncio.run(server.run_batch("spectrum", template, {"q": [0.5, 1.0]}))
    bad_angle = dict(template, metrics=[{"quantity": "R", "stat": "at", "wavelength": 550, "angle": 0}])
    with pytest.raises(ToolError, match="angle 0 is not computed"):
        asyncio.run(server.run_batch("spectrum", bad_angle, {"q": [0.5]}))
    no_wavelength = dict(template, metrics=[{"quantity": "R", "stat": "at", "polarization": "30deg"}])
    with pytest.raises(ToolError, match="needs a wavelength"):
        asyncio.run(server.run_batch("spectrum", no_wavelength, {"q": [0.5]}))
    from openfilters_mcp import tasks
    tasks.check_spectrum_metrics(dict(template, metrics=[{"quantity": "R", "stat": "at", "wavelength": 550,
                                                          "polarization": "30", "angle": 45}]))


def test_job_files_survive_concurrent_read_and_rewrite(tmp_path):
    # e2e: job_status failed with "Permission denied: ...job.json" while the job manager rewrote it
    # (Windows refuses to replace a file being read, and to open one being replaced, for a moment).
    import threading
    from openfilters_mcp import jobs
    path = str(tmp_path / "job.json")
    jobs._write_json(path, {"n": 0})
    stop, failures = threading.Event(), []

    def rewrite():
        i = 0
        try:
            while not stop.is_set():
                i += 1
                jobs._write_json(path, {"n": i, "pad": "x" * 2000})
        except Exception as error:  # noqa: BLE001 - reported below
            failures.append(error)

    writer = threading.Thread(target=rewrite)
    writer.start()
    try:
        for _ in range(2000):
            assert jobs._read_json(path)["n"] >= 0
    finally:
        stop.set()
        writer.join()
    assert not failures, failures
