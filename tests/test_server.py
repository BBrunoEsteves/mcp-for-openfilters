import asyncio
import re

from mcp_for_openfilters import server


def test_every_tool_has_title_and_annotations():
    # The Claude directory requires them; destructive_hint must be set on tools that write.
    for tool in asyncio.run(server.mcp.list_tools()):
        assert tool.title, tool.name
        a = tool.annotations
        assert a is not None and a.read_only_hint is not None and a.open_world_hint is not None, tool.name
        if not a.read_only_hint:
            assert a.destructive_hint is not None, tool.name


def test_prompts_render_with_their_defaults():
    prompts = asyncio.run(server.mcp.list_prompts())
    assert {p.name for p in prompts} >= {"design_antireflection", "design_mirror", "design_edge_filter",
                                         "design_bandpass", "review_for_manufacturing"}
    for p in prompts:
        required = {a.name: "{}" for a in p.arguments or [] if a.required}
        text = asyncio.run(server.mcp.get_prompt(p.name, required)).messages[0].content.text
        assert not re.search(r"\{[a-z_]+\}", text), p.name  # every placeholder filled


def test_each_example_is_its_own_resource():
    # Claude Desktop lists fixed resources in its attach menu, not resource templates.
    uris = {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}
    assert {"openfilters://examples/AR", "openfilters://examples/Edge"} <= uris
    contents = list(asyncio.run(server.mcp.read_resource("openfilters://examples/Edge")))
    assert '"designs"' in contents[0].content


def test_prompt_default_materials_exist():
    # The workflows once defaulted to Ta2O5, which OpenFilters does not ship.
    import inspect
    from mcp_for_openfilters import engine
    names = set(engine.catalog().get_material_names())
    for fn in (server.design_antireflection, server.design_mirror, server.design_edge_filter, server.design_bandpass):
        params = inspect.signature(fn).parameters
        for material in params["materials"].default.split(","):
            assert material.strip() in names, (fn.__name__, material)
        assert params["substrate"].default in names, fn.__name__


def test_load_project_finds_saved_name(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_FOR_OPENFILTERS_HOME", str(tmp_path))
    d = {"substrate": "BK7", "layers": [{"material": "SiO2", "thickness": 100}]}
    targets = [{"kind": "R", "wavelength": 550, "value": 0.02, "inequality": "<="}]
    saved = asyncio.run(server.save_project("by_name", [d], targets))
    loaded = asyncio.run(server.load_project("by_name"))
    assert saved["path"].endswith("by_name.ofp") and len(loaded["designs"]) == 1


def test_empty_prompt_boxes_use_the_defaults():
    # Claude Desktop shows empty boxes (prompt arguments carry no defaults) and may send "".
    blank = {a.name: "" for a in next(p for p in asyncio.run(server.mcp.list_prompts())
                                      if p.name == "design_antireflection").arguments}
    text = asyncio.run(server.mcp.get_prompt("design_antireflection", blank)).messages[0].content.text
    assert "coating on BK7" in text and "materials TiO2, SiO2." in text
    described = next(p for p in asyncio.run(server.mcp.list_prompts()) if p.name == "design_mirror").arguments
    assert all(a.description and a.description.startswith("Default:") for a in described)


def _first(result):
    return result[0] if isinstance(result, list) else result


def test_threshold_ranges_and_fwhm_match_the_batch_1_references():
    mirror = {"substrate": "FusedSilica", "center_wavelength": 1064,
              "formula": {"formula": "(HL)^15 H", "materials": {"H": "TiO2", "L": "SiO2"}}}
    out = _first(asyncio.run(server.compute_spectrum(mirror, ["R"], {"from": 900, "to": 1300, "step": 1},
                                                     threshold={"above": 0.999}, plot=False)))
    [[start, end]] = out["series"][0]["ranges_above_0.999"]
    assert 956 < start < 957.5 and 1196.5 < end < 1198
    assert out["series"][0]["max"] > 0.999995  # 8 significant figures show 99.9995x %
    fp = {"substrate": "FusedSilica", "center_wavelength": 532,
          "formula": {"formula": "(HL)^5 HH (LH)^5", "materials": {"H": "TiO2", "L": "SiO2"}}}
    out = _first(asyncio.run(server.compute_spectrum(fp, ["T"], {"from": 529, "to": 535, "step": 0.05},
                                                     threshold={"half_max": True}, plot=False)))
    assert abs(out["series"][0]["fwhm"] - 1.14) < 0.02


def test_angle_scan_summary_finds_brewster():
    bare = {"substrate": "BK7", "layers": [{"material": "SiO2", "thickness": 0}]}
    out = _first(asyncio.run(server.compute_angle_scan(bare, 550, ["R"], {"from": 50, "to": 62, "step": 0.01},
                                                       ["p"], plot=False)))
    curve = out["curves"]["R p"]
    assert abs(curve["at_deg_min"] - 56.63) < 0.02 and len(curve["samples"]) <= 25
