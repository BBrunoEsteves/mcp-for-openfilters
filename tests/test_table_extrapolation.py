"""The design field "table_extrapolation": how tabulated materials are filled in beyond their data.

"hold" (default) keeps the end values (the port's deviation, see openfilters_core/PROVENANCE.md);
"openfilters" restores OpenFilters' own extrapolation (as in its GUI); "refuse" forbids use outside the
data. Long work (optimizations, campaigns, tolerance analyses, batches) waits for an explicit choice.
"""

import asyncio
import os

import pytest

from mcp_for_openfilters import engine

# The field report's TiO2 table (520-544 nm): OpenFilters extrapolated it to n = 4.90 at 1550 nm, 2.55 at 400 nm.
TIO2_TABLE = [[520, 2.37248, 6e-06], [526, 2.36666, 5e-06], [532, 2.36111, 4e-06], [538, 2.35583, 3e-06],
              [544, 2.35079, 2e-06]]
DESIGN = {"substrate": "FusedSilica", "center_wavelength": 532, "layers": [{"material": "tTiO2", "thickness": 100}]}


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path))
    engine.catalog(reload=True)
    engine.add_material("tTiO2", "table", table=TIO2_TABLE)


@pytest.fixture
def server(data_home, monkeypatch):
    from mcp_for_openfilters import server

    def shutdown():
        if server._interactive is not None:
            server._interactive.shutdown()
            server._interactive = None

    shutdown()
    monkeypatch.setattr(server, "_manager", None)
    yield server
    shutdown()


def with_mode(mode):
    return dict(DESIGN, table_extrapolation=mode)


def test_openfilters_mode_is_the_original_extrapolation():
    wavelengths = [400.0, 520.0, 523.0, 532.0, 541.0, 544.0, 1550.0]
    held = engine.material_N("tTiO2", wavelengths)
    original = engine.material_N("tTiO2", wavelengths, "openfilters")
    assert original[0].real == pytest.approx(2.55, abs=0.01) and original[-1].real == pytest.approx(4.90, abs=0.01)
    assert held[0].real == pytest.approx(2.37248) and held[-1].real == pytest.approx(2.35079)
    # At the data points both are the data; between them only the end intervals differ (the padding).
    for i in (1, 3, 5):
        assert original[i] == pytest.approx(held[i], abs=1e-12)


def test_spectra_follow_the_mode():
    hold = list(engine.spectra(with_mode("hold"), ["R"], [532, 700])["series"][0]["values"])
    default = list(engine.spectra(DESIGN, ["R"], [532, 700])["series"][0]["values"])
    original = list(engine.spectra(with_mode("openfilters"), ["R"], [532, 700])["series"][0]["values"])
    assert default == hold
    assert original[0] == pytest.approx(hold[0], abs=1e-12) and abs(original[1] - hold[1]) > 1e-3


def test_warnings_give_the_values_used_and_the_alternative():
    (default,) = [w for w in engine.material_warnings(DESIGN, [532, 700]) if w.startswith("tTiO2")]
    assert 'table_extrapolation "hold", the default' in default and "at 700 nm n = 2.351" in default
    assert "OpenFilters' extrapolation would give" in default
    (chosen,) = [w for w in engine.material_warnings(with_mode("openfilters"), [532, 700]) if w.startswith("tTiO2")]
    assert 'extrapolated as OpenFilters does (end segments of the interpolation) (table_extrapolation "openfilters")' in chosen
    assert "Holding the end values would give n = 2.351" in chosen


def test_refuse_and_unknown_modes():
    with pytest.raises(engine.EngineError, match='"refuse" forbids'):
        engine.spectra(with_mode("refuse"), ["R"], [532, 700])
    assert engine.spectra(with_mode("refuse"), ["R"], [520, 544])["series"]  # inside the data: fine
    with pytest.raises(engine.EngineError, match="table_extrapolation must be one of"):
        engine.spectra(with_mode("linear"), ["R"], [532])


def test_far_beyond_the_data_needs_a_choice():
    with pytest.raises(engine.EngineError, match="ask the user and set the design's table_extrapolation"):
        engine.spectra(DESIGN, ["R"], [532, 1550])
    assert engine.spectra(with_mode("hold"), ["R"], [532, 1550])["series"]
    assert engine.spectra(with_mode("openfilters"), ["R"], [532, 1550])["series"]


def test_unphysical_extrapolation_is_refused():
    engine.add_material("tFalling", "table", table=[[400, 2.0, 0], [500, 1.5, 0], [600, 1.0, 0]])
    design = {"substrate": "FusedSilica", "center_wavelength": 500,
              "layers": [{"material": "tFalling", "thickness": 100}], "table_extrapolation": "openfilters"}
    with pytest.raises(engine.EngineError, match="unphysical index .* with table_extrapolation openfilters"):
        engine.spectra(design, ["R"], [500, 900])
    assert engine.spectra(dict(design, table_extrapolation="hold"), ["R"], [500, 900])["series"]


def test_the_mode_travels_with_the_design():
    for mode in ("hold", "openfilters"):
        assert engine.design_from_filter(engine.build_filter(with_mode(mode)))["table_extrapolation"] == mode
    assert "table_extrapolation" not in engine.design_from_filter(engine.build_filter(DESIGN))
    target = [{"kind": "R", "wavelength": 700, "value": 0.3}]
    result = engine.optimize(with_mode("openfilters"), target, "refine", {"time_limit": 5})
    assert result["design"]["table_extrapolation"] == "openfilters"


def test_long_work_waits_for_a_choice():
    targets = [{"kind": "R", "from": 520, "to": 700, "step": 10, "value": 0.1, "inequality": "<="}]
    with pytest.raises(engine.EngineError, match=r"tTiO2 \(data 520-544 nm, used at 544-700 nm\). Ask the user"):
        engine.check_long_job("optimize", {"design": DESIGN, "target_specs": targets})
    with pytest.raises(engine.EngineError, match="This tolerance analysis uses materials outside"):
        engine.check_tolerance(DESIGN, targets)
    engine.check_long_job("optimize", {"design": with_mode("hold"), "target_specs": targets})
    engine.check_long_job("optimize", {"design": DESIGN, "target_specs": [dict(targets[0], to=540)]})  # inside
    with pytest.raises(engine.EngineError, match="This spectrum job"):
        engine.check_long_job("spectrum", {"design": DESIGN, "wavelengths": {"from": 500, "to": 600, "step": 5}})


def test_server_refuses_long_jobs_before_creating_them(server):
    from mcp_for_openfilters import settings
    targets = [{"kind": "R", "from": 520, "to": 700, "step": 10, "value": 0.1, "inequality": "<="}]
    with pytest.raises(server.ToolError, match="Ask the user"):
        asyncio.run(server.optimize_design(DESIGN, targets, method="refine", wait_seconds=0))
    with pytest.raises(server.ToolError, match="Ask the user"):
        asyncio.run(server.run_optimization_campaign(targets, starts=[DESIGN]))
    with pytest.raises(server.ToolError, match="Ask the user"):
        asyncio.run(server.run_batch("evaluate", {"design": DESIGN, "target_specs": targets}, {"x": [1]}))
    folder = settings.jobs_dir()
    assert not os.path.isdir(folder) or os.listdir(folder) == []


def test_saving_warns_where_the_gui_will_differ():
    targets = [{"kind": "R", "from": 520, "to": 700, "step": 10, "value": 0.1, "inequality": "<="}]
    (note,) = engine.gui_extrapolation_notes([DESIGN], targets)
    assert "design 1: tTiO2" in note and 'table_extrapolation "hold"' in note and "GUI extrapolates" in note
    assert engine.gui_extrapolation_notes([with_mode("openfilters")], targets) == []
    # The GUI analyses the design's wavelength_range (or the targets' with a margin): inside the data, no note.
    inside = dict(DESIGN, wavelength_range={"from": 522, "to": 542, "step": 1})
    assert engine.gui_extrapolation_notes([inside], targets) == []
