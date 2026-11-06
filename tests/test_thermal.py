"""Thermal model (F1 of the field report of 2026-10-26): peak sensitivities and d(lambda)/dT.

Reference values: section 2.7 of that report, which emulated the model by hand with perturbed materials
(+0.01 RIU and +1 % thickness, forward differences, 0.002 nm grid) on the designs of its Appendix A.
"""

import pytest

from mcp_for_openfilters import engine, thermal

# The report's ITO (Annex B, 520-544 nm); ITO sits at a node of the field and barely moves the peak.
ITO_TABLE = [[520, 1.88396, 0.007682], [526, 1.87719, 0.007843], [532, 1.87045, 0.008011],
             [538, 1.86375, 0.008198], [544, 1.85707, 0.008399]]


def report_design(thicknesses):
    """Alternating TiO2/SiO2 from the substrate, ITO on top (the report's Appendix A layout)."""
    layers = [{"material": "TiO2" if i % 2 == 0 else "SiO2", "thickness": t} for i, t in enumerate(thicknesses[:-1])]
    return {"substrate": "FusedSilica", "center_wavelength": 532,
            "layers": layers + [{"material": "tITO_0", "thickness": thicknesses[-1]}]}


SIO2_ORD1 = report_design([66.554731, 100.624374, 62.337858, 98.109086, 61.049166, 94.783283, 57.943325, 89.52387,
                           56.016644, 89.057285, 56.147355, 178.770904, 56.146472, 89.038281, 55.92616, 88.626433,
                           55.723781, 88.707601, 56.135691, 89.401047, 56.503867, 116.948069, 61.167846, 75.038694])
TIO2_ORD3 = report_design([67.18588, 101.446075, 62.734842, 98.383652, 60.767096, 93.643092, 56.514175, 88.889226,
                           55.992658, 89.108332, 337.818229, 89.106184, 55.974549, 88.674114, 55.666329, 88.335999,
                           55.789165, 89.175, 56.404466, 117.449956, 61.406902, 75.106695])


def report_thermal(tio2_dn_dT):
    return {"materials": {"SiO2": {"dn_dT": 1e-5, "cte": 0.5e-6, "poisson": 0.17},
                          "TiO2": {"dn_dT": tio2_dn_dT, "cte": 5e-6, "poisson": 0.27}},
            "substrate_cte": 0.55e-6}


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path))
    engine.catalog(reload=True)
    engine.add_material("tITO_0", "table", table=ITO_TABLE)


@pytest.mark.parametrize("design, fwhm, sensitivities, rates", [
    # report: FWHM; dl/dn SiO2, TiO2 (nm/RIU); dl/dstrain SiO2, TiO2 (nm); dl/dT (pm/K) at TiO2 dn/dT
    # -2.2e-4, -1e-4, -0.5e-4, 0
    (SIO2_ORD1, 1.55, [204, 77, 298, 187], [-13.10, -3.92, -0.09, 3.74]),
    (TIO2_ORD3, 1.33, [72, 149, 101, 356], [-29.17, -11.23, -3.76, 3.72]),
])
def test_sensitivity_reproduces_the_report(design, fwhm, sensitivities, rates):
    s = thermal.sensitivity(design, near=532)
    assert s["peak"]["fwhm_nm"] == pytest.approx(fwhm, abs=0.01)
    assert s["peak"]["center_nm"] == pytest.approx(532, abs=0.01)
    m = s["materials"]
    got = [m["SiO2"]["dlambda_dn_nm_per_RIU"], m["TiO2"]["dlambda_dn_nm_per_RIU"],
           m["SiO2"]["dlambda_dstrain_nm"], m["TiO2"]["dlambda_dstrain_nm"]]
    assert got == pytest.approx(sensitivities, abs=1.5)  # the report: forward differences, 0.01 RIU and 1 %
    assert abs(m["tITO_0"]["dlambda_dn_nm_per_RIU"]) < 2  # at a node of the field
    for tio2, rate in zip((-2.2e-4, -1e-4, -0.5e-4, 0.0), rates):
        r = thermal.sensitivity(design, near=532, thermal=report_thermal(tio2))
        assert r["dlambda_dT_pm_per_K"] == pytest.approx(rate, abs=0.3)
        contributions = sum(row["contribution_pm_per_K"] for row in r["materials"].values())
        assert contributions == pytest.approx(r["dlambda_dT_pm_per_K"], abs=1e-3)
    assert any("tITO_0" in w for w in r["warnings"])  # no thermal properties given for it


def test_athermal_dn_dT_and_kelvin_per_fwhm():
    # Report: TiO2 cavity order 3 is athermal at TiO2 dn/dT = -2.5e-5 and needs 46 K per FWHM at -2.2e-4.
    r = thermal.sensitivity(TIO2_ORD3, near=532, thermal=report_thermal(-2.2e-4))
    assert r["materials"]["TiO2"]["athermal_dn_dT"] == pytest.approx(-2.5e-5, abs=0.1e-5)
    assert r["kelvin_per_fwhm"] == pytest.approx(46, abs=1)
    assert "athermal_dn_dT" not in r["materials"]["tITO_0"]  # barely moves the peak: not meaningful
    athermal = report_thermal(r["materials"]["TiO2"]["athermal_dn_dT"])
    assert abs(thermal.sensitivity(TIO2_ORD3, near=532, thermal=athermal)["dlambda_dT_pm_per_K"]) < 0.1


def test_sweep_follows_the_linear_model():
    sw = thermal.sweep(TIO2_ORD3, [0, 10, 20, 40], report_thermal(-2.2e-4), near=532)
    assert [r["delta_T_K"] for r in sw["rows"]] == [0, 10, 20, 40]
    assert sw["rows"][0]["center_nm"] == pytest.approx(532, abs=0.01)
    assert sw["dlambda_dT_pm_per_K"] == pytest.approx(-29.17, abs=0.5)
    centers = [r["center_nm"] for r in sw["rows"]]
    assert all(a > b for a, b in zip(centers, centers[1:]))  # blue shift with a negative TiO2 dn/dT


def test_clamped_and_free_strain():
    props = {"materials": {"TiO2": {"cte": 5e-6, "poisson": 0.27}, "SiO2": {"cte": 0.5e-6, "poisson": 0.17}},
             "substrate_cte": 0.55e-6}
    design = {"substrate": "FusedSilica", "layers": [{"material": "TiO2", "thickness": 60},
                                                    {"material": "SiO2", "thickness": 90}]}
    strain, warnings = thermal.strains(design, props)
    assert strain["TiO2"] == pytest.approx(8.29e-6, rel=1e-3) and strain["SiO2"] == pytest.approx(0.48e-6, rel=1e-2)
    assert not warnings
    free, _ = thermal.strains(design, dict(props, clamped=False))
    assert free == {"TiO2": 5e-6, "SiO2": 0.5e-6}
    with pytest.raises(engine.EngineError, match="poisson ratio and the substrate_cte"):
        thermal.strains(design, {"materials": {"TiO2": {"cte": 5e-6}}})
    with pytest.raises(engine.EngineError, match="unknown"):
        thermal.strains(design, {"materials": {"TiO2": {"dndT": 1e-4}}})


def test_perturbations_leave_the_library_alone():
    before = engine.list_materials("TiO2")
    thermal.sensitivity(SIO2_ORD1, near=532)
    assert engine.list_materials("TiO2") == before
    assert engine.material_N("TiO2", [532.0])[0].real == pytest.approx(2.36111, abs=1e-5)


@pytest.fixture
def fresh_pool():
    # The server's worker processes keep the data folder they started with: start them for this test's.
    from mcp_for_openfilters import server

    def shutdown():
        if server._interactive is not None:
            server._interactive.shutdown()
            server._interactive = None

    shutdown()
    yield server
    shutdown()


def test_server_tools(fresh_pool):
    import asyncio
    server = fresh_pool
    out = asyncio.run(server.thermal_sensitivity(SIO2_ORD1, near=532, thermal=report_thermal(-2.2e-4)))
    assert out["dlambda_dT_pm_per_K"] == pytest.approx(-13.10, abs=0.3)
    result = asyncio.run(server.thermal_sweep(SIO2_ORD1, [0, 50], report_thermal(-2.2e-4), near=532))
    assert result[0]["plot_files"] and result[0]["dlambda_dT_pm_per_K"] == pytest.approx(-13.1, abs=0.5)
