"""Regression tests from the field report of 2026-10-26 (tunable Fabry-Perot at 532 nm with an ITO heater).

Item numbers (B1, B2, ...) are those of the report.
"""

import pytest

from openfilters_core import project as of_project
from openfilters_mcp import engine

TIO2_TABLE = [[520, 2.37248, 6e-06], [526, 2.36666, 5e-06], [532, 2.36111, 4e-06], [538, 2.35583, 3e-06],
              [544, 2.35079, 2e-06]]


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFILTERS_MCP_HOME", str(tmp_path))
    engine.catalog(reload=True)


def test_b1_tabulated_material_is_held_constant_beyond_its_data():
    # A 520-544 nm table gave n = 4.90 at 1550 nm and 2.55 at 400 nm (PCHIP end segments extrapolated).
    engine.add_material("tTiO2_0", "table", table=TIO2_TABLE)
    N = engine.material_N("tTiO2_0", [400.0, 500.0, 520.0, 532.0, 544.0, 600.0, 1550.0])
    assert N[2].real == pytest.approx(2.37248) and N[3].real == pytest.approx(2.36111)  # data unchanged
    assert N[4].real == pytest.approx(2.35079)
    assert [z.real for z in N[:2]] == pytest.approx([2.37248] * 2)  # held at the first point
    assert [z.real for z in N[5:]] == pytest.approx([2.35079] * 2)  # held at the last point
    assert [-z.imag for z in N[5:]] == pytest.approx([2e-06] * 2)


def test_b1_interpolation_within_the_data_is_unchanged():
    # The padding points only flatten what lies beyond the data: between the data points the
    # interpolation is the same PCHIP as before, except in the end intervals, which stay monotonic.
    engine.add_material("tTiO2_0", "table", table=TIO2_TABLE)
    n = [z.real for z in engine.material_N("tTiO2_0", [523.0, 529.0, 535.0, 541.0])]
    assert all(a > b for a, b in zip(n, n[1:]))
    assert 2.36666 < n[0] < 2.37248 and 2.35079 < n[3] < 2.35583


def test_b1_calculations_warn_near_the_data_and_refuse_far_beyond_it():
    engine.add_material("tTiO2_0", "table", table=TIO2_TABLE)
    design = {"substrate": "FusedSilica", "layers": [{"material": "tTiO2_0", "thickness": 100}]}
    near = engine.spectra(design, ["T"], [532, 700])  # 700 nm: 1.29x beyond 544 nm
    assert any("held at the value" in w for w in near["warnings"])
    with pytest.raises(engine.EngineError, match="beyond the data"):
        engine.spectra(design, ["T"], [532, 1550])
    with pytest.raises(engine.EngineError, match="beyond the data"):
        engine.optimize(design, [{"kind": "T", "wavelength": 1550, "value": 1}], "refine", {"time_limit": 5})
    for result in (engine.monitoring(design, [700.0]), engine.electric_field(design, 700.0)):
        assert any("held at the value" in w for w in result["warnings"])


def test_b1_material_summary_says_values_are_held():
    row = engine.add_material("tTiO2_0", "table", table=TIO2_TABLE)
    assert row["n,k"]["1550"][0] == pytest.approx(2.35079, abs=1e-4)
    assert "held at the value" in row["note"]


def test_b2_project_text_outside_latin1_is_spelled_out():
    # "dλ/dT" in a description failed with a bare "Error executing tool save_project" on Windows.
    design = {"substrate": "BK7", "description": "teste dλ/dT Δn — µ ° ç “x” ≤ 1",
              "layers": [{"material": "TiO2", "thickness": 100}]}
    path = engine.save_project(engine.settings.projects_dir() + "/b2.ofp", [design],
                               [{"kind": "T", "wavelength": 532, "value": 1}], "comentário λ/4 → Ω")
    raw = open(path, "rb").read()
    assert b"teste dlambda/dT Deltan - \xb5 \xb0 \xe7 \"x\" <= 1" in raw  # Latin-1 bytes
    assert b"coment\xe1rio lambda/4 -> Omega" in raw
    loaded = engine.load_project(path)
    assert loaded["designs"][0]["description"] == "teste dlambda/dT Deltan - µ ° ç \"x\" <= 1"
    assert loaded["comment"].strip() == "comentário lambda/4 -> Omega"


def test_b2_core_writer_uses_latin1_whatever_the_locale(tmp_path):
    # The writer used the OS locale (cp1252 on Windows, UTF-8 elsewhere); the reader uses Latin-1.
    p = of_project.project(engine.catalog())
    p.set_comment("é λ")
    path = str(tmp_path / "core.ofp")
    of_project.write_project(p, path)
    raw = open(path, "rb").read()
    assert b"\xe9 ?" in raw and b"\xc3\xa9" not in raw


@pytest.mark.parametrize("text, expected", [("λ/4", "lambda/4"), ("Σσς", "Sigmasigmasigma"), ("α–β", "alpha-beta"),
                                            ("µm and μm", "µm and µm"), ("☃", "?"), ("é ° ±", "é ° ±")])
def test_b2_latin1_text(text, expected):
    assert engine.latin1_text(text) == expected


ITO_TABLE = [[400, 2.0438, 0.01135], [550, 1.8504, 0.00861], [800, 1.5526, 0.0287]]  # the report's ITO at 3 points
TARGETS_STD = [
    {"kind": "T", "wavelength": 532, "value": 0.97, "inequality": ">=", "tolerance": 0.005},
    {"kind": "T", "from": 480, "to": 522, "step": 2, "value": 0.02, "inequality": "<=", "tolerance": 0.01},
    {"kind": "T", "from": 542, "to": 600, "step": 2, "value": 0.02, "inequality": "<=", "tolerance": 0.01}]


def fabry_perot_with_ito(fixed):
    """The report's design 2.3: ITO(QW) (HL)^5 H 2L H (LH)^5 ITO(QW) on fused silica."""
    stack = engine.describe({"substrate": "FusedSilica", "center_wavelength": 532, "formula": {
        "formula": "(HL)^5 H LL H (LH)^5", "materials": {"H": "TiO2", "L": "SiO2"}}})["design"]["layers"]
    ito = {"material": "ITO_t", "thickness": 71.105715, **({"fixed": True} if fixed else {})}
    return {"substrate": "FusedSilica", "center_wavelength": 532, "layers": [ito] + stack + [dict(ito)]}


def test_b4_fixed_layers_keep_their_thickness():
    # The refine clamped the bottom ITO (an electrode) to min_thickness = 20 nm.
    engine.add_material("ITO_t", "table", table=ITO_TABLE)
    r = engine.optimize(fabry_perot_with_ito(fixed=True), TARGETS_STD, "refine",
                        {"time_limit": 30, "min_thickness": 20})
    layers = r["design"]["layers"]
    assert layers[0]["thickness"] == 71.105715 and layers[-1]["thickness"] == 71.105715
    assert layers[0]["refine_thickness"] is False and layers[-1]["refine_thickness"] is False
    free = engine.optimize(fabry_perot_with_ito(fixed=False), TARGETS_STD, "refine",
                           {"time_limit": 30, "min_thickness": 20})
    assert free["design"]["layers"][0]["thickness"] != pytest.approx(71.105715)  # the test would catch a no-op


def test_b4_fixed_symbol_in_a_formula():
    engine.add_material("ITO_t", "table", table=ITO_TABLE)
    design = {"substrate": "FusedSilica", "center_wavelength": 532, "formula": {
        "formula": "(HL)^5 H LL H (LH)^5 I",
        "materials": {"H": "TiO2", "L": "SiO2", "I": {"material": "ITO_t", "fixed": True}}}}
    start = engine.describe(design)["design"]["layers"]
    assert start[-1]["material"] == "ITO_t" and start[-1]["refine_thickness"] is False
    r = engine.optimize(design, TARGETS_STD, "refine", {"time_limit": 30, "min_thickness": 20})
    assert r["design"]["layers"][-1]["thickness"] == pytest.approx(start[-1]["thickness"], abs=1e-9)
    assert r["design"]["layers"][-2]["thickness"] != pytest.approx(start[-2]["thickness"])


def test_b4_a_material_cannot_be_both_fixed_and_free_in_a_formula():
    design = {"substrate": "BK7", "formula": {"formula": "HLA", "materials": {
        "H": "TiO2", "L": "SiO2", "A": {"material": "SiO2", "fixed": True}}}}
    with pytest.raises(engine.EngineError, match="fixed symbol and by a free one"):
        engine.describe(design)


def test_b4_fixed_layer_next_to_the_same_material_is_reported():
    design = {"substrate": "BK7", "layers": [{"material": "SiO2", "thickness": 75, "fixed": True},
                                             {"material": "SiO2", "thickness": 90}, {"material": "TiO2", "thickness": 60}]}
    r = engine.optimize(design, [{"kind": "R", "wavelength": 550, "value": 0}], "refine", {"time_limit": 10})
    assert any("one fixed and one not" in w for w in r["warnings"])


def test_b4_min_thickness_leaves_a_thin_fixed_layer_alone():
    layers = [{"material": "TiO2", "thickness": 10, "fixed": True}, {"material": "SiO2", "thickness": 90},
              {"material": "TiO2", "thickness": 60}, {"material": "SiO2", "thickness": 3}]
    r = engine.optimize({"substrate": "BK7", "layers": layers},
                        [{"kind": "R", "from": 500, "to": 600, "step": 10, "value": 0, "tolerance": 0.01}],
                        "refine", {"time_limit": 30, "min_thickness": 20})
    out = r["design"]["layers"]
    assert out[0]["thickness"] == 10 and out[0]["refine_thickness"] is False
    assert all(l["thickness"] >= 20 - 1e-6 for l in out[1:])


def test_b3_half_max_near_picks_the_resonance_not_the_band_edge_ripple():
    # (HL)^5 H LL H (LH)^5 I over 440-640 nm: the band-edge ripple near 445 nm is higher than the
    # resonance at 532 nm, and {"half_max": true} measured the ripple (FWHM 11.1 nm).
    engine.add_material("ITO_t", "table", table=ITO_TABLE)
    design = {"substrate": "FusedSilica", "center_wavelength": 532, "formula": {
        "formula": "(HL)^5 H LL H (LH)^5 I", "materials": {"H": "TiO2", "L": "SiO2", "I": "ITO_t"}}}
    s = engine.spectra(design, ["T"], {"from": 440, "to": 640, "step": 0.05})
    wl, T = s["wavelengths"], s["series"][0]["values"]
    assert engine.half_max_summary(wl, T, {"half_max": True})["peak_nm"] < 460  # the old behaviour: highest point
    peak = engine.half_max_summary(wl, T, {"half_max": True, "near": 532})
    assert peak["peak_nm"] == pytest.approx(532, abs=0.5) and 0.5 < peak["fwhm"] < 3
    assert peak["half_max_center_nm"] == pytest.approx(532, abs=0.5)
    window = engine.half_max_summary(wl, T, {"half_max": True, "from": 525, "to": 539})
    assert window["peak_nm"] == peak["peak_nm"] and window["fwhm"] == peak["fwhm"]


def test_b7_a_clipped_half_max_edge_is_reported():
    # The upper edge sat at the window limit and the FWHM was returned as if valid.
    x = [530 + 0.01 * i for i in range(701)]  # 530-537 nm
    y = [1 / (1 + ((w - 536.5) / 0.6) ** 2) for w in x]  # Lorentzian at 536.5 nm, FWHM 1.2 nm: edge at 537.1
    out = engine.half_max_summary(x, y, {"half_max": True})
    assert out["fwhm"] is None and out["half_max_edge_clipped"] == ["high"]
    assert out["fwhm_at_least"] == pytest.approx(537 - 535.9, abs=0.01)
    whole = engine.half_max_summary([525 + 0.01 * i for i in range(1501)],
                                    [1 / (1 + ((525 + 0.01 * i - 536.5) / 0.6) ** 2) for i in range(1501)], {"half_max": True})
    assert whole["fwhm"] == pytest.approx(1.2, abs=0.01) and "half_max_edge_clipped" not in whole


def test_b6_an_unreachable_target_is_reported():
    # T = 1 "equal" at 532 nm with absorbing ITO ran 32 609 iterations until the time limit.
    engine.add_material("ITO_t", "table", table=ITO_TABLE)
    targets = [{"kind": "T", "wavelength": 532, "value": 1.0, "inequality": "equal", "tolerance": 0.01}]
    r = engine.optimize(fabry_perot_with_ito(fixed=True), targets, "refine", {"time_limit": 3})
    (warning,) = [w for w in r["warnings"] if "cannot be met" in w]
    assert "ITO_t" in warning and "T >= 0.9" in warning
    # TiO2's tiny absorption (k = 4e-6) does not matter at the tolerance: no warning for a mirror.
    mirror = {"substrate": "BK7", "center_wavelength": 532,
              "formula": {"formula": "(HL)^6 H", "materials": {"H": "TiO2", "L": "SiO2"}}}
    r = engine.optimize(mirror, [{"kind": "R", "wavelength": 532, "value": 1.0}], "refine", {"time_limit": 3})
    assert not any("cannot be met" in w for w in r.get("warnings", []))


def test_b5_a_start_at_the_thickness_limit_is_reported():
    # (HL)^5 H LLLL H (LH)^5 I starts at 1999.8 nm with max_total_thickness 2000: 7 iterations, no progress.
    engine.add_material("ITO_t", "table", table=ITO_TABLE)
    design = {"substrate": "FusedSilica", "center_wavelength": 532, "formula": {
        "formula": "(HL)^5 H LLLL H (LH)^5 I", "materials": {"H": "TiO2", "L": "SiO2", "I": "ITO_t"}}}
    r = engine.optimize(design, TARGETS_STD, "refine", {"time_limit": 10, "max_total_thickness": 2000})
    assert any("at max_total_thickness" in w for w in r["warnings"])


def test_best_design_snapshot_keeps_layer_flags():
    # The best design seen during an optimization was recorded without flags, so a fixed layer
    # became free in the next round (min_thickness rounds, further needle passes).
    design = {"substrate": "BK7", "layers": [{"material": "TiO2", "thickness": 40, "fixed": True},
                                             {"material": "SiO2", "thickness": 90}]}
    o = engine.METHODS["needle"](engine.build_filter(design), engine.build_targets(
        [{"kind": "R", "wavelength": 550, "value": 0}]))
    assert engine._snapshot(o) == [{"material": "TiO2", "thickness": 40.0, "refine_thickness": False,
                                    "add_needles": False}, {"material": "SiO2", "thickness": 90.0}]


def test_b8_batch_peak_metrics():
    # The thermal analysis needed 27 separate compute_spectrum calls: run_batch had no peak metrics.
    from openfilters_mcp import tasks
    engine.add_material("ITO_t", "table", table=ITO_TABLE)
    design = {"substrate": "FusedSilica", "center_wavelength": 532, "formula": {
        "formula": "(HL)^5 H LL H (LH)^5 I", "materials": {"H": "TiO2", "L": "SiO2", "I": "ITO_t"}}}
    result = engine.spectra(design, ["T"], {"from": 440, "to": 640, "step": 0.05})
    metrics = [{"name": s, "quantity": "T", "stat": s, "near": 532}
               for s in ("peak_wavelength", "peak_value", "fwhm", "center")]
    row = tasks.spectrum_metrics(result, metrics)
    peak = engine.half_max_summary(result["wavelengths"], result["series"][0]["values"], {"near": 532})
    assert row == {"peak_wavelength": peak["peak_nm"], "peak_value": peak["peak_value"], "fwhm": peak["fwhm"],
                   "center": peak["half_max_center_nm"]}
    with pytest.raises(engine.EngineError, match="stat must be one of"):
        tasks.spectrum_metrics(result, [{"quantity": "T", "stat": "median"}])


def test_b8_batch_can_sweep_whole_designs():
    from openfilters_mcp import server
    designs = [{"name": "order 1", "substrate": "BK7", "layers": [{"material": "TiO2", "thickness": 60}]},
               {"substrate": "BK7", "layers": [{"material": "SiO2", "thickness": 90}]}]
    cases = server._expand({"design": "$design", "quantities": ["T"]}, {"design": designs}, "grid")
    assert [params for params, _ in cases] == [{"design": "order 1"}, {"design": "design 2"}]
    assert [args["design"] for _, args in cases] == designs


# Verification report (same day, after the fixes above): N1, N3.

def test_n3_removed_layers_are_named_and_reach_job_rows():
    # A heater layer disappeared and the only sign was the layer count (25 -> 24).
    from openfilters_mcp import jobs
    design = {"substrate": "BK7", "center_wavelength": 550,
              "formula": {"formula": "0.25H 0.25L H L", "materials": {"H": "TiO2", "L": "SiO2"}}}
    targets = [{"kind": "R", "from": 420, "to": 680, "step": 10, "value": 0, "tolerance": 0.01}]
    r = engine.optimize(design, targets, "refine", {"time_limit": 30, "min_thickness": 30})
    assert r["thin_layers_removed"] == len(r["removed_layers"]) == 2 and r["nb_layers"] == 2
    first = r["removed_layers"][0]
    assert first == {"position": 1, "material": "TiO2", "thickness_nm": 30.0}
    assert any("were removed because the merit was no worse" in w and first["material"] in w for w in r["warnings"])
    row = jobs.result_row(0, "optimize", {"ok": True, "result": r})
    assert row["thin_layers_removed"] == r["thin_layers_removed"] and row["warnings"] == r["warnings"]


def test_n1_server_info_reports_the_build():
    import asyncio
    from openfilters_mcp import server
    info = asyncio.run(server.server_info())
    assert info["build"] and info["started"]  # a git commit in a checkout, "release" when installed


def test_b4_fixed_layers_with_the_needle_and_step_methods():
    # The report only tried refine. Needle: a fixed cavity stays while needles go elsewhere and thin
    # layers are removed. Step: a fixed mixture layer keeps its thickness and index and gets no steps.
    stack = engine.describe({"substrate": "FusedSilica", "center_wavelength": 532, "formula": {
        "formula": "(HL)^4 H LL H (LH)^4", "materials": {"H": "TiO2", "L": "SiO2"}}})["design"]["layers"]
    cavity = len(stack) // 2
    stack[cavity] = dict(stack[cavity], fixed=True)
    r = engine.optimize({"substrate": "FusedSilica", "center_wavelength": 532, "layers": stack}, TARGETS_STD,
                        "needle", {"time_limit": 30, "min_thickness": 20})
    fixed = [l for l in r["design"]["layers"] if l.get("refine_thickness") is False]
    assert [l["thickness"] for l in fixed] == [stack[cavity]["thickness"]]
    mixture = {"substrate": "BK7", "center_wavelength": 550, "layers": [
        {"material": "SiO2TiO2", "thickness": 100, "index": 1.7, "fixed": True},
        {"material": "SiO2TiO2", "thickness": 300, "index": 1.9}]}
    r = engine.optimize(mixture, [{"kind": "R", "from": 450, "to": 650, "step": 5, "value": 0.005, "inequality": "<="}],
                        "step", {"time_limit": 30})
    first = r["design"]["layers"][0]
    assert (first["thickness"], first["index"]) == (100.0, 1.7) and not first.get("graded")
    assert first["refine_index"] is False and first["add_steps"] is False
