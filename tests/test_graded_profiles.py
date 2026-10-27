"""Graded-profile generator (phase 2 of the rugate / graded-layer report of 2026-10-26, item F1).

Acceptance criterion 8 of the report: a rugate made by the generator (native resolution, Hann apodization,
quintic matching) has no peaks above 1 % at lambda0/2 or lambda0/3, side lobes clearly below those of the
report's C3 (about 53 % at 1270 nm), and warns about the mismatch left against air and fused silica (outside
IdealMixture's 1.5-2.4).
"""

import numpy as np
import pytest

from openfilters_mcp import engine

BASE = {"substrate": "FusedSilica", "incident_medium": "void", "center_wavelength": 1064}
C3_SIDE_LOBE = 0.53
AIR_FLOOR = 0.04  # air / n = 1.5 interface left unmatched (IdealMixture stops at 1.5)


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFILTERS_MCP_HOME", str(tmp_path))
    engine.catalog(reload=True)


def rugate(apodization=None, matching=None, resolution=None, **profile):
    graded = {"profile": {"type": "rugate", "n_min": 1.5, "n_max": 2.2, "periods": 16, **profile}}
    if apodization:
        graded["profile"]["apodization"] = apodization
    if matching:
        graded["matching"] = matching
    if resolution:
        graded["resolution"] = resolution
    return dict(BASE, layers=[{"material": "IdealMixture", "graded": graded}])


def spectrum(design, start=300, stop=1600, step=1):
    s = engine.spectra(design, ["R"], {"from": start, "to": stop, "step": step})
    return np.asarray(s["wavelengths"]), np.asarray(s["series"][0]["values"])


def side_lobes(x, r):
    """Local maxima within 250 nm of the stop band (R above half its peak), outside it."""
    band = x[r >= r.max() / 2]
    lo, hi = band.min(), band.max()
    return [r[i] for i in range(1, len(r) - 1)
            if r[i] >= r[i - 1] and r[i] > r[i + 1] and (lo - 250 < x[i] < lo or hi < x[i] < hi + 250)]


def expanded(design):
    out, warnings = engine.expand_profiles(design)
    return out["layers"], warnings


def test_criterion_8_hann_rugate_with_quintic_matching():
    design = rugate({"type": "hann"}, {"substrate": "quintic", "medium": "quintic"})
    x, r = spectrum(design)
    assert r[x == 1064][0] > 0.95
    for lo, hi in ((500, 570), (340, 375)):  # lambda0/2, lambda0/3: only the air interface's ~4 % (no harmonic)
        assert r[(x >= lo) & (x <= hi)].max() < AIR_FLOOR + 0.01
    assert max(side_lobes(x, r)) < AIR_FLOOR + 0.01 < C3_SIDE_LOBE
    warnings = engine.material_warnings(design, [1064])
    assert any("FusedSilica" in w and "unmatched by 0.0478" in w for w in warnings)
    assert any("void" in w and "unmatched by 0.5000" in w and "R = 4 %" in w for w in warnings)


def test_criterion_8_hann_taper_matched_to_the_substrate_only():
    x, r = spectrum(rugate({"type": "hann", "taper_periods": 4}, {"substrate": "quintic"}))
    assert r[x == 1064][0] > 0.99
    assert max(side_lobes(x, r)) < C3_SIDE_LOBE - 0.05


def test_ln_n_in_optical_thickness_has_no_second_harmonic_but_n_in_physical_thickness_does():
    matching = {"substrate": "quintic", "medium": "quintic"}
    x, good = spectrum(rugate("none", matching), 500, 570, 0.5)
    _, naive = spectrum(rugate("none", matching, modulate="n", coordinate="physical"), 500, 570, 0.5)
    assert good.max() < AIR_FLOOR + 0.05
    assert naive.max() > 0.3


def test_steps_have_exact_optical_thickness_and_are_merged():
    layers, _ = expanded(rugate({"type": "hann"}, {"substrate": "quintic", "medium": "quintic", "periods": 2}))
    graded = layers[0]["graded"]
    steps, thicknesses = graded["steps"], graded["step_thicknesses"]
    n = engine.mixture_steps("IdealMixture", 1064)
    optical = sum(n[k] * d for k, d in zip(steps, thicknesses))
    assert optical == pytest.approx((16 + 2 + 2) * 1064 / 2, rel=1e-9)
    assert all(a != b for a, b in zip(steps, steps[1:]))
    assert min(steps) == 0 and max(steps) == 70  # 1.5 and 2.2
    assert "thickness" not in layers[0]


def test_coarser_resolution_matches_the_reports_c4():
    """The report's own script, q = 5: 337 steps, R 0.99601 at 1047 nm. The generator uses the rounded index
    for each step's physical thickness (the report's note), so its spectrum is close, not identical."""
    layers, _ = expanded(rugate({"type": "sin2", "taper_periods": 4}, resolution=0.05))
    steps = layers[0]["graded"]["steps"]
    assert all(k % 5 == 0 for k in steps)
    assert abs(len(steps) - 337) <= 5
    x, r = spectrum(rugate({"type": "sin2", "taper_periods": 4}, resolution=0.05), 900, 1200, 0.5)
    assert r.max() == pytest.approx(0.99601, abs=5e-4)
    band = engine.crossing_ranges(x, r, 0.99)
    assert band[0][0] == pytest.approx(993.70, abs=1.5) and band[-1][1] == pytest.approx(1130.70, abs=1.5)


def test_unmatched_rugate_suggests_matching():
    warnings = engine.material_warnings(rugate({"type": "hann"}), [1064])
    assert any("void" in w and "matching" in w for w in warnings)


@pytest.mark.parametrize("shape", ["quintic", "linear"])
def test_ramps(shape):
    design = dict(BASE, layers=[{"material": "IdealMixture", "graded": {"profile": {
        "type": shape, "n_start": 2.2, "n_end": 1.5, "optical_thickness": 1064}}}])
    layers, _ = expanded(design)
    steps = layers[0]["graded"]["steps"]
    assert steps[0] == 70 and steps[-1] == 0 and steps == sorted(steps, reverse=True)
    assert len(steps) == 71
    thicknesses = layers[0]["graded"]["step_thicknesses"]
    if shape == "quintic":  # flat ends: the first and last steps are much thicker than the middle ones
        assert thicknesses[0] > 5 * thicknesses[35]
    describe = engine.describe(design)
    assert describe["layers"][0]["n_first@cw"] == 2.2 and describe["layers"][0]["n_last@cw"] == 1.5


def test_physical_ramp_and_custom_profile_thickness():
    ramp = dict(BASE, layers=[{"material": "IdealMixture", "graded": {"profile": {
        "type": "linear", "n_start": 1.6, "n_end": 2.0, "thickness": 300}}}])
    assert sum(expanded(ramp)[0][0]["graded"]["step_thicknesses"]) == pytest.approx(300)
    custom = dict(BASE, layers=[{"material": "IdealMixture", "graded": {"profile": {
        "type": "custom", "points": [[0, 1.6], [100, 2.0], [250, 2.0], [400, 1.7]]}}}])
    layers, _ = expanded(custom)
    assert sum(layers[0]["graded"]["step_thicknesses"]) == pytest.approx(400)
    assert max(layers[0]["graded"]["steps"]) == 50


def test_profiles_work_in_every_layer_position():
    """Matching uses what is next to the profile: a homogeneous layer, the substrate or the medium."""
    design = dict(BASE, layers=[{"material": "IdealMixture", "index": 1.9, "thickness": 100},
                                {"material": "IdealMixture", "graded": {
                                    "profile": {"type": "rugate", "n_min": 1.7, "n_max": 2.1, "periods": 4},
                                    "matching": {"substrate": "linear"}}},
                                {"material": "SiO2", "thickness": 100}])
    layers, warnings = expanded(design)
    assert layers[1]["graded"]["steps"][0] == 40 and not warnings  # ramp from the 1.9 layer below
    assert engine.describe(design)["nb_layers"] == 3


def test_adjacent_profiles_without_matching():
    """Found in a chat check (2026-10-27): a ramp next to a custom profile was refused because the rugate ripple
    hint looked up both neighbours of every profile layer."""
    design = dict(BASE, center_wavelength=550, layers=[
        {"material": "IdealMixture", "graded": {"profile": {"type": "quintic", "n_start": 1.5, "n_end": 2.4,
                                                            "optical_thickness": 550}}},
        {"material": "IdealMixture", "graded": {"profile": {"type": "custom", "points": [[0, 2.4], [100, 2.0],
                                                                                         [200, 2.0], [300, 1.6]]}}},
        {"material": "IdealMixture", "graded": {"profile": {"type": "rugate", "n_min": 1.6, "n_max": 2.0,
                                                            "periods": 4}}}])
    layers, warnings = expanded(design)
    assert [l["graded"]["steps"][0] for l in layers] == [0, 90, 29]  # the rugate starts at the geometric mean, 1.789
    assert engine.describe(design)["nb_layers"] == 3
    assert any("void" in w for w in warnings) and any("next to layer 2 (IdealMixture) (n = 1.6000)" in w for w in warnings)


def test_matching_to_a_neighbouring_profile():
    """Profiles are expanded from the substrate outward: one below is already steps and can be matched to (its
    last step); one above cannot."""
    ramp = {"material": "IdealMixture", "graded": {"profile": {"type": "linear", "n_start": 1.6, "n_end": 2.0, "qwot": 1}}}
    rugate = {"profile": {"type": "rugate", "n_min": 1.6, "n_max": 2.0, "periods": 4}}
    layers, _ = expanded(dict(BASE, layers=[ramp, {"material": "IdealMixture",
                                                   "graded": dict(rugate, matching={"substrate": "quintic"})}]))
    assert layers[1]["graded"]["steps"][0] == 50  # from the ramp's last index, 2.0
    with pytest.raises(engine.EngineError, match="layer 2 is a profile too"):
        expanded(dict(BASE, layers=[{"material": "IdealMixture", "graded": dict(rugate, matching={"medium": "quintic"})},
                                    ramp]))


def test_rounding_warning_names_the_layer():
    design = dict(BASE, layers=[{"material": "IdealMixture",
                                 "graded": {"indices_at_cw": [1.503, 2.2149], "step_thicknesses": [100, 100]}}])
    assert any(w.startswith("layer 1: indices_at_cw rounded") for w in engine.material_warnings(design, [1064]))


@pytest.mark.parametrize("graded, message", [
    ({"profile": {"type": "rugate", "n_min": 1.4, "n_max": 2.2, "periods": 4}}, "covers 1.5000-2.4000"),
    ({"profile": {"type": "rugate", "n_min": 1.5, "n_max": 2.2}}, "needs 'periods'"),
    ({"profile": {"type": "wave", "periods": 4}}, "one of rugate"),
    ({"profile": {"type": "rugate", "n_min": 1.5, "n_max": 2.2, "periods": 4, "taper": 2}}, "unknown key 'taper'"),
    ({"profile": {"type": "rugate", "n_min": 1.5, "n_max": 2.2, "periods": 4, "apodization": "kaiser"}}, "apodization type"),
    ({"profile": {"type": "quintic", "n_start": 1.5, "n_end": 2.2}}, "needs one of thickness"),
    ({"profile": {"type": "custom", "points": [[0, 1.6]]}}, "at least two"),
    ({"profile": {"type": "linear", "n_start": 1.5, "n_end": 2.2, "qwot": 1}, "matching": {"top": "quintic"}}, "unknown key 'top'"),
    ({"profile": {"type": "linear", "n_start": 1.5, "n_end": 2.2, "qwot": 1}, "resolution": "fine"}, "resolution"),
])
def test_bad_profiles_are_refused(graded, message):
    with pytest.raises(engine.EngineError, match=message):
        engine.describe(dict(BASE, layers=[{"material": "IdealMixture", "graded": graded}]))


def test_a_profile_needs_a_mixture():
    with pytest.raises(engine.EngineError, match="not a mixture"):
        engine.describe(dict(BASE, layers=[{"material": "TiO2", "graded": {
            "profile": {"type": "linear", "n_start": 1.5, "n_end": 2.2, "qwot": 1}}}]))


def test_generated_designs_survive_a_save_and_reload(tmp_path):
    design = rugate({"type": "hann"}, {"substrate": "quintic", "medium": "quintic"})
    path = engine.save_project(str(tmp_path / "rugate.ofp"), [design])
    again = engine.load_project(path)["designs"][0]
    assert again["layers"][0]["graded"]["steps"] == expanded(design)[0][0]["graded"]["steps"]
    assert spectrum(again, 900, 1200, 5)[1] == pytest.approx(spectrum(design, 900, 1200, 5)[1], abs=1e-6)
