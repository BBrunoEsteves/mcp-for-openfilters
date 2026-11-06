"""Regression tests from scenario Batch 3 (analysis and manufacturing, 2026-10-24)."""

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_for_openfilters import engine, jobs, plotting, server

MIRROR = {"substrate": "FusedSilica", "center_wavelength": 1064,
          "formula": {"formula": "(HL)^15 H", "materials": {"H": "TiO2", "L": "SiO2"}}}
HR_TARGET = [{"kind": "R", "from": 1000, "to": 1130, "step": 5, "value": 0.999, "inequality": ">=",
              "tolerance": 0.001}]


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path))
    engine.catalog(reload=True)


def test_sensitivity_ranks_layers_of_a_design_that_meets_its_targets():
    # S3.1: every delta_chi2 was 0 (R >= 99.9 % met with room to spare), so no layer could be ranked.
    r = engine.sensitivity(MIRROR, HR_TARGET)
    R = engine.spectra(MIRROR, ["R"], {"from": 1000, "to": 1130, "step": 5})["series"][0]["values"]
    assert r["base_chi2"] == 0 and all(row["delta_chi2"] == 0 for row in r["layers"])
    assert r["base_worst_margin"] == pytest.approx((min(R) - 0.999) / 0.001, rel=1e-4)
    assert all(row["delta_worst_margin"] < 0 for row in r["layers"])
    assert len(r["most_sensitive"]) == 5 and "note" in r
    worst = min(r["layers"], key=lambda row: row["delta_worst_margin"])
    assert r["most_sensitive"][0] == f"front #{worst['#']}"


def test_sensitivity_still_ranks_by_merit_when_it_changes():
    targets = [{"kind": "R", "from": 940, "to": 1220, "step": 5, "value": 0.999, "inequality": ">=",
                "tolerance": 0.001}]
    r = engine.sensitivity(MIRROR, targets)
    assert r["base_chi2"] > 0 and "note" not in r
    top = max(r["layers"], key=lambda row: row["delta_chi2"])
    assert r["most_sensitive"][0] == f"front #{top['#']}"


def test_monitoring_ends_keep_significant_figures():
    # S3.3: values rounded to 5 decimals made the last ~12 layers of a high reflector read 0.
    out = asyncio.run(server.compute_monitoring_curves(dict(MIRROR, consider_backside_on_monitoring=False),
                                                       [1064], mode="transmission", plot=False))
    T = engine.spectra(MIRROR, ["T"], [1064])["series"][0]["values"][0]
    last = out["signal_at_layer_ends"]["1064"][-1]
    assert T < 1e-5 and last == pytest.approx(T, rel=1e-4)


def test_campaign_marks_copies_with_equal_merit_as_duplicates():
    # S3.6: tasks 25 and 6 had chi2 285.92732 / 285.92733 but one layer 102.04 vs 100.80 nm.
    def design(*thicknesses):
        return {"layers": [{"material": m, "thickness": t} for m, t in zip(["TiO2", "SiO2"] * 10, thicknesses)]}
    a = design(110.86, 175.44, 116.92, 195.88, 105.69, 162.48, 102.77, 161.64, 96.89, 149.44,
               89.43, 143.9, 90.4, 102.04, 15.13, 40.34, 25.28, 160.5, 83.05, 47.1)
    b = design(110.8, 175.43, 116.94, 195.89, 105.65, 162.35, 102.75, 161.68, 96.9, 149.43,
               89.46, 143.83, 90.62, 100.8, 15.37, 40.33, 25.62, 160.58, 82.9, 47.22)
    assert jobs.same_design(a, b, 285.9273157716794, 285.92733)
    assert not jobs.same_design(a, b)  # without equal merits the strict tolerance applies
    assert not jobs.same_design(a, b, 285.927, 290.0)


def test_example_projects_load_by_name_and_are_read_only():
    # S3.7: resources are not reachable through some proxies, and load_project("Edge") looked in the data folder.
    for name in ("examples/Edge", "openfilters://examples/Edge", "examples/edge.ofp"):
        loaded = asyncio.run(server.load_project(name))
        assert loaded["designs"] and loaded["targets"]
    with pytest.raises(ToolError, match="examples: AR"):
        asyncio.run(server.load_project("examples/Nope"))
    with pytest.raises(ToolError, match="examples/<name>"):
        asyncio.run(server.load_project("Edge"))
    with pytest.raises(ToolError, match="read-only"):
        asyncio.run(server.save_project("examples/Edge", [MIRROR], HR_TARGET))


def test_step_method_explains_a_start_with_no_room_for_steps():
    # S3.9: a 100 nm mixture layer collapsed to one homogeneous layer at the lowest index, reported only as
    # "converged". A thicker start reaches the target, so it is a local optimum, not a material limit.
    targets = [{"kind": "R", "from": 450, "to": 650, "step": 5, "value": 0.005, "inequality": "<="}]
    thin = {"substrate": "BK7", "center_wavelength": 550,
            "layers": [{"material": "SiO2TiO2", "thickness": 100, "index": 1.9}]}
    r = engine.optimize(thin, targets, "step")
    assert len(r["design"]["layers"]) == 1
    assert any("added no steps" in w and "lowest index" in w for w in r["warnings"])
    thick = engine.optimize(dict(thin, layers=[dict(thin["layers"][0], thickness=500)]), targets, "step")
    assert len(thick["design"]["layers"]) > 1
    assert thick["targets"][0]["worst_value"] == pytest.approx(0.005, abs=1e-6)
    assert not any("added no steps" in w for w in thick.get("warnings", []))


def test_angle_plot_draws_s_and_p_at_seven_angles(monkeypatch):
    # S3.4: 14 series (7 angles, s and p) were cut to 8, with a note over the axis label.
    drawn = []
    monkeypatch.setattr(plotting, "_png", lambda fig, ax, n, legend_title=None: drawn.append((len(ax.lines), legend_title)))
    design = {"substrate": "BK7", "center_wavelength": 532,
              "formula": {"formula": "(HL)^4 HH (LH)^4", "materials": {"H": "TiO2", "L": "SiO2"}}}
    r = engine.spectra(design, ["T"], {"from": 520, "to": 540, "step": 1}, angles=[0, 5, 10, 15, 20, 25, 30],
                       polarizations=("s", "p"))
    plotting.spectra(r)
    assert drawn == [(14, None)]
